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
import secrets
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from hub import __version__, discovery, imaging, onvif, recorder, status_page
from hub.config import Config
from hub.go2rtc import Go2RTC, Go2RTCError
from hub.recorder import RecorderClient, RecorderError
from hub.state import HubState, StateStore
from hub.xmframe import Unwrapper

log = logging.getLogger("hovercam.hub")


class CommandError(Exception):
    pass


PLAYBACK_BUFFER = 48 * 1024 * 1024   # pause pulling from the recorder beyond this
PLAYBACK_IDLE_S = 90                 # drop a playback nobody is watching
MAX_PLAYBACKS = 3


class Playback:
    """One recording being streamed: recorder -> unwrap -> local HTTP feed -> go2rtc."""

    def __init__(self) -> None:
        self.chunks: deque[bytes] = deque()
        self.buffered = 0
        self.data = asyncio.Event()
        self.space = asyncio.Event()
        self.space.set()
        self.ready = asyncio.Event()
        self.done = False
        self.error: str | None = None
        self.info: dict = {}
        self.readers = 0
        self.created = self.last_active = time.monotonic()
        self.task: asyncio.Task | None = None


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_dir)
        self.state: HubState = self.store.load()
        self._imaging: dict[str, imaging.CameraImaging] = {}  # stream_key -> settings client
        self.session: aiohttp.ClientSession | None = None
        self.go2rtc: Go2RTC | None = None
        self._tasks: set[asyncio.Task] = set()
        self.playbacks: dict[str, Playback] = {}

    # ---- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.go2rtc = Go2RTC(session, self.config.go2rtc_url)
            await self._start_status_page()
            await self._ensure_registered()
            await self._restore_streams()
            await asyncio.gather(self._pairing_watch(), self._socket_forever(),
                                 self._playback_janitor())

    async def _start_status_page(self) -> None:
        runner = web.AppRunner(status_page.make_app(lambda: self.state, self._serve_playback))
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", self.config.status_port).start()
        except OSError as exc:
            # Optional convenience page; never let it stop the hub.
            log.warning("status page disabled (port %s: %s)", self.config.status_port,
                        exc.strerror or exc)
            return
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

    async def _unpaired(self) -> None:
        """The owner removed this hub (or deleted their account): forget every camera
        URL/password and offer a fresh pairing code."""
        log.info("hub was removed from its home; clearing cameras and re-pairing")
        for key in list(self.state.cameras):
            try:
                await self.go2rtc.remove_stream(key)
            except (Go2RTCError, aiohttp.ClientError):
                pass
        self.state.cameras.clear()
        self.state.recorders.clear()
        for key in list(self.playbacks):
            await self._drop_playback(key)
        self.state.paired = False
        self.state.pairing_expires_at = None
        self.store.save(self.state)
        task = asyncio.create_task(self._pairing_watch())  # issues a new code right away
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            # Short, capped backoff: a server deploy takes ~1-3 min and the hub should be
            # back within seconds of it, not minutes.
            delay = backoff + random.uniform(0, 2)
            log.info("reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> None:
        request_id = message.get("id")
        if request_id is None:
            # Unsolicited notice from the cloud.
            if message.get("type") == "status":
                paired = (message.get("data") or {}).get("paired")
                if paired is True:
                    self._mark_paired()
                elif paired is False and self.state.paired:   # only an explicit unpair
                    await self._unpaired()
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
            cameras = await discovery.discover()
            known = {urlparse(u).hostname for u in self.state.cameras.values()}
            for cam in cameras:
                cam["added"] = cam["address"] in known
            return {"cameras": cameras}
        if command == "camera.add_onvif":
            try:
                found = await onvif.OnvifCamera(
                    self.session, data["xaddr"], data.get("username", ""),
                    data.get("password", "")).resolve()
            except onvif.OnvifError as exc:
                raise CommandError(str(exc)) from None
            await self._camera_add(data["stream_key"], found["rtsp_url"])
            p = found["profile"]
            return {"manufacturer": found["manufacturer"], "model": found["model"],
                    "stream": f"{p['name'] or p['token']} {p['encoding'] or ''} "
                              f"{p['width']}x{p['height']}".strip()}
        if command == "camera.add":
            return await self._camera_add(data["stream_key"], data["rtsp_url"])
        if command == "camera.remove":
            key = data["stream_key"]
            await self.go2rtc.remove_stream(key)
            self.state.cameras.pop(key, None)
            self._imaging.pop(key, None)
            self.store.save(self.state)
            return {}
        if command == "webrtc.offer":
            key = data["stream_key"]
            if key not in self.state.cameras and key not in self.playbacks:
                raise CommandError("stream not found on this hub")
            if key in self.playbacks:
                self.playbacks[key].last_active = time.monotonic()
            return {"sdp": await self.go2rtc.webrtc_answer(key, data["sdp"])}
        if command.startswith("recorder.") or command.startswith("playback."):
            try:
                return await self._recorder_command(command, data)
            except RecorderError as exc:
                raise CommandError(str(exc)) from None
        if command in ("camera.image.get", "camera.image.set"):
            return await self._camera_image(command, data)
        if command == "snapshot":
            key = self._known(data["stream_key"])
            jpeg = await self.go2rtc.snapshot(key)
            return {"jpeg_b64": base64.b64encode(jpeg).decode()}
        raise CommandError(f"unknown command {command!r}")

    # ---- recorders (NVR/DVR) ---------------------------------------------------------

    def _recorder(self, recorder_id: str) -> dict:
        rec = self.state.recorders.get(recorder_id)
        if rec is None:
            raise CommandError("recorder not found on this hub")
        return rec

    def _client(self, rec: dict) -> RecorderClient:
        return RecorderClient(rec["host"], rec["username"], rec["password"])

    async def _recorder_command(self, command: str, data: dict) -> dict:
        if command == "recorder.scan":
            hosts = await recorder.scan()
            cams = {urlparse(u).hostname for u in self.state.cameras.values()}
            recs = {r["host"] for r in self.state.recorders.values()}
            return {"hosts": [{"address": h, "camera": h in cams, "added": h in recs}
                              for h in hosts]}
        if command == "recorder.add":
            rec = {"host": data["host"], "username": data["username"],
                   "password": data.get("password", "")}
            async with self._client(rec) as c:
                described = await c.describe()
            self.state.recorders[data["recorder_id"]] = rec
            self.store.save(self.state)
            return described
        if command == "recorder.remove":
            self.state.recorders.pop(data["recorder_id"], None)
            self.store.save(self.state)
            return {}
        if command == "recorder.describe":
            async with self._client(self._recorder(data["recorder_id"])) as c:
                return await c.describe()
        if command == "recorder.files":
            async with self._client(self._recorder(data["recorder_id"])) as c:
                files = await c.files(int(data["channel"]), *recorder.day_bounds(data["day"]))
            return {"files": files}
        if command == "playback.start":
            return await self._start_playback(data)
        if command == "playback.stop":
            await self._drop_playback(data["stream_key"])
            return {}
        raise CommandError(f"unknown command {command!r}")

    async def _start_playback(self, data: dict) -> dict:
        rec = self._recorder(data["recorder_id"])
        while len(self.playbacks) >= MAX_PLAYBACKS:          # keep the PC from overloading
            oldest = min(self.playbacks, key=lambda k: self.playbacks[k].created)
            await self._drop_playback(oldest)
        key = f"pb_{secrets.token_hex(6)}"
        pb = Playback()
        self.playbacks[key] = pb
        pb.task = asyncio.create_task(
            self._pull_recording(pb, rec, data["file"], data["start"], data["end"]))
        try:
            await asyncio.wait_for(pb.ready.wait(), 25)
        except asyncio.TimeoutError:
            pb.error = "the recorder did not send video in time"
        if not pb.info:
            await self._drop_playback(key)
            raise CommandError(pb.error or "no video in that time range")
        fps = int(pb.info.get("fps") or 15)
        # go2rtc 1.9.12+ refuses exec: sources from its API, so go2rtc pulls a plain local
        # HTTP MPEG-TS feed instead; the hub runs ffmpeg itself when go2rtc connects.
        source = f"http://127.0.0.1:{self.config.status_port}/playback/{key}"
        await self.go2rtc.add_stream(key, source)
        started = pb.info.get("started")
        return {"stream_key": key, "codec": pb.info.get("codec"), "fps": fps,
                "started": started.isoformat() if started else None}

    async def _pull_recording(self, pb: Playback, rec: dict, file: str, start: str,
                              end: str) -> None:
        try:
            async with self._client(rec) as c:
                unwrap = Unwrapper()
                async for raw in c.stream(file, start, end):
                    video = unwrap.feed(raw)
                    if video:
                        pb.chunks.append(video)
                        pb.buffered += len(video)
                        pb.data.set()
                    if unwrap.info["key_frames"] and not pb.ready.is_set():
                        pb.info = dict(unwrap.info)
                        pb.ready.set()
                    while pb.buffered > PLAYBACK_BUFFER:     # viewer is slower: wait
                        pb.space.clear()
                        await pb.space.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pb.error = str(exc) or exc.__class__.__name__
            log.warning("playback stopped: %s", pb.error)
        finally:
            pb.done = True
            pb.data.set()
            pb.ready.set()

    async def _serve_playback(self, request: web.Request) -> web.StreamResponse:
        """Local-only MPEG-TS feed for go2rtc: recorder video re-encoded to H.264 by ffmpeg,
        paced in real time (-re)."""
        if request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        pb = self.playbacks.get(request.match_info["key"])
        if pb is None or not pb.info:
            raise web.HTTPNotFound()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            log.warning("playback needs ffmpeg on PATH")
            raise web.HTTPServiceUnavailable()
        fmt = "hevc" if pb.info.get("codec") == "hevc" else "h264"
        fps = int(pb.info.get("fps") or 15)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", fmt,
            "-framerate", str(fps), "-i", "pipe:0", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-vf", "scale=-2:720",
            "-g", str(fps * 2), "-f", "mpegts", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

        async def feed() -> None:
            try:
                while True:
                    while pb.chunks:
                        chunk = pb.chunks.popleft()
                        pb.buffered -= len(chunk)
                        pb.space.set()
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                        pb.last_active = time.monotonic()
                    if pb.done:
                        break
                    pb.data.clear()
                    await asyncio.wait_for(pb.data.wait(), 60)
            except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        resp = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await resp.prepare(request)
        pb.readers += 1
        feeder = asyncio.create_task(feed())
        try:
            while True:
                out = await proc.stdout.read(65536)
                if not out:
                    break
                await resp.write(out)
                pb.last_active = time.monotonic()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            feeder.cancel()
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            pb.readers -= 1
            pb.last_active = time.monotonic()
        return resp

    async def _drop_playback(self, key: str) -> None:
        pb = self.playbacks.pop(key, None)
        if pb is None:
            return
        if pb.task is not None:
            pb.task.cancel()
        try:
            await self.go2rtc.remove_stream(key)
        except (Go2RTCError, aiohttp.ClientError):
            pass

    async def _playback_janitor(self) -> None:
        while True:
            await asyncio.sleep(20)
            now = time.monotonic()
            for key, pb in list(self.playbacks.items()):
                if pb.readers == 0 and now - pb.last_active > PLAYBACK_IDLE_S:
                    log.info("closing idle playback %s", key)
                    await self._drop_playback(key)

    async def _camera_image(self, command: str, data: dict) -> dict:
        """Picture settings (brightness, color, ...) via the camera's ONVIF imaging service.
        The login comes from the camera's RTSP URL, which only this hub stores."""
        key = self._known(data["stream_key"])
        try:
            client = self._imaging.get(key)
            if client is None:
                host, user, password = imaging.login_from_rtsp(self.state.cameras[key])
                xaddr = await imaging.find_xaddr(self.session, host)
                client = await imaging.CameraImaging.connect(self.session, xaddr, user, password)
                self._imaging[key] = client
            if command == "camera.image.set":
                await client.set(data.get("settings") or {})
            return await client.get()
        except imaging.OnvifError as exc:
            self._imaging.pop(key, None)  # rediscover next time (camera moved, login changed)
            raise CommandError(str(exc)) from None

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
