"""Unwrap Xiongmai/XMEye recorder video into a plain H.264/H.265 stream.

Recorder files and playback streams interleave frames like:
  00 00 01 FC  media fps w/8 h/8  datetime(u32)  length(u32)  <video: key frame>
  00 00 01 FD  length(u32)                                  <video: P frame>
  00 00 01 FA  codec rate length(u16)                       <audio, G.711>
  00 00 01 F9  type n length(u16)                           <info>
  00 00 01 FE  (like FC)                                    <JPEG snapshot>
Bytes 0xF9-0xFE can never start a real H.264/H.265 NAL unit (forbidden bit set), so it is
safe to resynchronise on these markers if the stream starts mid-frame.
"""

from __future__ import annotations

import struct
from datetime import datetime

_MARKERS = {0xF9, 0xFA, 0xFC, 0xFD, 0xFE}
_CODECS = {1: "mpeg4", 2: "h264", 3: "hevc", 0x12: "h264", 0x13: "hevc"}
_MAX_FRAME = 16 * 1024 * 1024


def _xm_datetime(v: int) -> datetime | None:
    try:
        return datetime(((v >> 26) & 0x3F) + 2000, (v >> 22) & 0xF, (v >> 17) & 0x1F,
                        (v >> 12) & 0x1F, (v >> 6) & 0x3F, v & 0x3F)
    except ValueError:
        return None


def sniff_codec(payload: bytes) -> str | None:
    """Tell H.264 from H.265 by the first NAL unit header of a key frame."""
    i = payload.find(b"\x00\x00\x01")
    if i < 0 or i + 4 >= len(payload):
        return None
    b = payload[i + 3]
    if b & 0x81 == 0 and (b >> 1) & 0x3F in (32, 33, 34, 35, 39):  # HEVC VPS/SPS/PPS/AUD/SEI
        return "hevc"
    if b & 0x1F in (5, 6, 7, 8, 9):                             # H.264 IDR/SEI/SPS/PPS/AUD
        return "h264"
    return None


def _next_marker(data: bytes, start: int) -> int:
    i = start
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            return -1
        if data[i + 3] in _MARKERS:
            return i
        i += 1


def unwrap(data: bytes) -> tuple[bytes, dict]:
    """Return (elementary video stream, info) where info has codec, fps, width, height,
    started (datetime of the first key frame), key_frames, frames.

    Output starts at the first key frame (frames before it can't be decoded). Width/height
    come from one-byte fields (pixels/8), so sizes above 2040 wrap; treat them as hints."""
    out = bytearray()
    info = {"codec": None, "fps": None, "width": None, "height": None, "started": None,
            "key_frames": 0, "frames": 0}
    i = _next_marker(data, 0)
    n = len(data)
    while 0 <= i and i + 8 <= n:
        kind = data[i + 3]
        if kind in (0xFC, 0xFE):
            if i + 16 > n:
                break
            media, fps, w, h, stamp, length = struct.unpack("<BBBBII", data[i + 4:i + 16])
            header = 16
            if kind == 0xFC:
                info["key_frames"] += 1
                info["codec"] = info["codec"] or sniff_codec(
                    data[i + 16:i + 16 + min(length, 64)]) or _CODECS.get(media)
                info["fps"] = info["fps"] or (fps or None)
                info["width"] = info["width"] or w * 8
                info["height"] = info["height"] or h * 8
                info["started"] = info["started"] or _xm_datetime(stamp)
        elif kind == 0xFD:
            (length,) = struct.unpack("<I", data[i + 4:i + 8])
            header = 8
        else:
            _, _, length = struct.unpack("<BBH", data[i + 4:i + 8])
            header = 8
        if length > _MAX_FRAME:          # garbage that looked like a marker: resync
            i = _next_marker(data, i + 4)
            continue
        end = i + header + length
        if kind in (0xFC, 0xFD) and info["key_frames"]:
            out += data[i + header:min(end, n)]
            info["frames"] += 1
        if end > n:                      # last frame cut off by the end of the download
            break
        nxt = end if end + 4 <= n and data[end:end + 3] == b"\x00\x00\x01" \
            and data[end + 3] in _MARKERS else _next_marker(data, end)
        i = nxt
    return bytes(out), info


class Unwrapper:
    """Streaming version of unwrap(): feed recorder bytes as they arrive, get video bytes
    back. Only complete frames are emitted; partial ones wait for the next chunk."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.info = {"codec": None, "fps": None, "width": None, "height": None,
                     "started": None, "key_frames": 0, "frames": 0}

    def feed(self, chunk: bytes) -> bytes:
        buf = self._buf
        buf += chunk
        out = bytearray()
        i, n = 0, len(buf)
        while True:
            if not (i + 4 <= n and buf[i:i + 3] == b"\x00\x00\x01" and buf[i + 3] in _MARKERS):
                j = _next_marker(bytes(buf[i:]), 0)
                if j < 0:
                    i = max(i, n - 3)      # keep a possible partial marker
                    break
                i += j
            if i + 8 > n:
                break
            kind = buf[i + 3]
            if kind in (0xFC, 0xFE):
                if i + 16 > n:
                    break
                media, fps, w, h, stamp, length = struct.unpack("<BBBBII", buf[i + 4:i + 16])
                header = 16
            elif kind == 0xFD:
                (length,) = struct.unpack("<I", buf[i + 4:i + 8])
                header = 8
            else:
                _, _, length = struct.unpack("<BBH", buf[i + 4:i + 8])
                header = 8
            if length > _MAX_FRAME:
                i += 4
                continue
            end = i + header + length
            if end > n:
                break                      # wait for the rest of this frame
            if kind == 0xFC:
                info = self.info
                info["key_frames"] += 1
                info["codec"] = info["codec"] or sniff_codec(
                    bytes(buf[i + 16:i + 16 + min(length, 64)])) or _CODECS.get(media)
                info["fps"] = info["fps"] or (fps or None)
                info["width"] = info["width"] or w * 8
                info["height"] = info["height"] or h * 8
                info["started"] = info["started"] or _xm_datetime(stamp)
            if kind in (0xFC, 0xFD) and self.info["key_frames"]:
                out += buf[i + header:end]
                self.info["frames"] += 1
            i = end
        del buf[:i]
        return bytes(out)
