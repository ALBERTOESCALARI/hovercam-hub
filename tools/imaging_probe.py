"""Read a camera's picture settings over ONVIF (read-only; changes nothing).

    .venv/bin/python tools/imaging_probe.py 192.168.0.108 admin
(asks for the camera password)
"""

import asyncio
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from hub import imaging  # noqa: E402


async def main(host: str, user: str, password: str) -> None:
    async with aiohttp.ClientSession() as session:
        xaddr = await imaging.find_xaddr(session, host)
        print("ONVIF service:", xaddr)
        cam = await imaging.CameraImaging.connect(session, xaddr, user, password)
        print("Imaging service:", cam.url, "| video source:", cam.source)
        print(json.dumps(await cam.get(), indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    host = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    try:
        asyncio.run(main(host, user, getpass.getpass(f"Password for {user}@{host}: ")))
    except imaging.OnvifError as exc:
        sys.exit(f"Failed: {exc}")
