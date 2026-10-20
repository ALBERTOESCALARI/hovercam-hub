import asyncio
import shutil
import subprocess
from datetime import datetime

import pytest

from hub import rewind


def test_speeds_and_fast_forward_args():
    assert rewind.clean_speed("4") == 4 and rewind.clean_speed(-2) == -2
    assert rewind.clean_speed(9) == 1 and rewind.clean_speed(None) == 1
    args = rewind.fast_decoder_args("ffmpeg", "hevc", 20, 4)
    assert "setpts=PTS/4,fps=12" in args[args.index("-vf") + 1] and args[args.index("-f") + 1] == "hevc"


def test_windows_go_back_in_time_and_stop_at_the_floor():
    lo, hi = datetime(2026, 10, 18, 12, 0, 0), datetime(2026, 10, 18, 12, 1, 10)
    w = list(rewind.windows(lo, hi))
    assert w[0] == (datetime(2026, 10, 18, 12, 0, 40), hi)
    assert w[-1][0] == lo and len(w) == 3


def test_hold_gives_the_requested_speed():
    # key frame every 2 s of video; 6 output fps
    assert rewind.hold_frames(2, -1) == 12     # 2 s shown per 2 s of video = 1x
    assert rewind.hold_frames(2, -4) == 3      # 0.5 s per 2 s = 4x
    assert rewind.hold_frames(0.1, -4) == 1


def test_split_frames():
    raw = b"x" * (rewind.FRAME_BYTES * 2 + 10)
    assert len(rewind.split_frames(raw)) == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_decode_key_frames_from_a_real_stream():
    video = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "4", "-c:v", "libx264", "-g", "10", "-f", "h264", "pipe:1"],
        capture_output=True, check=True).stdout
    frames = asyncio.run(rewind.decode_key_frames(video, "h264"))
    assert len(frames) == 4 and all(len(f) == rewind.FRAME_BYTES for f in frames)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_fast_forward_pipeline_plays_four_times_faster():
    import time
    video = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "8", "-c:v", "libx264", "-g", "10", "-f", "h264", "pipe:1"],
        capture_output=True, check=True).stdout
    raw = subprocess.run(rewind.fast_decoder_args("ffmpeg", "h264", 10, 4), input=video,
                         capture_output=True, check=True).stdout
    frames = len(raw) // rewind.FRAME_BYTES
    assert abs(frames - 2 * rewind.FAST_FPS) <= 2           # 8 s at 4x = 2 s of output
    t = time.monotonic()
    ts = subprocess.run(["ffmpeg", "-loglevel", "error", *rewind.raw_input_args(rewind.FAST_FPS),
                         "-c:v", "libx264", "-f", "mpegts", "pipe:1"], input=raw,
                        capture_output=True, check=True).stdout
    assert ts and 1.5 < time.monotonic() - t < 4             # paced in real time


def test_playback_runs_through_every_file_in_the_range():
    import types

    from hub.agent import Agent

    files = [  # a continuous hour the recorder split at two alarms (plus one overlap)
        {"name": "/a.h264", "begin": "2026-10-20 12:00:00", "end": "2026-10-20 12:20:00"},
        {"name": "/b.h264", "begin": "2026-10-20 12:20:00", "end": "2026-10-20 12:21:10"},
        {"name": "/c.h264", "begin": "2026-10-20 12:21:00", "end": "2026-10-20 13:00:00"},
    ]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def files(self, channel, begin, end):
            return files

    hub = types.SimpleNamespace(_client=lambda rec: FakeClient())
    pieces = asyncio.run(Agent._pieces(hub, {}, "/a.h264", 3, "2026-10-20 12:10:00",
                                       "2026-10-20 12:30:00"))
    assert pieces == [("/a.h264", "2026-10-20 12:10:00", "2026-10-20 12:20:00"),
                      ("/b.h264", "2026-10-20 12:20:00", "2026-10-20 12:21:10"),
                      ("/c.h264", "2026-10-20 12:21:10", "2026-10-20 12:30:00")]
    # No channel (older app): just the file it asked for.
    assert asyncio.run(Agent._pieces(hub, {}, "/a.h264", None, "x", "y")) == [("/a.h264", "x", "y")]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_both_playback_qualities_encode():
    video = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10",
         "-t", "2", "-c:v", "libx264", "-g", "10", "-f", "h264", "pipe:1"],
        capture_output=True, check=True).stdout
    assert rewind.clean_quality("low") == "low" and rewind.clean_quality("4k") == "normal"
    for q in ("normal", "low"):
        out = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "h264", "-framerate", "10", "-i", "pipe:0",
             "-an", "-c:v", "libx264", *rewind.QUALITY[q]["x264"], "-tune", "zerolatency",
             "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-vf", rewind.QUALITY[q]["scale"],
             "-g", "20", "-f", "mpegts", "pipe:1"],
            input=video, capture_output=True, check=True).stdout
        assert len(out) > 1000, q
