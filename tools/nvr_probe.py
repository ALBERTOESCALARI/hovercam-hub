"""Read-only check that the hub can talk to a Xiongmai/XMEye recorder (DVRIP, TCP 34567).
Logs in, prints model, channel names and today's recordings per channel. Changes nothing.
Usage:  python nvr_probe.py 192.168.0.103"""
import getpass, hashlib, json, socket, struct, sys
from datetime import datetime

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.103"
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sofia_hash(pw):
    d = hashlib.md5(pw.encode()).digest()
    return "".join(CHARS[(a + b) % 62] for a, b in zip(d[::2], d[1::2]))


class Recorder:
    def __init__(self, host):
        self.sock = socket.create_connection((host, 34567), timeout=10)
        self.session, self.seq = 0, 0

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("recorder closed the connection")
            buf += chunk
        return buf

    def send(self, msg, data):
        body = json.dumps(data).encode()
        self.sock.sendall(struct.pack("<BB2xII2xHI", 255, 0, self.session, self.seq, msg,
                                      len(body) + 2) + body + b"\x0a\x00")
        self.seq += 1
        _, _, _, _, _, size = struct.unpack("<BB2xII2xHI", self._recv(20))
        text = self._recv(size).rstrip(b"\x00\x0a").decode("utf-8", "replace")
        return json.loads(text) if text else {}

    def login(self, user, pw):
        r = self.send(1000, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                             "PassWord": sofia_hash(pw), "UserName": user})
        if r.get("Ret") not in (100, 515):
            raise SystemExit(f"Login failed (code {r.get('Ret')}). 203 = wrong password, "
                             "205 = account locked for a while.")
        self.session = int(r["SessionID"], 16)
        return r

    def get(self, name, code):
        return self.send(code, {"Name": name, "SessionID": "0x%08X" % self.session}).get(name)

    def files_today(self, channel):
        day = datetime.now().strftime("%Y-%m-%d")
        r = self.send(1440, {"Name": "OPFileQuery", "OPFileQuery": {
            "BeginTime": f"{day} 00:00:00", "EndTime": f"{day} 23:59:59", "Channel": channel,
            "DriverTypeMask": "0x0000FFFF", "Event": "*", "StreamType": "0x00000000",
            "Type": "h264"}})
        if r.get("Ret") != 100:
            return []
        return r.get("OPFileQuery") or []


def main():
    user = input("Recorder username [admin]: ").strip() or "admin"
    pw = getpass.getpass("Recorder password (hidden): ")
    rec = Recorder(HOST)
    rec.login(user, pw)
    print(f"\nLOGIN OK to {HOST}")
    info = rec.get("SystemInfo", 1020) or {}
    print("Model:", info.get("DeviceModel") or info.get("HardWare"),
          "| Firmware:", info.get("SoftWareVersion"),
          "| Channels:", info.get("VideoInChannel"))
    titles = rec.get("ChannelTitle", 1048) or []
    count = int(info.get("VideoInChannel") or len(titles) or 8)
    print("\nChannel  Name                 Recordings today")
    for ch in range(count):
        files = rec.files_today(ch)
        span = f"{files[0]['BeginTime'][11:16]}-{files[-1]['EndTime'][11:16]}" if files else ""
        name = titles[ch] if ch < len(titles) else "?"
        print(f"{ch + 1:>7}  {name:<20} {len(files):>3} files  {span}")
    print("\nDone. Nothing was changed on the recorder.")


if __name__ == "__main__":
    main()
