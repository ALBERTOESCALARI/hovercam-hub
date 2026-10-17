# hovercam-hub

The piece of **HoverCam** that runs at home. It sits on the same network as the
cameras, keeps an outbound WebSocket to the HoverCam API (no port-forwarding), and
uses [go2rtc](https://github.com/AlexxIT/go2rtc) to turn camera RTSP streams into
WebRTC for the apps.

What it does:
- First boot: registers with the cloud and shows a **pairing code** in its log and at
  `http://<hub-ip>:8099`.
- Answers cloud commands: `discover` (ONVIF WS-Discovery scan), `camera.add` (tests
  the stream before accepting), `camera.remove`, `webrtc.offer`, `snapshot`.
- Keeps camera RTSP URLs and passwords **only** in `state/hub_state.json` (mode 0600).
- Re-adds cameras to go2rtc on restart and reconnects to the cloud with backoff.

## Run with Docker (Raspberry Pi 4/5, mini PC, NAS)

```bash
HOVERCAM_API_URL=https://<your-api> HOVERCAM_HUB_NAME="Garage Pi" docker compose up -d
docker compose logs hub      # shows the pairing code
```

Host networking is required: discovery uses LAN multicast and WebRTC uses UDP 8555.

## Run for development (Mac)

```bash
brew install go2rtc     # or download the binary from the go2rtc releases page
cp go2rtc.base.yaml go2rtc.yaml && go2rtc -config go2rtc.yaml &
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
HOVERCAM_API_URL=http://localhost:8000 python -m hub
pytest
```

Docker Desktop on macOS doesn't support host networking well, so run the hub
directly there and use Docker on Linux.

## Env vars

| Var | Default |
|---|---|
| `HOVERCAM_API_URL` | `http://127.0.0.1:8000` |
| `GO2RTC_URL` | `http://127.0.0.1:1984` |
| `HOVERCAM_STATE_DIR` | `./state` (`/data` in Docker) |
| `HOVERCAM_HUB_NAME` | `HoverCam Hub` |
| `HOVERCAM_STATUS_PORT` | `8099` |

To re-pair from scratch, stop the hub and delete `state/hub_state.json`.

## Run on Windows

1. Install Python 3.12 (tick "Add python.exe to PATH"), Git, and ffmpeg:
   `winget install Python.Python.3.12 Git.Git Gyan.FFmpeg` (then open a new terminal).
2. `git clone https://github.com/ALBERTOESCALARI/hovercam-hub.git` and `cd hovercam-hub`.
3. Download `go2rtc_win64.zip` from the go2rtc releases page; put `go2rtc.exe` in the folder.
4. To keep an existing pairing, copy `state-prod\hub_state.json` from the old machine into
   `state-prod\` here (and stop the hub there first). Otherwise it registers as a new hub.
5. Install it as a startup task (runs before sign-in, no window; logs to `hub.log`):
   `schtasks /Create /TN "HoverCam Hub" /TR "%USERPROFILE%\hovercam-hub\start-hub.bat" /SC ONSTART /DELAY 0001:00 /RU SYSTEM /RL HIGHEST /F`
   then `schtasks /Run /TN "HoverCam Hub"`. (Or double-click `start-hub.bat` to run it once.) Allow both Windows Firewall prompts on **Private** networks
   (go2rtc needs UDP 8555; the hub needs LAN multicast for camera discovery).
6. Settings -> Power: never sleep when plugged in. Run only ONE copy of a hub at a time.
