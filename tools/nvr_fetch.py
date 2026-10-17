"""Test: download a short piece of a recording from a Xiongmai/XMEye recorder and play it.
Read-only on the recorder. Usage:  python3 nvr_fetch.py 192.168.0.103 [channel 1-16] [minutes]"""
import getpass, hashlib, json, os, shutil, socket, struct, subprocess, sys, time
from datetime import datetime, timedelta

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.103"
CHANNEL = int(sys.argv[2]) if len(sys.argv) > 2 else 1
MINUTES = int(sys.argv[3]) if len(sys.argv) > 3 else 2
CAP = 120 * 1024 * 1024  # stop after 120 MB no matter what
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
FMT = "%Y-%m-%d %H:%M:%S"


def sofia_hash(pw):
    d = hashlib.md5(pw.encode()).digest()
    return "".join(CHARS[(a + b) % 62] for a, b in zip(d[::2], d[1::2]))


class Recorder:
    def __init__(self, host):
        self.sock = socket.create_connection((host, 34567), timeout=15)
        self.session, self.seq = 0, 0

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("recorder closed the connection")
            buf += chunk
        return buf

    def _send_only(self, msg, data):
        body = json.dumps(data).encode()
        self.sock.sendall(struct.pack("<BB2xII2xHI", 255, 0, self.session, self.seq, msg,
                                      len(body) + 2) + body + b"\x0a\x00")
        self.seq += 1

    def _read_packet(self):
        hdr = self._recv(20)
        msgid = struct.unpack("<H", hdr[14:16])[0]
        size = struct.unpack("<I", hdr[16:20])[0]
        return msgid, (self._recv(size) if size else b"")

    def send(self, msg, data):
        self._send_only(msg, data)
        _, payload = self._read_packet()
        text = payload.rstrip(b"\x00\x0a").decode("utf-8", "replace")
        return json.loads(text) if text else {}

    def login(self, user, pw):
        r = self.send(1000, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                             "PassWord": sofia_hash(pw), "UserName": user})
        if r.get("Ret") not in (100, 515):
            raise SystemExit(f"Login failed (code {r.get('Ret')}).")
        self.session = int(r["SessionID"], 16)

    def files(self, ch, begin, end):
        """All files in the range; the recorder returns at most 64 per request."""
        out, start = [], begin
        while True:
            r = self.send(1440, {"Name": "OPFileQuery", "OPFileQuery": {
                "BeginTime": start, "EndTime": end, "Channel": ch, "DriverTypeMask": "0x0000FFFF",
                "Event": "*", "StreamType": "0x00000000", "Type": "h264"}})
            batch = (r.get("OPFileQuery") or []) if r.get("Ret") == 100 else []
            new = [f for f in batch if f not in out]
            out += new
            if len(batch) < 64 or not new:
                return out
            start = batch[-1]["BeginTime"]

    def download(self, f, start, end, path):
        param = {"PlayMode": "ByName", "FileName": f["FileName"], "StreamType": 0, "Value": 0,
                 "TransMode": "TCP"}
        claim = self.send(1424, {"Name": "OPPlayBack", "OPPlayBack": {
            "Action": "Claim", "Parameter": param, "StartTime": start, "EndTime": end}})
        print("  claim reply:", claim.get("Ret"))
        self._send_only(1420, {"Name": "OPPlayBack", "OPPlayBack": {
            "Action": "DownloadStart", "Parameter": param, "StartTime": start, "EndTime": end}})
        total, ids, t0, next_report = 0, set(), time.time(), 1 << 20
        with open(path, "wb") as fh:
            while True:
                try:
                    msgid, data = self._read_packet()
                except socket.timeout:
                    print("  (no more data after 15 s, stopping)")
                    break
                ids.add(msgid)
                if not data:
                    break
                if total == 0 and data.lstrip().startswith(b"{"):
                    print("  start reply:", data[:60])
                    continue
                fh.write(data)
                total += len(data)
                if total >= next_report:
                    print(f"  {total / 1048576:.1f} MB  ({total / 1048576 / (time.time() - t0):.1f} MB/s)",
                          end="\r")
                    next_report += 1 << 20
                if total >= CAP:
                    print("\n  stopped at the 120 MB safety cap")
                    break
        print(f"\n  downloaded {total / 1048576:.2f} MB in {time.time() - t0:.1f} s; packet ids {sorted(ids)}")
        return total


def main():
    user = input("Recorder username: ").strip()
    pw = getpass.getpass("Recorder password (hidden): ")
    rec = Recorder(HOST)
    rec.login(user, pw)
    ch = CHANNEL - 1
    now = datetime.now()
    files = rec.files(ch, now.strftime("%Y-%m-%d 00:00:00"), now.strftime(FMT))
    print(f"Channel {CHANNEL}: {len(files)} files today")
    if not files:
        raise SystemExit("No recordings today on that channel; try another channel number.")
    f = files[-2] if len(files) > 1 else files[-1]   # last *finished* file
    fb, fe = (datetime.strptime(f[k], FMT) for k in ("BeginTime", "EndTime"))
    file_kb = int(f.get("FileLength", "0"), 0)
    print(f"Using {f['FileName']}\n  {f['BeginTime']} -> {f['EndTime']}  ({file_kb / 1024:.1f} MB whole file)")
    ws = max(fb, fe - timedelta(minutes=MINUTES))
    print(f"Asking for just {ws.strftime('%H:%M:%S')} -> {fe.strftime('%H:%M:%S')}")
    desk = os.path.expanduser("~/Desktop")
    raw = os.path.join(desk, "hovercam_clip.h264")
    got = rec.download(f, ws.strftime(FMT), f["EndTime"], raw)
    share = (fe - ws) / (fe - fb) if fe > fb else 1
    expected = file_kb * 1024 * share
    if file_kb:
        verdict = "YES - it sent only the requested piece" if got < 0.6 * file_kb * 1024 else \
                  "NO - it sent (most of) the whole file"
        print(f"Partial download honored? {verdict} (expected ~{expected / 1048576:.1f} MB for the piece)")
    if shutil.which("ffprobe"):
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=codec_name,width,height", "-of", "compact=p=0", raw],
                              capture_output=True, text=True).stdout.strip()
        print("Video:", info or "(ffprobe could not read it)")
        mp4 = os.path.join(desk, "hovercam_clip.mp4")
        tag = ["-tag:v", "hvc1"] if "hevc" in info else []
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-fflags", "+genpts", "-i", raw, "-c", "copy",
                        *tag, mp4])
        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            print("Playable copy:", mp4)
            subprocess.run(["open", mp4])
    print("Done. Nothing was changed on the recorder.")


if __name__ == "__main__":
    main()
