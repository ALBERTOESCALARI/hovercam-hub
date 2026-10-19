"""Recorder (Xiongmai NVR/DVR) settings in plain terms, for the app's Recorder settings screen.

read() turns the recorder's own configs into a small summary; apply() makes one change at a
time and only touches the fields it means to (it re-sends the recorder's own config with
those fields changed). Verified field names on an NBD80S16S-KL (V4.03.R11):
  ChannelTitle                       ["BACK DOOR", ...]
  AVEnc.VideoWidget[i]               ChannelTitleAttribute / TimeTitleAttribute .EncodeBlend
                                     (drawn into the video) and .PreviewBlend (NVR monitor)
  Detect.MotionDetect[i]             Enable, Level 1-6, EventHandler.RecordEnable
  Record[i]                          RecordMode ConfigRecord|ManualRecord|ClosedRecord,
                                     TimeSection[day][6] "1 00:00:00-24:00:00",
                                     Mask[day][6] bits 1 regular, 2 motion, 4 alarm
  NetWork.RemoteDeviceV3[i]          Decoder[0].IPAddress (the camera on that channel)
  StorageInfo                        partitions with TotalSpace/RemainSpace (hex MB)
  General.AutoMaintain               AutoRebootDay ("Never", "EveryDay", "Monday"...), Hour
"""

from __future__ import annotations

import copy
from datetime import datetime

from hub.recorder import FMT, RecorderClient, RecorderError

FULL_DAY = "1 00:00:00-24:00:00"
OFF_SECTION = "0 00:00:00-24:00:00"
MASKS = {"continuous": "0x00000007", "motion": "0x00000006"}
REBOOT_DAYS = ["Never", "EveryDay", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
               "Friday", "Saturday"]
MAX_TITLE = 32


def _hex(v) -> int:
    try:
        return int(str(v), 16)
    except ValueError:
        return 0


def _time(v) -> datetime | None:
    try:
        t = datetime.strptime(str(v), FMT)
    except ValueError:
        return None
    return t if t.year > 2000 else None


def recording_mode(rec: dict) -> str:
    """'continuous', 'motion', 'off' or 'custom' (a schedule set on the recorder itself)."""
    mode = rec.get("RecordMode")
    if mode == "ClosedRecord":
        return "off"
    if mode == "ManualRecord":
        return "continuous"
    masks = set()
    any_on = False
    for sections, day_masks in zip(rec.get("TimeSection") or [], rec.get("Mask") or []):
        on = [(t, m) for t, m in zip(sections, day_masks) if str(t).startswith("1 ")]
        if not on:
            continue
        any_on = True
        if len(on) != 1 or on[0][0] != FULL_DAY:
            return "custom"
        masks.add(_hex(on[0][1]))
    if not any_on:
        return "off"
    if len(masks) != 1 or len(rec.get("TimeSection") or []) < 7:
        return "custom"
    m = masks.pop()
    if m & 1:
        return "continuous"
    if m & 2:
        return "motion"
    return "custom"


def set_recording(rec: dict, mode: str) -> dict:
    rec = copy.deepcopy(rec)
    if mode == "off":
        rec["RecordMode"] = "ClosedRecord"
        return rec
    rec["RecordMode"] = "ConfigRecord"
    width = max((len(d) for d in rec.get("TimeSection") or []), default=6) or 6
    rec["TimeSection"] = [[FULL_DAY] + [OFF_SECTION] * (width - 1) for _ in range(7)]
    rec["Mask"] = [[MASKS[mode]] + ["0x00000000"] * (width - 1) for _ in range(7)]
    return rec


def storage_summary(storage) -> dict:
    total = free = 0
    starts, ends = [], []
    for disk in storage or []:
        for part in disk.get("Partition") or []:
            total += _hex(part.get("TotalSpace"))
            free += _hex(part.get("RemainSpace"))
            starts += [t for t in (_time(part.get("OldStartTime")), _time(part.get("NewStartTime")))
                       if t]
            ends += [t for t in (_time(part.get("OldEndTime")), _time(part.get("NewEndTime"))) if t]
    oldest, newest = (min(starts) if starts else None), (max(ends) if ends else None)
    return {
        "disks": len(storage or []),
        "total_gb": round(total / 1024, 1),
        "free_gb": round(free / 1024, 1),
        "oldest": oldest.strftime(FMT) if oldest else None,
        "newest": newest.strftime(FMT) if newest else None,
        "days": round((newest - oldest).total_seconds() / 86400, 1) if oldest and newest else None,
    }


