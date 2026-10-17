"""Minimal ONVIF client: ask a camera for its own RTSP address.

Uses WS-Security UsernameToken (password digest), timed with the camera's clock so
cameras whose time is off still accept it. Standard library + aiohttp only.

Flow: GetSystemDateAndTime (no auth) -> GetDeviceInformation -> GetCapabilities(Media)
-> GetProfiles -> pick the best profile -> GetStreamUri -> add credentials.
"""

import base64
import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape

import aiohttp

ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    "<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
)
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
          "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
B64 = ("http://docs.oasis-open.org/wss/2004/01/"
       "oasis-200401-soap-message-security-1.0#Base64Binary")
MAX_AREA = 1920 * 1080  # phones play 1080p smoothly; 4K main streams are wasted bandwidth


class OnvifError(Exception):
    pass


def security_header(username: str, password: str, now: datetime) -> str:
    nonce = os.urandom(16)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        f'<Security s:mustUnderstand="1" xmlns="{WSSE}"><UsernameToken>'
        f"<Username>{escape(username)}</Username>"
        f'<Password Type="{DIGEST}">{digest}</Password>'
        f'<Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</Nonce>'
        f'<Created xmlns="{WSU}">{created}</Created>'
        "</UsernameToken></Security>"
    )


def parse_camera_time(root: ET.Element) -> datetime | None:
    utc = root.find(".//{*}UTCDateTime")
    if utc is None:
        return None
    try:
        d, t = utc.find("{*}Date"), utc.find("{*}Time")
        return datetime(
            int(d.findtext("{*}Year")), int(d.findtext("{*}Month")), int(d.findtext("{*}Day")),
            int(t.findtext("{*}Hour")), int(t.findtext("{*}Minute")),
            int(t.findtext("{*}Second")), tzinfo=timezone.utc,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def parse_profiles(root: ET.Element) -> list[dict]:
    profiles = []
    for p in root.findall(".//{*}Profiles"):
        enc = p.find(".//{*}VideoEncoderConfiguration")
        width = height = 0
        encoding = None
        if enc is not None:
            encoding = (enc.findtext("{*}Encoding") or "").strip() or None
            try:
                width = int(enc.findtext("{*}Resolution/{*}Width") or 0)
                height = int(enc.findtext("{*}Resolution/{*}Height") or 0)
            except ValueError:
                pass
        if p.get("token"):
            profiles.append({"token": p.get("token"), "name": p.findtext("{*}Name"),
                             "encoding": encoding, "width": width, "height": height})
    return profiles


def pick_profile(profiles: list[dict]) -> dict:
    """Prefer H.264 (plays everywhere over WebRTC), then the largest size up to 1080p."""
    h264 = [p for p in profiles if (p["encoding"] or "").upper().replace(".", "") == "H264"]
    pool = h264 or profiles
    fits = [p for p in pool if 0 < p["width"] * p["height"] <= MAX_AREA] or pool
    return max(fits, key=lambda p: p["width"] * p["height"])


def with_credentials(uri: str, reach_host: str, username: str, password: str) -> str:
    """Put the login into the RTSP URL and use the host we actually reached (cameras
    sometimes report another interface or 0.0.0.0)."""
    parsed = urlparse(uri)
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
    return parsed._replace(netloc=f"{auth}{reach_host}{port}").geturl()


def _reachable(url: str, reach_host: str) -> str:
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{reach_host}{port}").geturl()


class OnvifCamera:
    def __init__(self, session: aiohttp.ClientSession, xaddr: str, username: str,
                 password: str) -> None:
        self.session = session
        self.xaddr = xaddr
        self.host = urlparse(xaddr).hostname or ""
        self.username = username
        self.password = password
        self.clock_offset = timedelta(0)

    async def _call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        header = ""
        if auth:
            header = security_header(self.username, self.password,
                                     datetime.now(timezone.utc) + self.clock_offset)
        try:
            async with self.session.post(
                url, data=ENVELOPE.format(header=header, body=body).encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as res:
                status, text = res.status, await res.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OnvifError(f"camera did not answer ONVIF ({exc.__class__.__name__})") from None
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise OnvifError(f"camera sent an unexpected reply (HTTP {status})") from None
        fault = root.find(".//{*}Fault")
        if fault is not None:
            reason = " ".join(fault.itertext()).strip()
            if status in (400, 401) or "NotAuthorized" in reason or "uthori" in reason:
                raise OnvifError("wrong camera username or password")
            raise OnvifError(f"camera refused the request: {reason[:120]}")
        return root

    async def resolve(self) -> dict:
        try:  # sync to the camera's clock so the digest isn't rejected as stale
            cam_now = parse_camera_time(
                await self._call(self.xaddr, "<tds:GetSystemDateAndTime/>", auth=False))
            if cam_now:
                self.clock_offset = cam_now - datetime.now(timezone.utc)
        except OnvifError:
            pass

        info = {"manufacturer": None, "model": None}
        try:
            dev = await self._call(self.xaddr, "<tds:GetDeviceInformation/>")
            info = {"manufacturer": dev.findtext(".//{*}Manufacturer"),
                    "model": dev.findtext(".//{*}Model")}
        except OnvifError as exc:
            if "password" in str(exc):
                raise

        caps = await self._call(
            self.xaddr,
            "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>")
        media = caps.findtext(".//{*}Media/{*}XAddr") or self.xaddr
        media = _reachable(media, self.host)

        profiles = parse_profiles(await self._call(media, "<trt:GetProfiles/>"))
        if not profiles:
            raise OnvifError("camera reported no video streams")
        chosen = pick_profile(profiles)

        uri_root = await self._call(
            media,
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(chosen['token'])}</trt:ProfileToken></trt:GetStreamUri>")
        uri = (uri_root.findtext(".//{*}MediaUri/{*}Uri") or "").strip()
        if not uri.startswith("rtsp"):
            raise OnvifError("camera did not give a video address")
        return {
            **info,
            "rtsp_url": with_credentials(uri, self.host, self.username, self.password),
            "profile": chosen,
        }
