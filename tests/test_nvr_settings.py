"""Recorder settings against a fake recorder that behaves like the real NBD80S16S-KL."""

import asyncio
import copy
import json
import struct
from datetime import datetime

import pytest

from hub import nvr_settings
from hub.recorder import RecorderClient, RecorderError

DAY = ["1 00:00:00-24:00:00"] + ["0 00:00:00-00:00:00"] * 5


def record(mask="0x00000007", mode="ConfigRecord"):
    return {"Mask": [[mask] + ["0x00000000"] * 5 for _ in range(7)], "PacketLength": 60,
            "PreRecord": 5, "RecordMode": mode, "Redundancy": False,
            "TimeSection": [list(DAY) for _ in range(7)]}


def widget(name):
    attr = {"BackColor": "0x80000000", "EncodeBlend": True, "FrontColor": "0xF0FFFFFF",
            "PreviewBlend": True, "RelativePos": [570, 7552, 255, 24]}
    return {"ChannelTitle": {"Name": name, "SerialNo": "20081125"},
            "ChannelTitleAttribute": dict(attr), "CoversNum": 0, "TimeTitleAttribute": dict(attr)}


def initial():
    titles = ["BACK DOOR", "LATERAL RIGHT", "Patio Door"]
    return {
        "SystemInfo": {"HardWare": "NBD80S16S-KL", "SoftWareVersion": "V4.03.R11"},
        "StorageInfo": [{"Partition": [
            {"TotalSpace": "0x000E8E0C", "RemainSpace": "0x00000000",
             "OldStartTime": "2026-10-09 00:37:16", "OldEndTime": "2026-10-18 13:04:41",
             "NewStartTime": "2026-10-09 00:30:24", "NewEndTime": "2026-10-18 13:04:40"},
            {"TotalSpace": "0x00000000", "RemainSpace": "0x00000000",
             "OldStartTime": "0000-00-00 00:00:00", "OldEndTime": "0000-00-00 00:00:00",
             "NewStartTime": "0000-00-00 00:00:00", "NewEndTime": "0000-00-00 00:00:00"}]}],
        "ChannelTitle": titles,
        "AVEnc.VideoWidget": [widget(t) for t in titles],
        "Detect.MotionDetect": [{"Enable": True, "Level": 5, "Region": ["0xffffffff"],
                                 "EventHandler": {"RecordEnable": True}} for _ in titles],
        "Record": [record(), record("0x00000006"), record(mode="ClosedRecord")],
        "NetWork.RemoteDeviceV3": [{"Decoder": [{"Enable": True, "IPAddress": ip}]}
                                   for ip in ("192.168.0.108", "192.168.0.104", "192.168.0.109")],
        "General.AutoMaintain": {"AutoDeleteFilesDays": 0, "AutoRebootDay": "Monday",
                                 "AutoRebootHour": 14},
    }


class FakeRecorder:
    def __init__(self, per_channel_sets=True):
        self.cfg = initial()
        self.time = "2026-10-18 13:00:00"
        self.sets: list[str] = []
        self.rebooted = False
        self.per_channel_sets = per_channel_sets

    @staticmethod
    def pkt(msgid, body):
        data = json.dumps(body).encode() + b"\n\x00"
        return struct.pack("<BB2xII2xHI", 255, 0, 0x1234, 0, msgid, len(data)) + data

    async def handle(self, reader, writer):
        try:
            while True:
                hdr = await reader.readexactly(20)
                msgid = struct.unpack("<H", hdr[14:16])[0]
                size = struct.unpack("<I", hdr[16:20])[0]
                body = json.loads((await reader.readexactly(size)).rstrip(b"\x00\n"))
                writer.write(self.pkt(msgid + 1, self.reply(msgid, body)))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def reply(self, msgid, body):
        name = body.get("Name")
        if msgid == 1000:
            return {"Ret": 100, "SessionID": "0x00001234", "AliveInterval": 20}
        if msgid in (1020, 1042, 1048):
            if name not in self.cfg:
                return {"Ret": 607}
            return {"Ret": 100, "Name": name, name: copy.deepcopy(self.cfg[name])}
        if msgid in (1040, 1046):
            self.sets.append(name)
            base, _, idx = name.partition(".[")
            if idx:
                if not self.per_channel_sets:
                    return {"Ret": 607}
                self.cfg[base][int(idx.rstrip("]"))] = body[name]
            elif name in self.cfg:
                self.cfg[name] = body[name]
            else:
                return {"Ret": 607}
            return {"Ret": 100}
        if msgid == 1452:
            return {"Ret": 100, "Name": "OPTimeQuery", "OPTimeQuery": self.time}
        if msgid == 1450:
            if name == "OPTimeSetting":
                self.time = body[name]
            if name == "OPMachine":
                self.rebooted = body[name] == {"Action": "Reboot"}
            return {"Ret": 100}
        return {"Ret": 100}


