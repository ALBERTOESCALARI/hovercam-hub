"""Fast forward and rewind for recordings.

Fast forward (2-4x): the recorder hands over recordings as fast as we read them. A first
ffmpeg decodes as fast as it can, squeezes time N-fold (setpts) and keeps 12 frames a second
at 640x360; the encoder then reads those raw frames at a steady 12 fps (-re). So the stream
is lighter than normal playback and doesn't stall. (Pacing on raw frames works with every
ffmpeg version; the recording's own timing info can't be trusted for -re at N x.)

Rewind (1-4x): video can't be decoded backwards, so the hub fetches the recording in
30-second windows going back in time, decodes only the key frames (one every ~2 s) at
640x360, and plays them newest-first, each held long enough to give the chosen speed.
The next window is fetched while the current one plays.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta

WIDTH, HEIGHT = 640, 360
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2           # yuv420p
REWIND_FPS = 6                                  # output frame rate while rewinding
WINDOW_S = 30
SPEEDS = (-4, -3, -2, -1, 1, 2, 3, 4)


def clean_speed(value) -> int:
    try:
        speed = int(value)
    except (TypeError, ValueError):
        return 1
    return speed if speed in SPEEDS else 1


FAST_FPS = 12                                   # output frame rate while fast forwarding
_FIT = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2")


def fast_decoder_args(ffmpeg: str, codec: str, fps: int, speed: int) -> list[str]:
    """First stage of fast forward: raw recording in, 640x360 frames at FAST_FPS out."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "hevc" if codec == "hevc" else "h264", "-framerate", str(fps), "-i", "pipe:0",
            "-an", "-vf", f"setpts=PTS/{speed},fps={FAST_FPS},{_FIT}",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]


def raw_input_args(rate: int) -> list[str]:
    """Encoder input for 640x360 raw frames played at `rate` frames per second."""
    return ["-re", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{WIDTH}x{HEIGHT}",
            "-framerate", str(rate), "-i", "pipe:0"]


def windows(floor: datetime, start: datetime, size_s: int = WINDOW_S):
    """(t0, t1) windows going back from start to floor, newest first."""
    t1 = start
    while t1 > floor:
        t0 = max(floor, t1 - timedelta(seconds=size_s))
        yield t0, t1
        t1 = t0


def hold_frames(seconds_per_key: float, speed: int) -> int:
    """How many output frames each key frame stays on screen to rewind at `speed`."""
    return max(1, round(REWIND_FPS * seconds_per_key / max(1, abs(speed))))


def split_frames(raw: bytes) -> list[bytes]:
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES)]


_SYNC_ARGS: list[list[str]] = [["-fps_mode", "passthrough"], ["-vsync", "0"]]


async def decode_key_frames(video: bytes, codec: str) -> list[bytes]:
    """Key frames of a raw H.264/H.265 stream as 640x360 yuv420p frames, oldest first."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or not video:
        return []
    for sync in list(_SYNC_ARGS):   # -fps_mode on ffmpeg 5.1+, -vsync on older builds
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-skip_frame", "nokey",
            "-f", "hevc" if codec == "hevc" else "h264", "-i", "pipe:0", "-an",
            "-vf", _FIT,
            *sync, "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(video)
        if out or b"Unrecognized option" not in err:
            if sync is not _SYNC_ARGS[0]:
                _SYNC_ARGS.remove(sync)
                _SYNC_ARGS.insert(0, sync)   # remember what this ffmpeg understands
            return split_frames(out)
    return []
