"""Hub state on local disk. This is the ONLY place camera RTSP URLs (with passwords)
are stored. The file is written with 0600 permissions."""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class HubState:
    hub_id: str | None = None
    hub_secret: str | None = None
    paired: bool = False
    pairing_code: str | None = None
    pairing_expires_at: str | None = None
    cameras: dict[str, str] = field(default_factory=dict)  # stream_key -> rtsp_url


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "hub_state.json"
        directory.mkdir(parents=True, exist_ok=True)

    def load(self) -> HubState:
        if not self.path.exists():
            return HubState()
        return HubState(**json.loads(self.path.read_text()))

    def save(self, state: HubState) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=2)
        os.replace(tmp, self.path)