def run(fake, fn):
    async def go():
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        import hub.recorder as rec
        old, rec.PORT = rec.PORT, server.sockets[0].getsockname()[1]
        try:
            async with RecorderClient("127.0.0.1", "u", "p") as c:
                return await fn(c)
        finally:
            rec.PORT = old
            server.close()
    return asyncio.run(go())


def test_read_summarizes_the_recorder():
    s = run(FakeRecorder(), lambda c: nvr_settings.read(c, {"k103": "192.168.0.104"}))
    assert s["model"] == "NBD80S16S-KL"
    assert s["storage"]["total_gb"] == 931.5 and s["storage"]["oldest"] == "2026-10-09 00:30:24"
    assert s["storage"]["days"] == 9.5
    assert s["auto_reboot"] == {"day": "Monday", "hour": 14}
    assert [c["recording"] for c in s["channels"]] == ["continuous", "motion", "off"]
    ch1 = s["channels"][1]
    assert ch1["title"] == "LATERAL RIGHT" and ch1["ip"] == "192.168.0.104"
    assert ch1["stream_key"] == "k103"            # matched to the live camera by IP
    assert ch1["show_title"] is True and ch1["motion_level"] == 5


def test_rename_overlay_motion_recording_and_clock():
    fake = FakeRecorder()

    async def changes(c):
        await nvr_settings.apply(c, {"channel": 2, "title": "  Living   room "})
        await nvr_settings.apply(c, {"channel": 2, "show_title": False})
        await nvr_settings.apply(c, {"channel": 0, "motion_level": 9})
        await nvr_settings.apply(c, {"channel": 0, "recording": "motion"})
        await nvr_settings.apply(c, {"channel": 2, "recording": "continuous"})
        await nvr_settings.apply(c, {"sync_time": True})
        await nvr_settings.apply(c, {"auto_reboot_day": "Never"})
        return await nvr_settings.read(c)

    s = run(fake, changes)
    assert fake.cfg["ChannelTitle"][2] == "Living room"
    w = fake.cfg["AVEnc.VideoWidget"][2]
    assert w["ChannelTitleAttribute"]["EncodeBlend"] is False
    assert w["TimeTitleAttribute"]["EncodeBlend"] is True          # clock left alone
    assert fake.cfg["AVEnc.VideoWidget"][0]["ChannelTitleAttribute"]["EncodeBlend"] is True
    assert fake.cfg["Detect.MotionDetect"][0]["Level"] == 6        # clamped to 1-6
    assert [c["recording"] for c in s["channels"]] == ["motion", "motion", "continuous"]
    assert fake.cfg["Record"][0]["PreRecord"] == 5                 # untouched fields kept
    assert abs((datetime.strptime(fake.time, "%Y-%m-%d %H:%M:%S") - datetime.now())
               .total_seconds()) < 5
    assert fake.cfg["General.AutoMaintain"] == {"AutoDeleteFilesDays": 0,
                                                "AutoRebootDay": "Never", "AutoRebootHour": 14}
    assert "Record.[0]" in fake.sets                                 # only that camera sent


