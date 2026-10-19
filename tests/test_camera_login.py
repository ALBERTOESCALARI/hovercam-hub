"""The hub tries camera logins carefully: the working one first, failures not retried."""

import asyncio
import types

import pytest

pytest.importorskip("aiohttp")

from hub import nvr_settings  # noqa: E402
from hub.agent import Agent  # noqa: E402
from hub.recorder import RecorderError, sofia_hash  # noqa: E402
from tests.test_nvr_settings import FakeRecorder, widget  # noqa: E402


class PickyCamera(FakeRecorder):
    def __init__(self, password):
        super().__init__()
        self.cfg = {"ChannelTitle": ["CAM"], "AVEnc.VideoWidget": [widget("CAM")]}
        self.good, self.logins = sofia_hash(password), 0

    def reply(self, msgid, body):
        if msgid == 1000:
            self.logins += 1
            if body["PassWord"] != self.good:
                return {"Ret": 203}
        return super().reply(msgid, body)


def test_tries_the_recorders_login_then_remembers_and_never_hammers():
    cam = PickyCamera("right")
    hub = types.SimpleNamespace(_cam_logins={"127.0.0.1": [("admin", "wrong"), ("admin", "right")]},
                                _cam_ok={}, _cam_failed={})
    with_camera = Agent._with_camera.__get__(hub)

    async def go():
        server = await asyncio.start_server(cam.handle, "127.0.0.1", 0)
        import hub.recorder as rec
        old, rec.PORT = rec.PORT, server.sockets[0].getsockname()[1]
        try:
            first = await with_camera("127.0.0.1", nvr_settings.read_camera_overlay)
            second = await with_camera("127.0.0.1", nvr_settings.read_camera_overlay)
            hub._cam_logins["127.0.0.1"] = [("admin", "wrong")]
            with pytest.raises(RecorderError):
                await with_camera("127.0.0.1", nvr_settings.read_camera_overlay)
            return first, second
        finally:
            rec.PORT = old
            server.close()

    first, second = asyncio.run(go())
    assert first == second == {"show_title": True, "show_time": True}
    assert hub._cam_ok["127.0.0.1"] == ("admin", "right")
    assert cam.logins == 3   # wrong once, right twice; the known-bad login is not retried
    assert len(hub._cam_failed) == 1
