import asyncio
import json
import struct

from hub.alarms import Debouncer, normalize
from hub.recorder import RecorderClient


def pkt(msgid: int, body: dict, session: int = 0x1234) -> bytes:
    data = json.dumps(body).encode() + b"\n\x00"
    return struct.pack("<BB2xII2xHI", 255, 0, session, 0, msgid, len(data)) + data


async def fake_recorder(reader, writer):
    async def read_one():
        hdr = await reader.readexactly(20)
        size = struct.unpack("<I", hdr[16:20])[0]
        return struct.unpack("<H", hdr[14:16])[0], await reader.readexactly(size)

    msgid, _ = await read_one()                       # login
    assert msgid == 1000
    writer.write(pkt(1001, {"Ret": 100, "SessionID": "0x00001234", "AliveInterval": 5}))
    msgid, _ = await read_one()                       # subscribe
    assert msgid == 1500
    writer.write(pkt(1501, {"Ret": 100}))
    for status in ("Start", "Stop"):
        writer.write(pkt(1504, {"Name": "AlarmInfo", "AlarmInfo": {
            "Channel": 2, "Event": "VideoMotion", "StartTime": "2026-10-18 10:40:12",
            "Status": status}}))
    await writer.drain()
    writer.close()


def test_alarm_subscription_reads_alarm_info():
    async def run():
        server = await asyncio.start_server(fake_recorder, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        import hub.recorder as rec
        old, rec.PORT = rec.PORT, port
        try:
            async with RecorderClient("127.0.0.1", "u", "p") as c:
                assert c.alive == 5
                return [info async for info in c.alarms()]
        finally:
            rec.PORT = old
            server.close()

    got = asyncio.run(run())
    assert [g["Status"] for g in got] == ["Start", "Stop"]
    assert normalize(got[0]) == {"channel": 2, "kind": "motion", "event": "VideoMotion",
                                 "time": "2026-10-18 10:40:12"}
    assert normalize(got[1]) is None   # 'Stop' is not a new event


def test_kinds_and_debounce():
    assert normalize({"Channel": 0, "Event": "HumanDetect", "Status": "Start"})["kind"] == "person"
    assert normalize({"Channel": 0, "Event": "VideoLoss", "Status": "Start"})["kind"] == "video_loss"
    d = Debouncer()
    assert d.allow(("r", 0, "motion"), "motion", now=0)
    assert not d.allow(("r", 0, "motion"), "motion", now=60)      # within 2 min
    assert d.allow(("r", 1, "motion"), "motion", now=60)          # other camera
    assert d.allow(("r", 0, "motion"), "motion", now=121)
