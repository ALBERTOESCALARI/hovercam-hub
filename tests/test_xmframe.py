import struct

from hub.xmframe import unwrap


def key_frame(payload, fps=15, w=1920, h=1080, stamp=0):
    return b"\x00\x00\x01\xfc" + struct.pack("<BBBBII", 2, fps, w // 8, h // 8, stamp,
                                             len(payload)) + payload


def p_frame(payload):
    return b"\x00\x00\x01\xfd" + struct.pack("<I", len(payload)) + payload


def audio(payload):
    return b"\x00\x00\x01\xfa" + struct.pack("<BBH", 14, 2, len(payload)) + payload


SPS_IDR = b"\x00\x00\x00\x01\x67\x42\x00\x1f\x00\x00\x00\x01\x68\xce\x00\x00\x00\x01\x65\x88\x84"
P1 = b"\x00\x00\x00\x01\x41\x9a\x01"
P2 = b"\x00\x00\x00\x01\x41\x9a\x02"


def test_strips_headers_and_audio():
    # 2026-10-17 18:00:00 in XM packed form
    stamp = (26 << 26) | (10 << 22) | (17 << 17) | (18 << 12)
    data = key_frame(SPS_IDR, stamp=stamp) + audio(b"\xd5" * 160) + p_frame(P1) + p_frame(P2)
    video, info = unwrap(data)
    assert video == SPS_IDR + P1 + P2
    assert info["codec"] == "h264" and info["fps"] == 15
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["started"].isoformat() == "2026-10-17T18:00:00"
    assert info["frames"] == 3 and info["key_frames"] == 1


def test_resyncs_when_download_starts_mid_frame():
    data = b"\x12\x34junk-from-a-previous-frame" + key_frame(SPS_IDR) + p_frame(P1)
    video, info = unwrap(data)
    assert video == SPS_IDR + P1


def test_keeps_partial_last_frame():
    data = key_frame(SPS_IDR) + p_frame(P1 + b"\xaa" * 50)[:-20]
    video, _ = unwrap(data)
    assert video.startswith(SPS_IDR + P1)


HEVC_KEY = (b"\x00\x00\x00\x01\x40\x01\x0c" + b"\x00\x00\x00\x01\x42\x01\x01"
            + b"\x00\x00\x00\x01\x44\x01\xc1" + b"\x00\x00\x00\x01\x26\x01\xaf")


def test_detects_hevc_from_bitstream_even_with_unknown_media_byte():
    frame = b"\x00\x00\x01\xfc" + struct.pack("<BBBBII", 0x55, 18, 320 % 256, 180, 0,
                                                len(HEVC_KEY)) + HEVC_KEY
    video, info = unwrap(frame + p_frame(b"\x00\x00\x00\x01\x02\x01\xd0"))
    assert info["codec"] == "hevc"
    assert video.startswith(HEVC_KEY)


def test_drops_frames_before_first_key_frame():
    data = p_frame(P1) + p_frame(P2) + key_frame(SPS_IDR) + p_frame(P1)
    video, info = unwrap(data)
    assert video == SPS_IDR + P1 and info["frames"] == 2


def test_streaming_unwrapper_matches_one_shot_for_any_chunking():
    from hub.xmframe import Unwrapper
    data = (b"tail-of-old-frame" + key_frame(SPS_IDR) + audio(b"\xd5" * 300) + p_frame(P1)
            + p_frame(P2) + key_frame(SPS_IDR) + p_frame(P1))
    expected, _ = unwrap(data)
    for size in (1, 3, 7, 16, 64, len(data)):
        u = Unwrapper()
        got = b"".join(u.feed(data[i:i + size]) for i in range(0, len(data), size))
        assert got == expected, size
        assert u.info["codec"] == "h264" and u.info["key_frames"] == 2
