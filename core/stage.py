"""What the plugin puts on screen: the overlay and designed scenes.

Both are local HTML files handed to OBS as browser sources. They live in the plugin's data
directory (which survives plugin updates) so the paths OBS remembers never go stale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import InvalidInput, OnAirError
from .studio import Studio

OVERLAY_SOURCE = "On Air Overlay"
SCENE_SOURCE_PREFIX = "On Air Scene · "
OVERLAY_EVENT = "onair"

EFFECTS = ("confetti", "emoji", "fireworks")
STYLES = ("aurora", "synthwave", "matrix", "starfield", "confetti", "bounce")

# Scene kinds: the copy a streamer would otherwise type by hand.
SCENE_KINDS: dict[str, dict[str, Any]] = {
    "starting-soon": {"kicker": "Stream", "title": "Starting soon", "subtitle": "Grab a drink. We begin shortly.",
                      "countdown": 300, "style": "aurora"},
    "brb": {"kicker": "Paused", "title": "Be right back", "subtitle": "", "countdown": 0, "style": "bounce"},
    "ending": {"kicker": "That's a wrap", "title": "Thanks for watching", "subtitle": "See you next time.",
               "countdown": 0, "style": "starfield"},
    "intermission": {"kicker": "Intermission", "title": "Back in a moment", "subtitle": "", "countdown": 120,
                     "style": "synthwave"},
    "title": {"kicker": "", "title": "", "subtitle": "", "countdown": 0, "style": "aurora"},
}

MAX_CUSTOM_HTML_BYTES = 512 * 1024
_CONFIG_PLACEHOLDER = '"__ONAIR_CONFIG__"'
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "scene"


def media_url(reference: str) -> str:
    """A browser-loadable URL for an http(s) link or a local file path."""
    reference = reference.strip()
    if reference.startswith(("http://", "https://", "file://", "data:image/")):
        return reference
    path = Path(reference).expanduser()
    if not path.is_file():
        raise InvalidInput(f"'{reference}' is not a web link or an existing file.")
    return path.resolve().as_uri()


def scene_config(kind: str, *, style: str = "", title: str = "", subtitle: Optional[str] = None,
                 accent: str = "", countdown: Optional[int] = None, image: str = "") -> dict[str, Any]:
    """Validated configuration for a designed scene: the kind's defaults, overridden by the caller."""
    if kind not in SCENE_KINDS:
        raise InvalidInput(f"Unknown scene kind '{kind}'. Use one of: {', '.join(SCENE_KINDS)}")
    config = dict(SCENE_KINDS[kind])
    if style:
        if style not in STYLES:
            raise InvalidInput(f"Unknown style '{style}'. Use one of: {', '.join(STYLES)}")
        config["style"] = style
    if title:
        config["title"] = title.strip()
    if subtitle is not None:
        config["subtitle"] = subtitle.strip()
    if countdown is not None:
        config["countdown"] = max(0, min(int(countdown), 6 * 3600))
    if accent:
        if not _COLOR.match(accent):
            raise InvalidInput(f"'{accent}' is not a colour. Use #RRGGBB.")
        config["accent"] = accent
    if image:
        config["image"] = media_url(image)
    if not config["title"]:
        raise InvalidInput("This scene needs a title.")
    config["kind"] = kind
    return config


def render_scene(template: str, config: dict[str, Any]) -> str:
    """Inline ``config`` into the scene template."""
    if _CONFIG_PLACEHOLDER not in template:
        raise OnAirError("The scene template is damaged (no config placeholder).")
    # "</" must not appear inside a <script> block; JSON allows the escaped form.
    return template.replace(_CONFIG_PLACEHOLDER, json.dumps(config).replace("</", "<\\/"), 1)