def test_falls_back_to_whole_list_when_per_channel_refused():
    fake = FakeRecorder(per_channel_sets=False)
    run(fake, lambda c: nvr_settings.apply(c, {"channel": 1, "motion_enabled": False}))
    assert fake.cfg["Detect.MotionDetect"][1]["Enable"] is False
    assert fake.cfg["Detect.MotionDetect"][0]["Enable"] is True
    assert "Detect.MotionDetect" in fake.sets


def test_bad_changes_are_refused_and_reboot_works():
    fake = FakeRecorder()
    with pytest.raises(RecorderError):
        run(fake, lambda c: nvr_settings.apply(c, {"channel": 7, "title": "x"}))
    with pytest.raises(RecorderError):
        run(fake, lambda c: nvr_settings.apply(c, {"channel": 0, "recording": "sometimes"}))
    with pytest.raises(RecorderError):
        run(fake, lambda c: nvr_settings.apply(c, {"channel": 0, "title": "   "}))
    assert fake.sets == []
    run(fake, lambda c: nvr_settings.apply(c, {"reboot": True}))
    assert fake.rebooted


def test_recording_modes():
    assert nvr_settings.recording_mode(record("0x00000007")) == "continuous"
    assert nvr_settings.recording_mode(record("0x00000002")) == "motion"
    assert nvr_settings.recording_mode(record(mode="ManualRecord")) == "continuous"
    night = record()
    night["TimeSection"][0][0] = "1 20:00:00-24:00:00"
    assert nvr_settings.recording_mode(night) == "custom"


def test_overlay_changes_go_to_the_camera_itself():
    cam = FakeRecorder()
    cam.cfg = {"ChannelTitle": ["Patio Door"], "AVEnc.VideoWidget": [widget("Patio Door")]}

    async def go():
        server = await asyncio.start_server(cam.handle, "127.0.0.1", 0)
        import hub.recorder as rec
        old, rec.PORT = rec.PORT, server.sockets[0].getsockname()[1]
        try:
            async with RecorderClient("127.0.0.1", "u", "p") as c:
                await nvr_settings.apply_to_camera(c, {"channel": 7, "show_title": False})
                await nvr_settings.apply_to_camera(c, {"channel": 7, "title": "Living room"})
                assert await nvr_settings.read_camera_overlay(c) == {"show_title": False,
                                                                     "show_time": True}
        finally:
            rec.PORT = old
            server.close()

    asyncio.run(go())
    w = cam.cfg["AVEnc.VideoWidget"][0]
    assert w["ChannelTitleAttribute"]["EncodeBlend"] is False
    assert w["TimeTitleAttribute"]["EncodeBlend"] is True
    assert w["ChannelTitle"]["Name"] == "Living room" and cam.cfg["ChannelTitle"] == ["Living room"]


def test_camera_logins_come_from_the_recorder():
    fake = FakeRecorder()
    fake.cfg["NetWork.RemoteDeviceV3"][1]["Decoder"][0].update(UserName="admin", PassWord="s3cret")
    got = run(fake, nvr_settings.camera_logins)
    assert got["192.168.0.104"] == ("admin", "s3cret")
    assert got["192.168.0.108"] == ("admin", "")


def test_day_night_color_on_a_xiongmai_camera():
    cam = FakeRecorder()
    cam.cfg = {"Camera.Param": [{"AeSensitivity": 5, "DayNightColor": "0x00000000"}]}

    async def go(c):
        await nvr_settings.set_day_night(c, "ON")
        return await nvr_settings.get_day_night(c)

    assert run(cam, go) == "ON"
    assert cam.cfg["Camera.Param"][0] == {"AeSensitivity": 5, "DayNightColor": "0x00000001"}
    with pytest.raises(RecorderError):
        run(cam, lambda c: nvr_settings.set_day_night(c, "PURPLE"))