def summarize(raw: dict, hub_now: datetime, camera_ips: dict[str, str] | None = None) -> dict:
    """raw holds the recorder's configs by name. camera_ips maps this hub's live camera
    stream keys to their IP, so channels can say which live camera they are."""
    titles = [str(t) for t in raw.get("ChannelTitle") or []]
    widgets = raw.get("AVEnc.VideoWidget") or []
    motion = raw.get("Detect.MotionDetect") or []
    record = raw.get("Record") or []
    remote = raw.get("NetWork.RemoteDeviceV3") or []
    by_ip = {ip: key for key, ip in (camera_ips or {}).items() if ip}
    channels = []
    for i, title in enumerate(titles):
        w = widgets[i] if i < len(widgets) else {}
        m = motion[i] if i < len(motion) else {}
        decoders = (remote[i].get("Decoder") if i < len(remote) else None) or [{}]
        ip = decoders[0].get("IPAddress") if decoders[0].get("Enable", True) else None
        channels.append({
            "channel": i,
            "title": title,
            "ip": ip or None,
            "stream_key": by_ip.get(ip) if ip else None,
            "show_title": (w.get("ChannelTitleAttribute") or {}).get("EncodeBlend"),
            "show_time": (w.get("TimeTitleAttribute") or {}).get("EncodeBlend"),
            "motion_enabled": m.get("Enable") if m else None,
            "motion_level": m.get("Level") if m else None,
            "recording": recording_mode(record[i]) if i < len(record) else None,
        })
    info = raw.get("SystemInfo") or {}
    nvr_time = _time(raw.get("OPTimeQuery"))
    maintain = raw.get("General.AutoMaintain") or {}
    return {
        "model": info.get("HardWare") or info.get("DeviceModel"),
        "firmware": info.get("SoftWareVersion"),
        "storage": storage_summary(raw.get("StorageInfo")),
        "time": nvr_time.strftime(FMT) if nvr_time else None,
        "clock_off_s": round((nvr_time - hub_now).total_seconds()) if nvr_time else None,
        "auto_reboot": {"day": maintain.get("AutoRebootDay"), "hour": maintain.get("AutoRebootHour")}
        if maintain else None,
        "channels": channels,
    }


READS = [("SystemInfo", 1020), ("StorageInfo", 1020), ("ChannelTitle", 1048),
         ("AVEnc.VideoWidget", 1042), ("Detect.MotionDetect", 1042), ("Record", 1042),
         ("NetWork.RemoteDeviceV3", 1042), ("General.AutoMaintain", 1042)]


async def read(c: RecorderClient, camera_ips: dict[str, str] | None = None) -> dict:
    raw: dict = {}
    for name, code in READS:
        try:
            raw[name] = await c.config_get(name, code)
        except RecorderError:
            raw[name] = None  # this model doesn't have it; the app hides that control
    t = await c.get_time()
    raw["OPTimeQuery"] = t.strftime(FMT) if t else None
    return summarize(raw, datetime.now(), camera_ips)


async def _set_one(c: RecorderClient, name: str, items: list, i: int) -> int:
    """Change one channel's entry: try just that channel; if the recorder refuses it or
    doesn't actually apply it, re-send the whole list (always works, just bigger)."""
    try:
        ret = await c.config_set(f"{name}.[{i}]", items[i])
        if (await c.config_get(name))[i] == items[i]:
            return ret
    except RecorderError:
        pass
    return await c.config_set(name, items)


def clean_title(title: str) -> str:
    title = " ".join(str(title).split())[:MAX_TITLE]
    if not title:
        raise RecorderError("the camera name can't be empty")
    return title


async def apply(c: RecorderClient, change: dict) -> dict:
    """One change from the app. Returns {"restart_needed": bool}."""
    rets: list[int] = []
    ch = change.get("channel")
    per_channel = {"title", "show_title", "show_time", "motion_enabled", "motion_level",
                   "recording"}
    if per_channel & change.keys():
        count = len(await c.config_get("ChannelTitle", 1048))
        if not isinstance(ch, int) or not 0 <= ch < count:
            raise RecorderError("no such camera on this recorder")
    if change.get("recording") not in (None, "continuous", "motion", "off"):
        raise RecorderError("recording must be continuous, motion or off")
    if "auto_reboot_day" in change and change["auto_reboot_day"] not in REBOOT_DAYS:
        raise RecorderError("unknown restart day")
    if "title" in change:
        titles = [str(t) for t in await c.config_get("ChannelTitle", 1048)]
        titles[ch] = clean_title(change["title"])
        rets.append(await c.set_titles(titles))
        if [str(t) for t in await c.config_get("ChannelTitle", 1048)][ch] != titles[ch]:
            widgets = await c.config_get("AVEnc.VideoWidget")   # older firmware keeps it here
            widgets[ch]["ChannelTitle"]["Name"] = titles[ch]
            rets.append(await _set_one(c, "AVEnc.VideoWidget", widgets, ch))
    if "show_title" in change or "show_time" in change:
        widgets = await c.config_get("AVEnc.VideoWidget")
        for key, attr in (("show_title", "ChannelTitleAttribute"),
                          ("show_time", "TimeTitleAttribute")):
            if key in change:
                widgets[ch][attr]["EncodeBlend"] = bool(change[key])
                widgets[ch][attr]["PreviewBlend"] = bool(change[key])
        rets.append(await _set_one(c, "AVEnc.VideoWidget", widgets, ch))
    if "motion_enabled" in change or "motion_level" in change or \
            change.get("recording") == "motion":
        motion = await c.config_get("Detect.MotionDetect")
        if "motion_enabled" in change:
            motion[ch]["Enable"] = bool(change["motion_enabled"])
        if "motion_level" in change:
            motion[ch]["Level"] = max(1, min(6, int(change["motion_level"])))
        if change.get("recording") == "motion":   # motion recording needs motion detection
            motion[ch]["Enable"] = True
            motion[ch].setdefault("EventHandler", {})["RecordEnable"] = True
        rets.append(await _set_one(c, "Detect.MotionDetect", motion, ch))
    if "recording" in change:
        record = await c.config_get("Record")
        record[ch] = set_recording(record[ch], change["recording"])
        rets.append(await _set_one(c, "Record", record, ch))
    if change.get("sync_time"):
        rets.append(await c.set_time(datetime.now()))
    if "auto_reboot_day" in change or "auto_reboot_hour" in change:
        maintain = await c.config_get("General.AutoMaintain")
        if "auto_reboot_day" in change:
            maintain["AutoRebootDay"] = change["auto_reboot_day"]
        if "auto_reboot_hour" in change:
            maintain["AutoRebootHour"] = int(change["auto_reboot_hour"])
        rets.append(await c.config_set("General.AutoMaintain", maintain))
    if change.get("reboot"):
        await c.reboot()
    return {"restart_needed": 603 in rets}


