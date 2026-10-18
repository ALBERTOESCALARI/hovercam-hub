"""Tiny local web page (http://<hub-ip>:8099) showing pairing state, so people
without a terminal can read the pairing code."""

import html

from aiohttp import web

from hub.state import HubState


def make_app(get_state, playback_handler=None) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        state: HubState = get_state()
        if state.paired:
            body = f"<h1>Paired</h1><p>{len(state.cameras)} camera(s) on this hub.</p>"
        elif state.pairing_code:
            body = (
                "<h1>Pair this hub</h1><p>Open the HoverCam app &rarr; Add hub, and enter:</p>"
                f"<p style='font-size:48px;font-family:monospace'>"
                f"{html.escape(state.pairing_code)}</p>"
            )
        else:
            body = "<h1>Starting&hellip;</h1>"
        page = (
            "<!doctype html><meta name=viewport content='width=device-width'>"
            "<meta http-equiv=refresh content=10><title>HoverCam Hub</title>"
            f"<body style='font-family:system-ui;padding:24px'>{body}</body>"
        )
        return web.Response(text=page, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", index)
    if playback_handler is not None:
        # Local-only MPEG-TS feed of recorder video that go2rtc pulls for playback.
        app.router.add_get("/playback/{key}", playback_handler)
    return app
