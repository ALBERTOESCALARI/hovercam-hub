"""Watch a recorder's alarms live (read-only). Walk in front of a camera to see motion.

    .venv/bin/python tools/nvr_alarms.py 192.168.0.103 albertoescalari
(asks for the recorder password; Ctrl+C to stop)
"""

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.alarms import normalize  # noqa: E402
from hub.recorder import RecorderClient, RecorderError  # noqa: E402


async def main(host: str, user: str, password: str) -> None:
    async with RecorderClient(host, user, password) as c:
        names = (await c.describe())["channels"]
        print(f"Listening on {host} (keep-alive every {c.alive}s). Move in front of a camera...")
        async for info in c.alarms():
            ev = normalize(info)
            cam = names[ev["channel"]] if ev and ev["channel"] < len(names) else "?"
            print(info, "->", f"{ev['kind']} on {cam}" if ev else "(end of alarm)", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2], getpass.getpass("Recorder password: ")))
    except RecorderError as exc:
        sys.exit(f"Failed: {exc}")
    except KeyboardInterrupt:
        pass
