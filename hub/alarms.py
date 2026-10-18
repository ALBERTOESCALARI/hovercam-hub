"""Turns recorder alarms into HoverCam events.

One long-lived alarm connection per recorder; reconnects with backoff. Repeated wrong
logins would make the recorder lock the account, so a login failure waits much longer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from hub.recorder import RecorderClient, RecorderError

log = logging.getLogger("hovercam.alarms")

KINDS = {
    "videomotion": "motion", "motiondetect": "motion", "smartmotion": "motion",
    "humandetect": "person", "humandection": "person", "persondetect": "person",
    "cardetect": "vehicle", "vehicledetect": "vehicle",
    "videoloss": "video_loss", "videoblind": "tamper", "localalarm": "alarm_input",
}
# Minimum seconds between two events of the same kind on the same channel.
QUIET = {"motion": 120, "person": 120, "vehicle": 120, "video_loss": 600, "tamper": 600}


def normalize(info: dict) -> dict | None:
    """AlarmInfo -> {"channel", "kind", "event", "time"}; None for 'Stop' or junk."""
    if str(info.get("Status", "Start")).lower() != "start":
        return None
    raw = str(info.get("Event") or "").strip()
    if not raw:
        return None
    try:
        channel = int(info.get("Channel", 0))
    except (TypeError, ValueError):
        return None
    return {"channel": channel, "kind": KINDS.get(raw.lower().replace("_", ""), raw.lower()),
            "event": raw, "time": str(info.get("StartTime") or "")}


class Debouncer:
    def __init__(self) -> None:
        self._last: dict[tuple, float] = {}

    def allow(self, key: tuple, kind: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        quiet = QUIET.get(kind, 120)
        if now - self._last.get(key, -1e9) < quiet:
            return False
        self._last[key] = now
        return True


class AlarmWatcher:
    def __init__(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit
        self._tasks: dict[str, asyncio.Task] = {}
        self._debounce = Debouncer()

    def start(self, recorder_id: str, rec: dict) -> None:
        self.stop(recorder_id)
        self._tasks[recorder_id] = asyncio.create_task(self._run(recorder_id, dict(rec)))

    def stop(self, recorder_id: str) -> None:
        task = self._tasks.pop(recorder_id, None)
        if task is not None:
            task.cancel()

    def stop_all(self) -> None:
        for rid in list(self._tasks):
            self.stop(rid)

    async def _run(self, recorder_id: str, rec: dict) -> None:
        backoff = 5.0
        while True:
            client = RecorderClient(rec["host"], rec["username"], rec.get("password", ""))
            try:
                await client.connect()
                log.info("listening for alarms on recorder %s", rec["host"])
                backoff = 5.0
                async for info in client.alarms():
                    event = normalize(info)
                    if event is None:
                        continue
                    if not self._debounce.allow((recorder_id, event["channel"], event["kind"]),
                                                event["kind"]):
                        continue
                    event.update(recorder_id=recorder_id,
                                 hub_time=datetime.now(timezone.utc).isoformat())
                    log.info("alarm: %s on channel %s", event["kind"], event["channel"] + 1)
                    self._emit(event)
                log.info("alarm connection to %s ended; reconnecting", rec["host"])
            except asyncio.CancelledError:
                raise
            except RecorderError as exc:
                text = str(exc)
                wait = 900 if ("password" in text or "locked" in text) else backoff
                log.warning("recorder %s alarms: %s (retry in %ds)", rec["host"], text, wait)
                await client.close()
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 120)
                continue
            except Exception as exc:  # network blips, recorder reboot
                log.warning("recorder %s alarms: %s", rec["host"], exc)
            await client.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
