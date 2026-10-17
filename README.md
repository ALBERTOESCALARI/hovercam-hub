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
go2rtc -config go2rtc.yaml &
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
