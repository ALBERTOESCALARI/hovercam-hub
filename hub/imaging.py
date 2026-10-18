"""Camera picture settings over ONVIF Imaging: brightness, color, contrast, sharpness and
day/night (IR-cut filter).

Flow: GetCapabilities(All) -> Media + Imaging addresses; GetProfiles -> video source token;
then GetImagingSettings / GetOptions / SetImagingSettings on that source.
"""

import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape

import aiohttp

from hub import discovery
from hub.onvif import OnvifCamera, OnvifError, _reachable

TIMG = "http://www.onvif.org/ver20/imaging/wsdl"
TT = "http://www.onvif.org/ver10/schema"

# Our name -> ONVIF element, in the order ImagingSettings20 requires (schema sequence).
FIELDS = [
    ("brightness", "Brightness"),
    ("saturation", "ColorSaturation"),
    ("contrast", "Contrast"),
    ("ir_cut", "IrCutFilter"),
    ("sharpness", "Sharpness"),
]
NUMERIC = {"brightness", "saturation", "contrast", "sharpness"}
IR_MODES = ("AUTO", "ON", "OFF")
ONVIF_PORTS = (8899, 80, 8000, 8080, 2020)   # 8899 is the Xiongmai/XMEye default


def login_from_rtsp(rtsp_url: str) -> tuple[str, str, str]:
    """(host, username, password) from the camera's RTSP URL stored on the hub."""
    u = urlparse(rtsp_url)
    return u.hostname or "", unquote(u.username or ""), unquote(u.password or "")


def parse_settings(root: ET.Element) -> dict:
    s = root.find(".//{*}ImagingSettings")
    out: dict = {}
    if s is None:
        return out
    for key, tag in FIELDS:
        text = s.findtext(f"{{*}}{tag}")
        if text is None:
            continue
        text = text.strip()
        if key in NUMERIC:
            try:
                out[key] = float(text)
            except ValueError:
                pass
        elif text.upper() in IR_MODES:
            out[key] = text.upper()
    return out


def parse_options(root: ET.Element) -> dict:
    opts = root.find(".//{*}ImagingOptions")
    ranges: dict = {}
    ir_modes: list[str] = []
    if opts is None:
        return {"ranges": ranges, "ir_cut_modes": ir_modes}
    for key, tag in FIELDS:
        if key in NUMERIC:
            node = opts.find(f"{{*}}{tag}")
            if node is not None:
                try:
                    lo, hi = float(node.findtext("{*}Min")), float(node.findtext("{*}Max"))
                    if hi > lo:
                        ranges[key] = [lo, hi]
                except (TypeError, ValueError):
                    pass
    for m in opts.findall("{*}IrCutFilterModes"):
        mode = (m.text or "").strip().upper()
        if mode in IR_MODES and mode not in ir_modes:
            ir_modes.append(mode)
    return {"ranges": ranges, "ir_cut_modes": ir_modes}


def settings_body(source: str, changes: dict) -> str:
    parts = []
    for key, tag in FIELDS:
        if key not in changes:
            continue
        value = changes[key]
        if key in NUMERIC:
            value = f"{float(value):g}"
        elif str(value).upper() in IR_MODES:
            value = str(value).upper()
        else:
            continue
        parts.append(f'<tt:{tag} xmlns:tt="{TT}">{value}</tt:{tag}>')
    if not parts:
        raise OnvifError("nothing to change")
    return (
        f'<timg:SetImagingSettings xmlns:timg="{TIMG}">'
        f"<timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken>"
        f"<timg:ImagingSettings>{''.join(parts)}</timg:ImagingSettings>"
        "<timg:ForcePersistence>true</timg:ForcePersistence>"
        "</timg:SetImagingSettings>"
    )


class CameraImaging:
    """One camera's picture settings. Addresses are found once and reused."""

    def __init__(self, cam: OnvifCamera, imaging_url: str, source: str) -> None:
        self.cam = cam
        self.url = imaging_url
        self.source = source

    @classmethod
    async def connect(cls, session: aiohttp.ClientSession, xaddr: str, username: str,
                      password: str) -> "CameraImaging":
        cam = OnvifCamera(session, xaddr, username, password)
        await cam.sync_clock()
        caps = await cam._call(
            xaddr, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        imaging = caps.findtext(".//{*}Imaging/{*}XAddr")
        if not imaging:
            raise OnvifError("this camera does not offer picture settings over ONVIF")
        media = _reachable(caps.findtext(".//{*}Media/{*}XAddr") or xaddr, cam.host)
        profiles = await cam._call(media, "<trt:GetProfiles/>")
        source = None
        for node in profiles.iter():
            if node.tag.endswith("VideoSourceConfiguration"):
                source = (node.findtext("{*}SourceToken") or "").strip() or None
                if source:
                    break
        if not source:
            raise OnvifError("camera reported no video source")
        return cls(cam, _reachable(imaging, cam.host), source)

    async def get(self) -> dict:
        tok = f"<timg:VideoSourceToken>{escape(self.source)}</timg:VideoSourceToken>"
        settings = parse_settings(await self.cam._call(
            self.url, f'<timg:GetImagingSettings xmlns:timg="{TIMG}">{tok}</timg:GetImagingSettings>'))
        try:
            options = parse_options(await self.cam._call(
                self.url, f'<timg:GetOptions xmlns:timg="{TIMG}">{tok}</timg:GetOptions>'))
        except OnvifError:
            options = {"ranges": {}, "ir_cut_modes": []}
        # Sliders need a range; ONVIF's usual 0-100 when the camera doesn't say.
        for key in NUMERIC:
            if key in settings and key not in options["ranges"]:
                options["ranges"][key] = [0.0, 100.0]
        if "ir_cut" in settings and not options["ir_cut_modes"]:
            options["ir_cut_modes"] = list(IR_MODES)
        return {"settings": settings, **options}

    async def set(self, changes: dict) -> None:
        await self.cam._call(self.url, settings_body(self.source, changes))


async def find_xaddr(session: aiohttp.ClientSession, host: str) -> str:
    """The camera's ONVIF device address: try the usual ports at once (unauthenticated
    GetSystemDateAndTime), then fall back to WS-Discovery."""
    async def answers(port: int) -> str | None:
        url = f"http://{host}:{port}/onvif/device_service"
        try:
            await OnvifCamera(session, url, "", "")._call(
                url, "<tds:GetSystemDateAndTime/>", auth=False)
            return url
        except OnvifError:
            return None

    results = await asyncio.gather(*(answers(p) for p in ONVIF_PORTS))
    for url in results:
        if url:
            return url
    for cam in await discovery.discover(timeout=2.5):
        if cam.get("address") == host and cam.get("xaddr"):
            return _reachable(cam["xaddr"], host)
    raise OnvifError("could not find the camera's ONVIF service")
