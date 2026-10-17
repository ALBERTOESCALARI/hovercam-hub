"""HoverCam hub agent.

1. First boot: registers with the cloud, saves hub id/secret, shows a pairing code.
2. Re-adds every saved camera to go2rtc (go2rtc forgets API-added streams on restart).
3. Holds a WebSocket to the cloud and answers commands:
   webrtc.offer, snapshot, discover, camera.add, camera.remove, ping.
"""

import asyncio
import base64
import json
import logging
import random
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from hub import __version__, discovery, status_page
from hub.config import Config
from hub.go2rtc import Go2RTC, Go2RTCError
from hub.state import HubState, StateStore

log = logging.getLogger("hovercam.hub")


class CommandError(Exception):
    pass


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_dir)
        self.state: HubState = self.store.load()
        self.session: aiohttp.ClientSession | None = None
        self.go2rtc: Go2RTC | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.go2rtc = Go2RTC(session, self.config.go2rtc_url)
            await self._start_status_page()
            await self._ensure_registered()
            await self._restore_streams()
            await asyncio.gather(self._pairing_watch(), self._socket_forever())

    async def _start_status_page(self) -> None:
        runner = web.AppRunner(status_page.make_app(lambda: self.state))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.config.status_port).start()
        log.info("status page on http://<this-device>:%s", self.config.status_port)

    async def _ensure_registered(self) -> None:
        while not self.state.hub_id:
            try:
                async with self.session.post(
                    f"{self.config.api_url}/hubs/register",
                    json={"name": self.config.hub_name, "agent_version": __version__},
                ) as res:
                    res.raise_for_status()
                    data = await res.json()
            except aiohttp.ClientError as exc:
                log.warning("register failed (%s); retrying in 10s", exc)
                await asyncio.sleep(10)
                continue
            self.state.hub_id = data["hub_id"]
            self.state.hub_secret = data["hub_secret"]
            self._set_pairing(data)
            self.store.save(self.state)
        if not self.state.paired:
            self._announce_pairing()

    def _set_pairing(self, data: dict) -> None:
        self.state.pairing_code = data["pairing_code"]
        self.state.pairing_expires_at = data["pairing_expires_at"]

    def _announce_pairing(self) -> None:
        log.info("=" * 48)
        log.info("  PAIRING CODE:  %s", self.state.pairing_code)
        log.info("  Enter it in the HoverCam app -> Add hub")
        log.info("=" * 48)

    def _hub_headers(self) -> dict[str, str]:
        return {"X-Hub-Id": self.state.hub_id, "X-Hub-Secret": self.state.hub_secret}

    async def _pairing_watch(self) -> None:
        """While unpaired, re-issue the pairing code each time it expires. The cloud
        also pushes a "status" message the moment the owner claims the hub."""
        while not self.state.paired:
            remaining = 0.0
            if self.state.pairing_expires_at:
                expires = datetime.fromisoformat(
                    self.state.pairing_expires_at.replace("Z", "+00:00"))
                remaining = (expires - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 30.0))
                continue
            if not await self._refresh_pairing_code():
                await asyncio.sleep(30)

    async def _refresh_pairing_code(self) -> bool:
        try:
            async with self.session.post(
                f"{self.config.api_url}/hubs/pairing-code", headers=self._hub_headers()
            ) as res:
                if res.status == 409:
                    self._mark_paired()
                    return True
                res.raise_for_status()
                self._set_pairing(await res.json())
                self.store.save(self.state)
                self._announce_pairing()
                return True
        except aiohttp.ClientError as exc:
            log.warning("pairing refresh failed: %s", exc)
            return False

    def _mark_paired(self) -> None:
        if not self.state.paired:
            self.state.paired = True
            self.state.pairing_code = None
            self.state.pairing_expires_at = None
            self.store.save(self.state)
            log.info("hub is paired")

    async def _restore_streams(self) -> None:
        for key, url in self.state.cameras.items():
            try:
                await self.go2rtc.add_stream(key, url)
            except (Go2RTCError, aiohttp.ClientError) as exc:
                log.error("could not restore %s: %s (is go2rtc running?)", key, exc)

    # ---- cloud socket ----------------------------------------------------------------

    async def _socket_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self.session.ws_connect(
                    self.config.ws_url, headers=self._hub_headers(), heartbeat=25
                ) as ws:
                    log.info("connected to cloud")
                    backoff = 1.0
                    await ws.send_json({"type": "hello", "data": {
                        "agent_version": __version__,
                        "cameras": sorted(self.state.cameras),
                    }})
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # Each command in its own task: a slow discovery sweep must
                            # never hold up someone opening a live view.
                            task = asyncio.create_task(self._handle(ws, json.loads(msg.data)))
                            self._tasks.add(task)
                            task.add_done_callback(self._tasks.discard)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    if ws.close_code == 4401:
                        log.error("cloud rejected hub credentials; delete state to re-register")
            except aiohttp.ClientError as exc:
                log.warning("cloud connection failed: %s", exc)
            delay = backoff + random.uniform(0, backoff / 2)
            log.info("reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 60.0)

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> None:
        request_id = message.get("id")
        if request_id is None:
            # Unsolicited notice from the cloud.
            if message.get("type") == "status" and (message.get("data") or {}).get("paired"):
                self._mark_paired()
            return
        try:
            data = await self._dispatch(message.get("type", ""), message.get("data") or {})
            reply = {"id": request_id, "ok": True, "data": data}
        except (CommandError, Go2RTCError) as exc:
            reply = {"id": request_id, "ok": False, "error": str(exc)}
        except Exception:
            log.exception("command %s failed", message.get("type"))
            reply = {"id": request_id, "ok": False, "error": "internal hub error"}
        if not ws.closed:
            await ws.send_json(reply)

    async def _dispatch(self, command: str, data: dict) -> dict:
        self._mark_paired()  # only a claimed hub receives commands
        if command == "ping":
            return {"agent_version": __version__}
        if command == "discover":
            return {"cameras": await discovery.discover()}
        if command == "camera.add":
            return await self._camera_add(data["stream_key"], data["rtsp_url"])
        if command == "camera.remove":
            key = data["stream_key"]
            await self.go2rtc.remove_stream(key)
            self.state.cameras.pop(key, None)
            self.store.save(self.state)
            return {}
        if command == "webrtc.offer":
            key = self._known(data["stream_key"])
            return {"sdp": await self.go2rtc.webrtc_answer(key, data["sdp"])}
        if command == "snapshot":
            key = self._known(data["stream_key"])
            jpeg = await self.go2rtc.snapshot(key)
            return {"jpeg_b64": base64.b64encode(jpeg).decode()}
        raise CommandError(f"unknown command {command!r}")

    def _known(self, key: str) -> str:
        if key not in self.state.cameras:
            raise CommandError("camera not found on this hub")
        return key

    async def _camera_add(self, key: str, rtsp_url: str) -> dict:
        await self.go2rtc.add_stream(key, rtsp_url)
        try:
            # Prove the URL/credentials work before the cloud saves the camera.
            await self.go2rtc.snapshot(key, timeout=12)
        except (Go2RTCError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await self.go2rtc.remove_stream(key)
            hint = ("the camera needs a username and password"
                    if "user/pass" in str(exc) or "401" in str(exc)
                    else "check the RTSP URL, username and password")
            raise CommandError(f"could not read video from that address; {hint}") from None
        self.state.cameras[key] = rtsp_url
        self.store.save(self.state)
        return {}
