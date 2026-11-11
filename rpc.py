"""RPC methods for the Desktop pane (``plugin.onair.<name>`` over the gateway socket).

Every method answers ``{"ok": true, ...}`` or ``{"ok": false, "error", "hint"}``; the pane shows
the hint as is. Methods return quickly: the one slow job (rendering a reel) runs in the
background and reports through a ``plugin.onair.reel`` event.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .core.app import App
from .core.errors import InvalidInput, OnAirError
from .core.stage import EFFECTS, SCENE_KINDS, STYLES

logger = logging.getLogger(__name__)

AppProvider = Callable[[], App]
Method = Callable[[App, dict[str, Any]], dict[str, Any]]


def _state(app: App, _params: dict[str, Any]) -> dict[str, Any]:
    return {**app.snapshot(), "design": {"kinds": list(SCENE_KINDS), "styles": list(STYLES), "effects": list(EFFECTS)}}


def _mark_update(app: App, params: dict[str, Any]) -> dict[str, Any]:
    if "id" not in params:
        raise InvalidInput("'id' is required.")
    return app.update_mark(int(params["id"]), note=params.get("note"), included=params.get("included"),
                           delete=bool(params.get("delete")))


def _reel(app: App, params: dict[str, Any]) -> dict[str, Any]:
    app.build_reel_in_background(params.get("session_id"))
    return {"started": True}


def _design(app: App, params: dict[str, Any]) -> dict[str, Any]:
    return app.design(str(params.get("name") or ""), kind=params.get("kind") or "title", style=params.get("style") or "",
                      title=params.get("title") or "", subtitle=params.get("subtitle"), accent=params.get("accent") or "",
                      countdown=params.get("countdown"), image=params.get("image") or "", switch=bool(params.get("switch")))


def _overlay(app: App, params: dict[str, Any]) -> dict[str, Any]:
    return app.overlay(str(params.get("action") or ""), text=params.get("text") or "", title=params.get("title") or "",
                       effect=params.get("effect") or "confetti", emoji=params.get("emoji") or "",
                       image=params.get("image") or "", seconds=params.get("seconds"))


METHODS: dict[str, Method] = {
    "state": _state,
    "preview": lambda app, p: app.preview(),
    "setup": lambda app, p: app.setup(),
    "doctor": lambda app, p: {"checks": [check.to_dict() for check in app.doctor()]},
    "scene": lambda app, p: app.scene(str(p.get("action") or "switch"), str(p.get("name") or "")),
    "source": lambda app, p: app.source(str(p.get("action") or ""), str(p.get("name") or ""), scene=str(p.get("scene") or "")),
    "output": lambda app, p: app.output(str(p.get("action") or ""), str(p.get("output") or "")),
    "mark": lambda app, p: app.mark(str(p.get("note") or "")),
    "mark.update": _mark_update,
    "session.start": lambda app, p: app.start_session(str(p.get("title") or ""), bool(p.get("record"))),
    "session.stop": lambda app, p: app.stop_session(build_reel=False),
    "reel": _reel,
    "design": _design,
    "overlay": _overlay,
    "capture": lambda app, p: app.set_capture_paused(bool(p.get("paused"))),
}


def serve(method: Method, app: AppProvider) -> Callable[..., dict[str, Any]]:
    def handle(params: Any = None, **_context: Any) -> dict[str, Any]:
        try:
            return {"ok": True, **method(app(), params if isinstance(params, dict) else {})}
        except OnAirError as exc:
            return {"ok": False, **exc.to_dict()}
        except Exception as exc:
            logger.exception("RPC failed")
            return {"ok": False, "error": f"Something went wrong: {exc}"}

    return handle


def register(ctx: Any, app: AppProvider) -> None:
    for name, method in METHODS.items():
        ctx.register_rpc(name, serve(method, app))