OVERLAY_KEYS = ("title", "show_title", "show_time")


async def apply_to_camera(cam: RecorderClient, change: dict) -> None:
    """The name and clock drawn on the video come from the camera itself; the recorder
    just records what the camera sends. So overlay changes also go to the camera (same
    DVRIP protocol, single channel). Raises RecorderError if the camera won't take them."""
    if "title" in change:
        try:
            await cam.set_titles([clean_title(change["title"])])
        except RecorderError:
            pass  # some cameras only keep it in the video widget, below
    widgets = await cam.config_get("AVEnc.VideoWidget")
    items = widgets if isinstance(widgets, list) else [widgets]
    w = items[0]
    if "title" in change and isinstance(w.get("ChannelTitle"), dict):
        w["ChannelTitle"]["Name"] = clean_title(change["title"])
    for key, attr in (("show_title", "ChannelTitleAttribute"),
                      ("show_time", "TimeTitleAttribute")):
        if key in change and isinstance(w.get(attr), dict):
            w[attr]["EncodeBlend"] = bool(change[key])
            w[attr]["PreviewBlend"] = bool(change[key])
    await cam.config_set("AVEnc.VideoWidget", items if isinstance(widgets, list) else w)


async def read_camera_overlay(cam: RecorderClient) -> dict:
    """What the camera itself draws on its video: {"show_title", "show_time"}."""
    widgets = await cam.config_get("AVEnc.VideoWidget")
    w = (widgets[0] if isinstance(widgets, list) and widgets else widgets) or {}
    return {"show_title": (w.get("ChannelTitleAttribute") or {}).get("EncodeBlend"),
            "show_time": (w.get("TimeTitleAttribute") or {}).get("EncodeBlend")}


async def camera_logins(c: RecorderClient) -> dict[str, tuple[str, str]]:
    """The login the recorder itself uses for each camera ({ip: (user, password)}). Kept in
    the hub's memory only, never sent anywhere: it's the one login known to work."""
    out: dict[str, tuple[str, str]] = {}
    try:
        remote = await c.config_get("NetWork.RemoteDeviceV3")
    except RecorderError:
        return out
    for item in remote or []:
        for d in item.get("Decoder") or []:
            ip = d.get("IPAddress")
            if ip and d.get("Enable", True):
                out[ip] = (str(d.get("UserName") or "admin"), str(d.get("PassWord") or ""))
    return out


DAY_NIGHT = {"AUTO": "0x00000000", "ON": "0x00000001", "OFF": "0x00000002"}  # ON = color


async def set_day_night(cam: RecorderClient, mode: str) -> None:
    """Color / black & white / auto on a Xiongmai camera (Camera.Param.DayNightColor). Many
    of these cameras accept the ONVIF day/night setting but ignore it; this one sticks."""
    value = DAY_NIGHT.get(str(mode).upper())
    if value is None:
        raise RecorderError("unknown day/night mode")
    params = await cam.config_get("Camera.Param")
    items = params if isinstance(params, list) else [params]
    if not items or "DayNightColor" not in items[0]:
        raise RecorderError("this camera has no day/night setting")
    items[0]["DayNightColor"] = value
    await cam.config_set("Camera.Param", items if isinstance(params, list) else items[0])


async def get_day_night(cam: RecorderClient) -> str | None:
    params = await cam.config_get("Camera.Param")
    item = (params[0] if isinstance(params, list) and params else params) or {}
    value = str(item.get("DayNightColor", "")).lower()
    for mode, hexv in DAY_NIGHT.items():
        if value and int(value, 16) == int(hexv, 16):
            return mode
    return None
