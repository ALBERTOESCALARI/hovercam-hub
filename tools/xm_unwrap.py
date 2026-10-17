"""Turn a recorder download (Xiongmai framing) into a playable MP4.
Usage:  python3 tools/xm_unwrap.py ~/Desktop/hovercam_clip.h264"""
import os, shutil, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hub.xmframe import unwrap  # noqa: E402

src = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/Desktop/hovercam_clip.h264")
raw = open(src, "rb").read()
video, info = unwrap(raw)
print(f"Input {len(raw) / 1048576:.1f} MB -> video {len(video) / 1048576:.1f} MB")
print(f"Codec {info['codec']}  {info['fps']} fps  "
      f"key frames {info['key_frames']}  frames {info['frames']}  starts {info['started']}")
ext = "h265" if info["codec"] == "hevc" else "h264"
clean = os.path.splitext(src)[0] + f"_clean.{ext}"
open(clean, "wb").write(video)
mp4 = os.path.splitext(src)[0] + "_clean.mp4"
if shutil.which("ffmpeg"):
    tag = ["-tag:v", "hvc1"] if info["codec"] == "hevc" else []
    fmt = ["-f", "hevc"] if info["codec"] == "hevc" else ["-f", "h264"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", *fmt, "-framerate", str(info["fps"] or 15),
                    "-i", clean, "-c", "copy", *tag, "-movflags", "+faststart", mp4])
    if os.path.exists(mp4) and os.path.getsize(mp4):
        print("Playable:", mp4)
        if sys.platform == "darwin":
            subprocess.run(["open", mp4])