class Stage:
    def __init__(self, studio: Studio, assets_dir: Path, data_dir: Callable[[], Path],
                 accent: Callable[[], str] = lambda: "") -> None:
        self._studio = studio
        self._assets = assets_dir
        self._data_dir = data_dir
        self._accent = accent

    # ------------------------------------------------------------------ overlay

    def install_overlay(self, scenes: Optional[list[str]] = None) -> dict[str, Any]:
        """Add the overlay to ``scenes`` (default: every scene that is not a designed full-screen
        scene). Safe to repeat: existing overlay items are left where they are."""
        path = self._publish("overlay/overlay.html", (self._assets / "overlay.html").read_text("utf-8"))
        canvas = self._studio.canvas()
        settings = {"is_local_file": True, "local_file": str(path), "width": canvas[0], "height": canvas[1],
                    "shutdown": False, "restart_when_active": False}
        targets = scenes if scenes is not None else [
            scene for scene in self._studio.scenes()
            if not any(src["name"].startswith(SCENE_SOURCE_PREFIX) for src in self._studio.sources(scene))]
        for scene in targets:
            self._studio.add_source(scene, OVERLAY_SOURCE, "browser", settings, placement="fullscreen")
        return {"source": OVERLAY_SOURCE, "scenes": targets, "file": str(path)}

    def overlay_installed(self) -> bool:
        try:
            return any(src["name"] == OVERLAY_SOURCE for src in self._studio.sources())
        except OnAirError:
            return False

    def card(self, body: str, title: str = "Hermes", ttl: float = 12) -> None:
        self._send(type="card", title=title, body=_clip(body, 220), ttl=ttl)

    def thinking(self, body: str = "thinking", title: str = "Hermes") -> None:
        self._send(type="thinking", title=title, body=_clip(body, 80))

    def toast(self, body: str, tone: str = "", ttl: float = 4) -> None:
        self._send(type="toast", body=_clip(body, 90), tone=tone, ttl=ttl)

    def chapter(self, title: str, kicker: str = "Chapter") -> None:
        self._send(type="chapter", title=_clip(title, 60), kicker=_clip(kicker, 30))

    def effect(self, name: str = "confetti", emoji: str = "", ttl: float = 4) -> None:
        if name not in EFFECTS:
            raise InvalidInput(f"Unknown effect '{name}'. Use one of: {', '.join(EFFECTS)}")
        self._send(type="effect", name=name, emoji=emoji[:8], ttl=ttl)

    def sticker(self, reference: str, placement: str = "bottom-right", scale: float = 0.25, ttl: float = 6) -> None:
        """Pop an image or GIF (web link or local file) on screen for ``ttl`` seconds."""
        self._send(type="sticker", url=media_url(reference), placement=placement, scale=scale, ttl=ttl)

    def hide(self) -> None:
        self._send(type="hide")

    def _send(self, **message: Any) -> None:
        accent = self._accent()
        if accent:
            message["accent"] = accent
        self._studio.emit_browser_event(OVERLAY_EVENT, message)

    # ------------------------------------------------------------------ designed scenes

    def design_scene(self, name: str, *, kind: str = "title", html: str = "", switch: bool = False,
                     **options: Any) -> dict[str, Any]:
        """Create (or restyle) scene ``name`` as one full-canvas animated page.

        Either a built-in ``kind`` + ``style``, or the caller's own ``html`` document."""
        name = name.strip()
        if not name:
            raise InvalidInput("The scene needs a name.")
        if html:
            if len(html.encode("utf-8")) > MAX_CUSTOM_HTML_BYTES:
                raise InvalidInput("Custom scene HTML is limited to 512 KB.")
            document, described = html, {"kind": "custom"}
        else:
            described = scene_config(kind, **options)
            document = render_scene((self._assets / "scene.html").read_text("utf-8"), described)
        path = self._publish(f"scenes/{slugify(name)}.html", document)
        canvas = self._studio.canvas()
        source = SCENE_SOURCE_PREFIX + name
        created = self._studio.create_scene(name)
        self._studio.add_source(name, source, "browser", {
            "is_local_file": True, "local_file": str(path), "width": canvas[0], "height": canvas[1],
            "restart_when_active": True}, placement="fullscreen", on_top=False)
        if not created:  # same path, new content: tell the browser source to reload it
            try:
                self._studio.raw("PressInputPropertiesButton", {"inputName": source, "propertyName": "refreshnocache"})
            except OnAirError:
                pass
        if switch:
            self._studio.switch_scene(name)
        return {"scene": name, "created": created, "switched": switch, "file": str(path), **described}

    def _publish(self, relative: str, content: str) -> Path:
        path = self._data_dir() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text("utf-8") != content:
            path.write_text(content, "utf-8")
        return path


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
