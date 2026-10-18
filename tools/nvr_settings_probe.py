"""See which settings your recorder lets HoverCam read (read-only, changes nothing).

    .venv\\Scripts\\python.exe tools\\nvr_settings_probe.py 192.168.0.103 albertoescalari
(asks for the recorder password). Prints a short summary and saves everything to
nvr_settings.json next to this folder. Passwords in the recorder's answers are blanked.
"""

import asyncio
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub.recorder import RecorderClient, RecorderError  # noqa: E402

INFO = [  # (name, message code)
    ("SystemInfo", 1020), ("StorageInfo", 1020), ("WorkState", 1020),
    ("ChannelTitle", 1048), ("SystemFunction", 1360), ("EncodeCapability", 1360),
]
CONFIGS = [  # read with ConfigGet (1042)
    "General.General", "General.Location", "AVEnc.VideoWidget", "Detect.MotionDetect",
    "Detect.BlindDetect", "Detect.LossDetect", "Detect.HumanDetection", "Record",
    "Simplify.Encode", "ExtRecord", "Storage.StorageGlobal", "NetWork.RemoteDeviceV3",
    "NetWork.NetCommon", "Camera.Param", "General.AutoMaintain",
]


def redact(v):
    if isinstance(v, dict):
        return {k: ("***" if "pass" in k.lower() or "pwd" in k.lower() else redact(x))
                for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


async def main(host: str, user: str, password: str) -> None:
    out: dict = {}
    async with RecorderClient(host, user, password) as c:
        sid = "0x%08X" % c.session
        for name, code in [(n, k) for n, k in INFO] + [(n, 1042) for n in CONFIGS]:
            try:
                r = await c._call(code, {"Name": name, "SessionID": sid})
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": str(exc)}
                print(f"{name:26} error {exc}")
                continue
            ok = r.get("Ret") in (100, 515) and name in r
            out[name] = redact(r.get(name)) if ok else {"ret": r.get("Ret")}
            preview = json.dumps(out[name])[:110] if ok else f"not available (Ret {r.get('Ret')})"
            print(f"{name:26} {preview}")
        try:
            r = await c._call(1452, {"Name": "OPTimeQuery", "SessionID": sid})
            out["OPTimeQuery"] = r.get("OPTimeQuery")
            print(f"{'Time':26} {r.get('OPTimeQuery')}")
        except Exception as exc:  # noqa: BLE001
            print(f"{'Time':26} error {exc}")
    path = ROOT / "nvr_settings.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"\nSaved {path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2], getpass.getpass("Recorder password: ")))
    except RecorderError as exc:
        sys.exit(f"Failed: {exc}")
