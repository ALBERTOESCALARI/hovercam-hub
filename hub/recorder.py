"""Async client for Xiongmai / XMEye recorders (NVR/DVR) over their DVRIP port (34567).

Log in, read model and channel names, list recordings, stream a recording for a time
range, listen for alarms (motion, video loss), and read/change settings (see nvr_settings). Protocol notes: 20-byte header (0xFF, version, session, sequence, msg id,
length) + JSON body terminated by "\\n\\0"; video arrives as msg 1426 packets until an empty one.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import struct
from collections.abc import AsyncIterator
from datetime import datetime

PORT = 34567
FMT = "%Y-%m-%d %H:%M:%S"
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ERRORS = {
    106: "wrong recorder username or password",
    203: "wrong recorder password",
    205: "the recorder locked this login after too many wrong passwords; try again in ~30 minutes",
}


class RecorderError(Exception):
    pass


def sofia_hash(password: str) -> str:
    d = hashlib.md5(password.encode()).digest()
    return "".join(_CHARS[(a + b) % 62] for a, b in zip(d[::2], d[1::2]))


class RecorderClient:
    def __init__(self, host: str, username: str, password: str, timeout: float = 10) -> None:
        self.host, self.username, self.password, self.timeout = host, username, password, timeout
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self.session, self.seq = 0, 0
        self.alive = 20  # seconds between keep-alives (the recorder says at login)

    async def __aenter__(self) -> RecorderClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._r, self._w = await asyncio.wait_for(
                asyncio.open_connection(self.host, PORT), self.timeout)
        except (OSError, asyncio.TimeoutError):
            raise RecorderError(f"can't reach a recorder at {self.host}") from None
        await self.login()

    async def close(self) -> None:
        if self._w is not None:
            if self.session:   # log out, so the recorder frees the session right away
                try:
                    await asyncio.wait_for(
                        self._send_only(1002, {"Name": "", "SessionID": "0x%08X" % self.session}),
                        2)
                except Exception:  # noqa: BLE001
                    pass
            self._w.close()
            try:
                await self._w.wait_closed()
            except Exception:
                pass
            self._w = None

    async def _send_only(self, msg: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self._w.write(struct.pack("<BB2xII2xHI", 255, 0, self.session, self.seq, msg,
                                  len(body) + 2) + body + b"\x0a\x00")
        self.seq += 1
        await self._w.drain()

    async def _read_packet(self, timeout: float | None = None) -> tuple[int, bytes]:
        hdr = await asyncio.wait_for(self._r.readexactly(20), timeout or self.timeout)
        msgid = struct.unpack("<H", hdr[14:16])[0]
        size = struct.unpack("<I", hdr[16:20])[0]
        payload = await asyncio.wait_for(self._r.readexactly(size), timeout or self.timeout) \
            if size else b""
        return msgid, payload

    async def _call(self, msg: int, data: dict) -> dict:
        await self._send_only(msg, data)
        _, payload = await self._read_packet()
        text = payload.rstrip(b"\x00\x0a").decode("utf-8", "replace")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    async def login(self) -> None:
        r = await self._call(1000, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                                    "PassWord": sofia_hash(self.password),
                                    "UserName": self.username})
        if r.get("Ret") not in (100, 515):
            raise RecorderError(_ERRORS.get(r.get("Ret"), f"recorder refused login ({r.get('Ret')})"))
        self.session = int(r["SessionID"], 16)
        self.alive = max(5, min(int(r.get("AliveInterval") or 20), 60))

    async def _get(self, name: str, code: int):
        r = await self._call(code, {"Name": name, "SessionID": "0x%08X" % self.session})
        return r.get(name)

    # ---- settings ----------------------------------------------------------------------
    # Ret 100 = OK, 515 = OK (upgrade notice), 603 = saved, takes effect after a restart.
    OK = (100, 515, 603)

    async def config_get(self, name: str, code: int = 1042):
        r = await self._call(code, {"Name": name, "SessionID": "0x%08X" % self.session})
        if r.get("Ret") not in self.OK or name not in r:
            raise RecorderError(f"the recorder doesn't offer {name} (code {r.get('Ret')})")
        return r[name]

    async def config_set(self, name: str, value, code: int = 1040) -> int:
        r = await self._call(code, {"Name": name, name: value,
                                    "SessionID": "0x%08X" % self.session})
        if r.get("Ret") not in self.OK:
            raise RecorderError(f"the recorder refused the change to {name} "
                                f"(code {r.get('Ret')})")
        return r["Ret"]

    async def set_titles(self, titles: list[str]) -> int:
        return await self.config_set("ChannelTitle", titles, code=1046)

    async def get_time(self) -> datetime | None:
        r = await self._call(1452, {"Name": "OPTimeQuery", "SessionID": "0x%08X" % self.session})
        try:
            return datetime.strptime(str(r.get("OPTimeQuery")), FMT)
        except ValueError:
            return None

    async def set_time(self, when: datetime) -> int:
        return await self.config_set("OPTimeSetting", when.strftime(FMT), code=1450)

    async def reboot(self) -> None:
        await self._send_only(1450, {"Name": "OPMachine", "OPMachine": {"Action": "Reboot"},
                                     "SessionID": "0x%08X" % self.session})

    async def describe(self) -> dict:
        info = await self._get("SystemInfo", 1020) or {}
        titles = await self._get("ChannelTitle", 1048) or []
        return {"model": info.get("DeviceModel") or info.get("HardWare"),
                "firmware": info.get("SoftWareVersion"),
                "channels": [str(t) for t in titles]}

    async def files(self, channel: int, begin: str, end: str) -> list[dict]:
        """All recordings that overlap [begin, end]; the recorder pages at 64."""
        out: list[dict] = []
        start = begin
        for _ in range(40):
            r = await self._call(1440, {"Name": "OPFileQuery", "OPFileQuery": {
                "BeginTime": start, "EndTime": end, "Channel": channel,
                "DriverTypeMask": "0x0000FFFF", "Event": "*", "StreamType": "0x00000000",
                "Type": "h264"}})
            batch = (r.get("OPFileQuery") or []) if r.get("Ret") == 100 else []
            new = [f for f in batch if f not in out]
            out += new
            if len(batch) < 64 or not new:
                break
            start = batch[-1]["BeginTime"]
        return [{"name": f["FileName"], "begin": f["BeginTime"], "end": f["EndTime"],
                 "size_kb": int(str(f.get("FileLength", "0")), 0)} for f in out]

    async def _keepalive(self) -> None:
        """Recorders drop a session that hasn't sent a keep-alive for a while, even if it is
        busy receiving (alarms, a download). Send one on a timer, whatever else happens."""
        sid = "0x%08X" % self.session
        try:
            while True:
                await asyncio.sleep(self.alive)
                await self._send_only(1006, {"Name": "KeepAlive", "SessionID": sid})
        except (asyncio.CancelledError, ConnectionError, OSError, AttributeError):
            pass

    async def stream(self, file_name: str, start: str, end: str) -> AsyncIterator[bytes]:
        """Yield the raw recording (Xiongmai framing) for start..end of one file."""
        param = {"PlayMode": "ByName", "FileName": file_name, "StreamType": 0, "Value": 0,
                 "TransMode": "TCP"}
        claim = await self._call(1424, {"Name": "OPPlayBack", "OPPlayBack": {
            "Action": "Claim", "Parameter": param, "StartTime": start, "EndTime": end}})
        if claim.get("Ret") not in (100, None):
            raise RecorderError(f"recorder refused playback ({claim.get('Ret')})")
        await self._send_only(1420, {"Name": "OPPlayBack", "OPPlayBack": {
            "Action": "DownloadStart", "Parameter": param, "StartTime": start, "EndTime": end}})
        keepalive = asyncio.create_task(self._keepalive())
        try:
            first = True
            while True:
                try:
                    msgid, data = await self._read_packet(timeout=30)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    return
                if msgid == 1007:      # keep-alive reply, not video
                    continue
                if not data:
                    return
                if first and data.lstrip().startswith(b"{"):
                    first = False
                    continue
                first = False
                yield data
        finally:
            keepalive.cancel()


    async def alarms(self) -> AsyncIterator[dict]:
        """Subscribe to the recorder's alarms (motion, video loss, ...) and yield each
        AlarmInfo as it arrives, e.g. {"Channel": 0, "Event": "VideoMotion",
        "StartTime": "2026-10-18 10:40:12", "Status": "Start"}. Sends keep-alives so the
        recorder doesn't drop the session. Returns when the connection ends."""
        sid = "0x%08X" % self.session
        await self._send_only(1500, {"Name": "", "SessionID": sid})   # guard / subscribe
        # Keep-alives go out on their own timer: a busy camera sends alarm messages every
        # second, and a keep-alive sent only when it's quiet never went out (the recorder
        # then dropped the session about once a minute).
        keepalive = asyncio.create_task(self._keepalive())
        try:
            while True:
                try:
                    msgid, payload = await self._read_packet(timeout=self.alive * 4)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                        OSError):
                    return   # nothing at all (not even keep-alive replies): connection is dead
                if msgid != 1504:
                    continue  # subscribe / keep-alive replies
                text = payload.rstrip(b"\x00\x0a").decode("utf-8", "replace")
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    continue
                info = body.get("AlarmInfo") or body.get(body.get("Name", "")) or {}
                if isinstance(info, dict) and info:
                    yield info
        finally:
            keepalive.cancel()


def local_subnet() -> ipaddress.IPv4Network | None:
    """The /24 this machine is on (for scanning). No traffic is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


async def scan(timeout: float = 0.5) -> list[str]:
    """Addresses on the local /24 with the recorder port open (recorders AND XM cameras)."""
    net = local_subnet()
    if net is None:
        return []
    sem = asyncio.Semaphore(64)

    async def probe(host: str) -> str | None:
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(host, PORT), timeout)
                w.close()
                return host
            except (OSError, asyncio.TimeoutError):
                return None

    found = await asyncio.gather(*(probe(str(h)) for h in net.hosts()))
    return sorted((h for h in found if h), key=lambda h: ipaddress.ip_address(h))


def day_bounds(day: str) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.strftime("%Y-%m-%d 00:00:00"), d.strftime("%Y-%m-%d 23:59:59")
