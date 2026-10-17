import stat

from hub.state import HubState, StateStore


def test_state_roundtrip_is_private(tmp_path):
    store = StateStore(tmp_path)
    store.save(HubState(hub_id="h1", hub_secret="s", cameras={"cam_1": "rtsp://u:p@x/1"}))
    loaded = store.load()
    assert loaded.cameras == {"cam_1": "rtsp://u:p@x/1"}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
