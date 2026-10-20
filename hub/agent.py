"""HoverCam hub agent.

1. First boot: registers with the cloud, saves hub id/secret, shows a pairing code.
2. Re-adds every saved camera to go2rtc (go2rtc forgets API-added streams on restart).
3. Holds a WebSocket to the cloud and answers commands:
   webrtc.offer, snapshot, discover, camera.add, camera.remove, ping.
"""

import asyncio
import base64
import hashlib
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

from hub import (
    __version__, alarms, discovery, imaging, nvr_settings, onvif, recorder, rewind, status_page,
    updater,
)
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
        self.speed = 1   # 2-4 fast forward, negative = rewind (see hub/rewind.py)


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
        self.commit: str | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.alarms = alarms.AlarmWatcher(self._emit_event)
        # Camera logins for overlay changes (memory only): ip -> [(user, password), ...] to
        # try, the one that worked, and failures not to retry for a while (a camera locks
        # its account after a few wrong passwords).
        self._cam_logins: dict[str, list[tuple[str, str]]] = {}
        self._cam_ok: dict[str, tuple[str, str]] = {}
        self._cam_failed: dict[tuple[str, str, str], float] = {}

    # ---- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.go2rtc = Go2RTC(session, self.config.go2rtc_url)
            await self._start_status_page()
            await self._ensure_registered()
            await self._restore_streams()
            if self.state.paired:
                for rid, rec in self.state.recorders.items():
                    self.alarms.start(rid, rec)
            self.commit = await updater.current_commit()
            if self.commit:
                log.info("running version %s (%s)", __version__, self.commit)
            await asyncio.gather(self._pairing_watch(), self._socket_forever(),
                                 self._playback_janitor(), self._clock_keeper(),
                                 updater.watch(busy=lambda: bool(self.playbacks)))

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
        self.alarms.stop_all()
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
                    self.ws = ws
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

    def _emit_event(self, event: dict) -> None:
        """Send a recorder event (motion, video loss, ...) to the cloud, if connected."""
        ws = self.ws
        if ws is None or ws.closed:
            log.info("event dropped (not connected to cloud): %s", event.get("kind"))
            return
        task = asyncio.create_task(ws.send_json({"type": "event", "data": event}))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            return {"agent_version": __version__, "commit": self.commit}
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
            self.alarms.start(data["recorder_id"], rec)
            return described
        if command == "recorder.remove":
            self.alarms.stop(data["recorder_id"])
            self.state.recorders.pop(data["recorder_id"], None)
            self.store.save(self.state)
            return {}
        if command == "recorder.describe":
            async with self._client(self._recorder(data["recorder_id"])) as c:
                return await c.describe()
        if command == "recorder.settings.get":
            async with self._client(self._recorder(data["recorder_id"])) as c:
                settings = await nvr_settings.read(c, self._camera_ips())
                await self._learn_camera_logins(c)
            await self._camera_overlays(settings)
            return settings
        if command == "recorder.settings.set":
            change = data.get("change") or {}
            async with self._client(self._recorder(data["recorder_id"])) as c:
                result = await nvr_settings.apply(c, change)
                if not change.get("reboot"):
                    result["settings"] = await nvr_settings.read(c, self._camera_ips())
                    await self._learn_camera_logins(c)
            if set(nvr_settings.OVERLAY_KEYS) & change.keys() and "settings" in result:
                result["camera"] = await self._overlay_to_camera(result["settings"], change)
            if change.get("day_night") and "settings" in result:
                result["camera"] = await self._day_night_to_cameras(result["settings"], change)
            if "settings" in result:
                await self._camera_overlays(result["settings"])
            return result
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

    async def _learn_camera_logins(self, c: RecorderClient) -> None:
        """Candidate logins per camera IP: the recorder's own (works for sure) first, then
        the one typed when the camera was added to live view."""
        nvr = await nvr_settings.camera_logins(c)
        rtsp = {}
        for url in self.state.cameras.values():
            host, user, password = imaging.login_from_rtsp(url)
            if host:
                rtsp[host] = (user or "admin", password)
        for ip in set(nvr) | set(rtsp):
            self._cam_logins[ip] = list(dict.fromkeys(
                [x for x in (self._cam_ok.get(ip), nvr.get(ip), rtsp.get(ip)) if x]))

    async def _with_camera(self, ip: str | None, fn):
        """Run fn(client) on the camera at ip with the first login that works. Never
        retries a login that failed in the last 6 hours, so cameras don't get locked."""
        if not ip or not self._cam_logins.get(ip):
            raise RecorderError("no login known for this camera")
        locked_out = False
        for user, password in self._cam_logins[ip]:
            key = (ip, user, hashlib.sha256(password.encode()).hexdigest())
            if time.monotonic() - self._cam_failed.get(key, -1e9) < 6 * 3600:
                continue
            try:
                async with RecorderClient(ip, user, password, timeout=5) as cam:
                    result = await fn(cam)
                self._cam_ok[ip] = (user, password)
                return result
            except RecorderError as exc:
                if "password" not in str(exc) and "locked" not in str(exc):
                    raise
                self._cam_failed[key] = time.monotonic()
                locked_out = locked_out or "locked" in str(exc)
        raise RecorderError("the camera locked its login for ~30 minutes; try again later"
                            if locked_out else "the camera didn't accept any login HoverCam knows")

    async def _camera_overlays(self, settings: dict) -> None:
        """Show what each camera really draws on its video (the camera decides, not the
        recorder); cameras HoverCam can't log in to keep the recorder's value."""
        async def one(chan: dict) -> None:
            try:
                got = await self._with_camera(chan.get("ip"), nvr_settings.read_camera_overlay)
            except (RecorderError, OSError, asyncio.TimeoutError):
                return
            chan.update({k: v for k, v in got.items() if v is not None})
            chan["overlay_from_camera"] = True

        await asyncio.gather(*(one(ch) for ch in settings.get("channels") or []))

    async def _day_night_to_cameras(self, settings: dict, change: dict) -> str:
        """Auto / color / black & white on one camera (channel given) or all of them.
        Returns "ok" or which cameras refused and why."""
        mode = str(change["day_night"]).upper()
        chans = settings.get("channels") or []
        ch = change.get("channel")
        targets = [c for c in chans if ch is None or c.get("channel") == ch]

        async def one(chan: dict) -> str | None:
            try:
                await self._with_camera(chan.get("ip"),
                                        lambda cam: nvr_settings.set_day_night(cam, mode))
                return None
            except (RecorderError, OSError, asyncio.TimeoutError) as exc:
                log.info("day/night on %s failed: %s", chan.get("ip"), exc)
                return f"{chan.get('title') or chan.get('ip')}: {exc or 'no answer'}"

        failed = [f for f in await asyncio.gather(*(one(c) for c in targets)) if f]
        return "ok" if not failed else "; ".join(failed)

    async def _overlay_to_camera(self, settings: dict, change: dict) -> str:
        """Send name/clock overlay changes to the camera itself (it draws them). Returns
        "ok", "no_login" or the reason the camera refused."""
        chans = settings.get("channels") or []
        ch = change.get("channel")
        ip = chans[ch].get("ip") if isinstance(ch, int) and 0 <= ch < len(chans) else None
        if not ip or not self._cam_logins.get(ip):
            return "no_login"
        try:
            await self._with_camera(ip, lambda cam: nvr_settings.apply_to_camera(cam, change))
            return "ok"
        except (RecorderError, OSError, asyncio.TimeoutError) as exc:
            log.info("camera %s overlay change failed: %s", ip, exc)
            return str(exc) or "the camera didn't answer"

    def _camera_ips(self) -> dict[str, str]:
        return {key: urlparse(url).hostname or "" for key, url in self.state.cameras.items()}

    async def _clock_keeper(self) -> None:
        """Keep each recorder's clock right (recordings and alerts are stamped with it):
        every 6 hours, set it to this PC's time if it is more than 90 seconds off. Also
        refreshes the camera logins the recorder knows (for picture/overlay settings)."""
        await asyncio.sleep(20)
        while True:
            for rid, rec in list(self.state.recorders.items()):
                try:
                    async with self._client(rec) as c:
                        await self._learn_camera_logins(c)
                        nvr = await c.get_time()
                        off = (nvr - datetime.now()).total_seconds() if nvr else 0
                        if abs(off) > 90:
                            await c.set_time(datetime.now())
                            log.info("set recorder %s clock (was %+.0fs off)", rec["host"], off)
                except Exception as exc:  # noqa: BLE001 - never let this stop the hub
                    log.info("recorder %s clock check skipped: %s", rec.get("host"), exc)
            await asyncio.sleep(6 * 3600)

    async def _start_playback(self, data: dict) -> dict:
        rec = self._recorder(data["recorder_id"])
        while len(self.playbacks) >= MAX_PLAYBACKS:          # keep the PC from overloading
            oldest = min(self.playbacks, key=lambda k: self.playbacks[k].created)
            await self._drop_playback(oldest)
        key = f"pb_{secrets.token_hex(6)}"
        pb = Playback()
        pb.speed = rewind.clean_speed(data.get("speed", 1))
        self.playbacks[key] = pb
        log.info("playback %s: %s %s..%s at %sx", key, data["file"], data["start"],
                 data["end"], pb.speed)
        if pb.speed < 0:   # rewinds from "end" back to "start"
            pb.task = asyncio.create_task(
                self._pull_rewind(pb, rec, data["file"], data["start"], data["end"],
                                  data.get("channel")))
        else:
            pb.task = asyncio.create_task(
                self._pull_recording(pb, rec, data["file"], data["start"], data["end"],
                                     data.get("channel")))
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
                "speed": pb.speed, "started": started.isoformat() if started else None}

    async def _pieces(self, rec: dict, file: str, channel, start: str,
                      end: str) -> list[tuple[str, str, str]]:
        """The recorder's files covering start..end, as (file, from, to), oldest first.
        Recorders split a continuous recording into a new file at every alarm, so one
        stretch of video can be dozens of files; playback runs through all of them."""
        pieces: list[tuple[str, str, str]] = []
        if isinstance(channel, int):
            try:
                async with self._client(rec) as c:
                    files = await c.files(channel, start, end)
                for f in sorted(files, key=lambda f: f["begin"]):
                    a, b = max(start, f["begin"]), min(end, f["end"])
                    if pieces:
                        a = max(a, pieces[-1][2])   # overlapping files: don't play twice
                    if a < b:
                        pieces.append((f["name"], a, b))
            except RecorderError as exc:
                log.info("file list for playback failed (%s); playing one file", exc)
        return pieces or [(file, start, end)]

    async def _pull_rewind(self, pb: Playback, rec: dict, file: str, floor: str,
                           start: str, channel=None) -> None:
        """Rewind: key frames of 30 s windows, newest first (see hub/rewind.py)."""
        fmt = recorder.FMT
        try:
            lo, hi = datetime.strptime(floor, fmt), datetime.strptime(start, fmt)
            pieces = await self._pieces(rec, file, channel, floor, start)
            for t0, t1 in rewind.windows(lo, hi):
                w0, w1 = t0.strftime(fmt), t1.strftime(fmt)
                unwrap = Unwrapper()
                parts: list[bytes] = []
                for name, a, b in pieces:
                    a, b = max(a, w0), min(b, w1)
                    if a >= b:
                        continue
                    async with self._client(rec) as c:
                        async for raw in c.stream(name, a, b):
                            video = unwrap.feed(raw)
                            if video:
                                parts.append(video)
                frames = await rewind.decode_key_frames(b"".join(parts),
                                                        unwrap.info.get("codec") or "h264")
                if not frames:
                    continue
                hold = rewind.hold_frames((t1 - t0).total_seconds() / len(frames), pb.speed)
                if not pb.ready.is_set():
                    pb.info = {"codec": "rewind", "fps": rewind.REWIND_FPS, "started": hi,
                               "key_frames": len(frames)}
                    pb.ready.set()
                for frame in reversed(frames):
                    for _ in range(hold):
                        pb.chunks.append(frame)
                        pb.buffered += len(frame)
                    pb.data.set()
                    while pb.buffered > PLAYBACK_BUFFER:     # viewer is slower: wait
                        pb.space.clear()
                        await pb.space.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pb.error = str(exc) or exc.__class__.__name__
            log.warning("rewind stopped: %s", pb.error)
        finally:
            pb.done = True
            pb.data.set()
            pb.ready.set()

    async def _pull_recording(self, pb: Playback, rec: dict, file: str, start: str,
                              end: str, channel=None) -> None:
        try:
            unwrap = Unwrapper()
            for name, a, b in await self._pieces(rec, file, channel, start, end):
                async with self._client(rec) as c:
                    async for raw in c.stream(name, a, b):
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
        else:
            log.info("recording download finished (%d key frames, %d frames)",
                     unwrap.info["key_frames"], unwrap.info["frames"])
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
        decoder = None
        if pb.speed < 0:     # raw 640x360 key frames from _pull_rewind
            inputs, filters, out_fps = rewind.raw_input_args(rewind.REWIND_FPS), [], rewind.REWIND_FPS
        elif pb.speed > 1:   # decoder squeezes time, encoder plays its frames at a steady rate
            inputs, filters, out_fps = rewind.raw_input_args(rewind.FAST_FPS), [], rewind.FAST_FPS
            decoder = await asyncio.create_subprocess_exec(
                *rewind.fast_decoder_args(ffmpeg, fmt, fps, pb.speed),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        else:
            inputs = ["-re", *rewind.HWACCEL, "-f", fmt, "-framerate", str(fps), "-i", "pipe:0"]
            filters, out_fps = ["-vf", "scale=-2:720:flags=fast_bilinear"], fps
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", *inputs, "-an",
            # ultrafast + capped bitrate: the PC must keep up in real time or the picture
            # freezes; recorder video decodes on the graphics chip when there is one.
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-crf", "26", "-maxrate", "2500k", "-bufsize", "5000k",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", *filters,
            "-g", str(out_fps * 2), "-f", "mpegts", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        sink = decoder.stdin if decoder is not None else proc.stdin

        async def pump() -> None:   # fast forward: decoder frames -> encoder
            try:
                while True:
                    frames = await decoder.stdout.read(rewind.FRAME_BYTES)
                    if not frames:
                        break
                    proc.stdin.write(frames)
                    await proc.stdin.drain()
            except (ConnectionError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def feed() -> None:
            try:
                while True:
                    while pb.chunks:
                        chunk = pb.chunks.popleft()
                        pb.buffered -= len(chunk)
                        pb.space.set()
                        sink.write(chunk)
                        await sink.drain()
                        pb.last_active = time.monotonic()
                    if pb.done:
                        break
                    pb.data.clear()
                    await asyncio.wait_for(pb.data.wait(), 60)
            except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    sink.close()
                except Exception:
                    pass

        resp = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await resp.prepare(request)
        pb.readers += 1
        feeder = asyncio.create_task(feed())
        pumper = asyncio.create_task(pump()) if decoder is not None else None
        sent = 0
        errors: list[str] = []

        async def collect(p, name: str) -> None:   # ffmpeg complaints, for the log
            try:
                text = (await p.stderr.read()).decode("utf-8", "replace").strip()
                if text:
                    errors.append(f"{name}: {text[-400:]}")
            except Exception:  # noqa: BLE001
                pass

        collectors = [asyncio.create_task(collect(proc, "encoder"))]
        if decoder is not None:
            collectors.append(asyncio.create_task(collect(decoder, "decoder")))
        try:
            while True:
                out = await proc.stdout.read(65536)
                if not out:
                    break
                await resp.write(out)
                sent += len(out)
                pb.last_active = time.monotonic()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            feeder.cancel()
            if pumper is not None:
                pumper.cancel()
            for p in (decoder, proc):
                if p is not None and p.returncode is None:
                    p.kill()
                if p is not None:
                    await p.wait()
            await asyncio.gather(*collectors, return_exceptions=True)
            key = request.match_info["key"]
            if pb.readers <= 1 and (sent < 10000 or errors or pb.error):
                log.warning("playback %s sent %d bytes (done=%s, error=%s) %s", key, sent,
                            pb.done, pb.error, " | ".join(errors))
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
                # The login typed when the camera was added first, then the one the
                # recorder uses for it (often the only one the camera really accepts).
                logins = list(dict.fromkeys([(user, password), *self._cam_logins.get(host, [])]))
                last: Exception | None = None
                for u, p in logins:
                    try:
                        client = await imaging.CameraImaging.connect(self.session, xaddr, u, p)
                        break
                    except imaging.OnvifError as exc:
                        last = exc
                        if "password" not in str(exc):
                            raise
                if client is None:
                    log.info("camera %s picture settings: %s", host, last)
                    raise last or imaging.OnvifError("wrong camera username or password")
                self._imaging[key] = client
            settings = data.get("settings") or {}
            day_night = settings.get("ir_cut") if command == "camera.image.set" else None
            if command == "camera.image.set":
                try:
                    await client.set(settings)
                except imaging.OnvifError:
                    if set(settings) != {"ir_cut"}:   # only day/night has a second way
                        raise
            result = await client.get()
            if day_night:
                # Xiongmai cameras often accept ONVIF's day/night but ignore it; set it
                # their own way too (over DVRIP, with the login the recorder uses).
                host = urlparse(self.state.cameras[key]).hostname
                try:
                    await self._with_camera(
                        host, lambda cam: nvr_settings.set_day_night(cam, day_night))
                    result.setdefault("settings", {})["ir_cut"] = str(day_night).upper()
                except (RecorderError, OSError, asyncio.TimeoutError) as exc:
                    log.info("day/night on %s via DVRIP failed: %s", host, exc)
            return result
        except imaging.OnvifError as exc:
            self._imaging.pop(key, None)  # rediscover next time (camera moved, login changed)
            log.info("picture settings for %s failed: %s", key, exc)
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
