"""Read a camera's own day/night and light settings (read-only, changes nothing).

    .venv\\Scripts\\python.exe tools\\camera_probe.py 192.168.0.109

Uses the logins the hub already has (the recorder's login for that camera, or the one typed
when the camera was added), so it asks for nothing. Passwords are never printed.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import imaging, nvr_settings  # noqa: E402
from hub.recorder import RecorderClient, RecorderError  # noqa: E402

NAMES = ["SystemInfo", "Camera.Param", "Camera.ParamEx", "Camera.WhiteLight",
         "Camera.DayLightTime", "Camera.NightLight", "Camera.ClearFog", "AVEnc.VideoWidget"]


def redact(v):
    if isinstance(v, dict):
        return {k: ("***" if "pass" in k.lower() or "pwd" in k.lower() else redact(x))
                for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


async def logins_for(ip: str) -> list[tuple[str, str]]:
    state_dir = Path(os.environ.get("HOVERCAM_STATE_DIR", ROOT / "state"))
    state = json.loads((state_dir / "hub_state.json").read_text())
    found: list[tuple[str, str]] = []
    for rec in (state.get("recorders") or {}).values():
        try:
            async with RecorderClient(rec["host"], rec["username"], rec.get("password", "")) as c:
                login = (await nvr_settings.camera_logins(c)).get(ip)
                if login:
                    found.append(login)
        except RecorderError as exc:
            print(f"(recorder {rec['host']}: {exc})")
    for url in (state.get("cameras") or {}).values():
        if urlparse(url).hostname == ip:
            host, user, password = imaging.login_from_rtsp(url)
            found.append((user or "admin", password))
    return list(dict.fromkeys(found))


async def main(ip: str) -> None:
    logins = await logins_for(ip)
    if not logins:
        sys.exit(f"No login known for {ip}.")
    for user, password in logins:
        try:
            async with RecorderClient(ip, user, password, timeout=6) as cam:
                print(f"Logged in to {ip} as {user}\n")
                out = {}
                for name in NAMES:
                    try:
                        out[name] = redact(await cam.config_get(name, 1020 if name ==
                                                                "SystemInfo" else 1042))
                        print(f"{name:22} {json.dumps(out[name])[:400]}")
                    except RecorderError as exc:
                        print(f"{name:22} not available ({exc})")
                (ROOT / f"camera_{ip}.json").write_text(json.dumps(out, indent=1))
                print(f"\nSaved camera_{ip}.json")
                return
        except RecorderError as exc:
            print(f"login as {user} refused: {exc}")
            if "locked" in str(exc):
                return
    sys.exit("The camera refused every login HoverCam knows.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
