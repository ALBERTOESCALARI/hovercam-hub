import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    api_url: str          # https://api.hovercam.hoverlayer.com
    go2rtc_url: str       # http://127.0.0.1:1984
    state_dir: Path       # where hub id/secret and camera URLs are kept
    hub_name: str
    status_port: int      # tiny local page that shows the pairing code

    @property
    def ws_url(self) -> str:
        base = self.api_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base.removeprefix("https://")
        elif base.startswith("http://"):
            base = "ws://" + base.removeprefix("http://")
        return base + "/ws/hub"


def load() -> Config:
    return Config(
        api_url=os.environ.get("HOVERCAM_API_URL", "http://127.0.0.1:8000").rstrip("/"),
        go2rtc_url=os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/"),
        state_dir=Path(os.environ.get("HOVERCAM_STATE_DIR", "./state")),
        hub_name=os.environ.get("HOVERCAM_HUB_NAME", "HoverCam Hub"),
        status_port=int(os.environ.get("HOVERCAM_STATUS_PORT", "8099")),
    )
