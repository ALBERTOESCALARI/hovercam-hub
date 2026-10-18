"""Thin client for go2rtc's HTTP API (https://github.com/AlexxIT/go2rtc).
go2rtc does the heavy lifting: pulls RTSP from cameras and serves WebRTC."""

from urllib.parse import quote

import aiohttp


class Go2RTCError(Exception):
    pass


class Go2RTC:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self.session = session
        self.base = base_url

    async def add_stream(self, name: str, src: str) -> None:
        url = f"{self.base}/api/streams?name={quote(name)}&src={quote(src, safe='')}"
        async with self.session.put(url) as res:
            if res.status >= 400:
                detail = (await res.text()).strip()[:200]
                raise Go2RTCError(f"add stream failed ({res.status}: {detail})")

    async def remove_stream(self, name: str) -> None:
        async with self.session.delete(f"{self.base}/api/streams?src={quote(name)}") as res:
            if res.status >= 400 and res.status != 404:
                raise Go2RTCError(f"remove stream failed ({res.status})")

    async def webrtc_answer(self, name: str, offer_sdp: str) -> str:
        """Non-trickle exchange: complete offer in, complete answer out."""
        async with self.session.post(
            f"{self.base}/api/webrtc?src={quote(name)}",
            data=offer_sdp,
            headers={"Content-Type": "application/sdp"},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as res:
            body = await res.text()
            if res.status >= 400 or not body.startswith("v=0"):
                raise Go2RTCError(f"webrtc negotiation failed ({res.status})")
            return body

    async def snapshot(self, name: str, timeout: float = 10) -> bytes:
        async with self.session.get(
            f"{self.base}/api/frame.jpeg?src={quote(name)}",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as res:
            body = await res.read()
            # go2rtc can answer with an error message instead of a picture (e.g. the
            # camera rejected the login), so only trust real JPEG bytes.
            if res.status >= 400 or not body.startswith(b"\xff\xd8"):
                detail = body[:200].decode("utf-8", "replace").strip()
                raise Go2RTCError(f"no image from camera: {detail or res.status}")
            return body
