"""What the plugin puts on screen: the overlay, designed scenes and media scenes.

The overlay and designed scenes are local HTML files handed to OBS as browser sources. Media
scenes are built from files other tools made (a generated image, a GIF, a rendered video, a
spoken line) with OBS's own image and media sources. Everything OBS is pointed at lives in the
plugin's data directory, which survives plugin updates and cache clean-ups, so the paths OBS
remembers never go stale.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import InvalidInput, NotFound, OnAirError
from .studio import Studio

OVERLAY_SOURCE = "On Air Overlay"
SCENE_SOURCE_PREFIX = "On Air Scene · "
OVERLAY_EVENT = "onair"

# Media scenes: one source per role, named after the scene, so building a scene twice updates it.
IMAGE_SOURCE_PREFIX = "On Air Image · "
VIDEO_SOURCE_PREFIX = "On Air Video · "
TITLE_SOURCE_PREFIX = "On Air Title · "
VOICE_SOURCE_PREFIX = "On Air Voice · "
MUSIC_SOURCE_PREFIX = "On Air Music · "
# One-shots: a single reused source each. They stop by themselves when the file ends.
SOUND_SOURCE = "On Air Sound"
CLIP_SOURCE = "On Air Clip"

MEDIA_TYPES: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tga"),
    "video": (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv", ".ts"),
    "audio": (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"),
}

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


def scene_key(name: str) -> str:
    return f"{slugify(name)[:80]}-{hashlib.sha256(name.encode()).hexdigest()[:16]}"


def media_url(reference: str) -> str:
    """A browser-loadable URL for an http(s) link or a local file path."""
    reference = reference.strip()
    if reference.startswith(("http://", "https://", "file://", "data:image/")):
        return reference
    path = Path(reference).expanduser()
    if not path.is_file():
        raise InvalidInput(f"'{reference}' is not a web link or an existing file.")
    return path.resolve().as_uri()


def media_file(reference: str, role: str, allowed: tuple[str, ...]) -> tuple[Path, str]:
    """The existing local file ``reference`` names and its media type, which must be in ``allowed``."""
    reference = reference.strip()
    if reference.startswith("file://"):
        reference = reference[len("file://"):]
    if reference.startswith(("http://", "https://")):
        raise InvalidInput(f"The {role} must be a file on this computer, not a web link.",
                           hint="Save the file first, or add a web page or web GIF with live_source kind 'browser'.")
    path = Path(reference).expanduser()
    if not path.is_file():
        raise InvalidInput(f"The {role} file '{reference}' does not exist.",
                           hint="Pass the absolute path the tool that made the file returned.")
    suffix = path.suffix.lower()
    kind = next((name for name, suffixes in MEDIA_TYPES.items() if suffix in suffixes), "")
    if kind not in allowed:
        wanted = ", ".join(ext for name in allowed for ext in MEDIA_TYPES[name])
        raise InvalidInput(f"The {role} '{path.name}' is not a supported file type.", hint=f"Use one of: {wanted}")
    return path.resolve(), kind


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

    draw_check_seconds = 4.0  # how long a healthy page gets to load and show the toast

    def overlay_draws(self) -> Optional[bool]:
        """Does OBS actually draw the overlay? Shows a two-second toast and looks for it.

        OBS can report a browser source as present and healthy while its browser engine is not
        drawing anything, and then every card and designed scene is silently blank. ``None``
        means it could not be checked (no Pillow, or OBS returned no image)."""
        try:
            from PIL import Image
        except ImportError:
            return None
        deadline = time.monotonic() + self.draw_check_seconds
        sent = 0.0
        while True:
            if time.monotonic() - sent >= 1.0:  # again each second: a page still loading misses the first
                self.toast("On Air overlay check", ttl=2)
                sent = time.monotonic()
            try:
                picture = Image.open(io.BytesIO(self._studio.screenshot(
                    OVERLAY_SOURCE, width=320, image_format="png"))).convert("RGBA")
            except (OnAirError, OSError, ValueError):
                return None
            if any(low != high for low, high in picture.getextrema()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)

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

    def design_scene(self, name: str, *, kind: str = "title", html: str = "", background: str = "",
                     voiceover: str = "", music: str = "", switch: bool = False,
                     **options: Any) -> dict[str, Any]:
        """Create (or restyle) scene ``name``.

        The picture is one of: the caller's own ``html`` document, a ``background`` file (image,
        GIF or video made by any other tool), or a built-in ``kind`` + ``style``. ``voiceover``
        (spoken once each time the scene goes on air) and ``music`` (looped while it is on air)
        are audio files and work with all three."""
        name = name.strip()
        if not name:
            raise InvalidInput("The scene needs a name.")
        if html and background:
            raise InvalidInput("Pass either 'html' or 'background', not both.")
        # Validate every file before OBS is touched, so a bad path cannot leave half a scene.
        picture = media_file(background, "background", ("image", "video")) if background else None
        voice = media_file(voiceover, "voiceover", ("audio",)) if voiceover else None
        bed = media_file(music, "music", ("audio",)) if music else None

        if picture:
            created = self._studio.create_scene(name)
            described = self._media_background(name, *picture, title=str(options.get("title") or ""))
        else:
            if html:
                if len(html.encode("utf-8")) > MAX_CUSTOM_HTML_BYTES:
                    raise InvalidInput("Custom scene HTML is limited to 512 KB.")
                document, described = html, {"kind": "custom"}
            else:
                described = scene_config(kind, **options)
                document = render_scene((self._assets / "scene.html").read_text("utf-8"), described)
            path = self._publish(f"scenes/{scene_key(name)}.html", document)
            canvas = self._studio.canvas()
            source = SCENE_SOURCE_PREFIX + name
            created = self._studio.create_scene(name)
            for prefix in (IMAGE_SOURCE_PREFIX, VIDEO_SOURCE_PREFIX, TITLE_SOURCE_PREFIX):
                self._remove_owned_source(name, prefix + name)
            item = self._studio.add_source(name, source, "browser", {
                "is_local_file": True, "local_file": str(path), "width": canvas[0], "height": canvas[1],
                "restart_when_active": True}, placement="fullscreen", on_top=False)
            self._studio.raw("SetSceneItemIndex", {"sceneName": name, "sceneItemId": item["item_id"], "sceneItemIndex": 0})
            if not created:  # same path, new content: tell the browser source to reload it
                try:
                    self._studio.raw("PressInputPropertiesButton", {"inputName": source, "propertyName": "refreshnocache"})
                except OnAirError:
                    pass
            described = {"file": str(path), **described}
        if voice:
            described["voiceover"] = self._scene_audio(name, VOICE_SOURCE_PREFIX, "voiceover", voice[0], loop=False)
        if bed:
            described["music"] = self._scene_audio(name, MUSIC_SOURCE_PREFIX, "music", bed[0], loop=True)
        if switch:
            self._studio.switch_scene(name)
        return {"scene": name, "created": created, "switched": switch, **described}

    def _media_background(self, scene: str, source_file: Path, media: str, *, title: str) -> dict[str, Any]:
        """A full-canvas image, GIF or looping video, with an optional title across the lower third."""
        path = self._import(scene, "background", source_file)
        if media == "image":
            keep, drop, kind, settings = IMAGE_SOURCE_PREFIX, VIDEO_SOURCE_PREFIX, "image", {"file": str(path)}
        else:
            keep, drop, kind = VIDEO_SOURCE_PREFIX, IMAGE_SOURCE_PREFIX, "media"
            settings = {"is_local_file": True, "local_file": str(path), "looping": True,
                        "restart_on_activate": True, "close_when_inactive": False, "clear_on_media_end": False}
        # The background changed from a picture to a video, or back. OBS deletes an input a
        # moment after it is asked to, so the old one is taken out of the scene first.
        for prefix in (drop, SCENE_SOURCE_PREFIX):
            self._remove_owned_source(scene, prefix + scene)
        item = self._studio.add_source(scene, keep + scene, kind, settings, placement="fullscreen", on_top=False)
        self._studio.raw("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": item["item_id"], "sceneItemIndex": 0})
        described: dict[str, Any] = {"kind": "media", "background": media, "file": str(path)}
        if title.strip():
            self._studio.add_source(scene, TITLE_SOURCE_PREFIX + scene, "text",
                                    {"text": _clip(title, 80), "font": {"face": "Helvetica", "size": 160},
                                     "outline": True, "drop_shadow": True},
                                    placement="lower-third")
            described["title"] = _clip(title, 80)
        else:
            self._remove_owned_source(scene, TITLE_SOURCE_PREFIX + scene)
        return described

    def _remove_owned_source(self, scene: str, source: str) -> None:
        for delete in (False, True):
            try:
                self._studio.remove_source(scene, source, delete=delete)
            except NotFound:
                break
        # RemoveInput is deferred in OBS. Do not reorder a replacement while its
        # predecessor still exists (especially during image/video/browser swaps).
        deadline = time.monotonic() + 3
        while any(item["inputName"] == source for item in
                  self._studio.raw("GetInputList").get("inputs", [])):
            if time.monotonic() >= deadline:
                raise OnAirError(f"OBS has not finished removing '{source}'. Try rebuilding the scene again.")
            time.sleep(0.05)

    def _scene_audio(self, scene: str, prefix: str, role: str, source_file: Path, *, loop: bool) -> str:
        path = self._import(scene, role, source_file)
        self._studio.add_source(scene, prefix + scene, "media", {
            "is_local_file": True, "local_file": str(path), "looping": loop,
            "restart_on_activate": True, "close_when_inactive": True, "clear_on_media_end": True}, on_top=False)
        return str(path)

    # ------------------------------------------------------------------ one-shots

    def play(self, reference: str, *, placement: str = "fullscreen", scale: Optional[float] = None) -> dict[str, Any]:
        """Play a sound or a video clip once in the scene on air. It ends by itself: the source
        goes silent, and a clip disappears, when the file finishes."""
        path, media = media_file(reference, "file", ("audio", "video"))
        path = self._import("", "sound" if media == "audio" else "clip", path)
        settings = {"is_local_file": True, "local_file": str(path), "looping": False,
                    "restart_on_activate": False, "close_when_inactive": False, "clear_on_media_end": True}
        if media == "audio":
            self._studio.add_source("", SOUND_SOURCE, "media", settings, on_top=False)
            source = SOUND_SOURCE
        else:
            self._studio.add_source("", CLIP_SOURCE, "media", settings, placement=placement, scale=scale)
            source = CLIP_SOURCE
        self._studio.restart_media(source)
        return {"playing": media, "source": source, "file": str(path)}

    def _import(self, scene: str, role: str, source_file: Path) -> Path:
        """Copy a media file into the data directory, replacing what that role held before."""
        directory = self._data_dir() / "media" / (scene_key(scene) if scene else "_oneshot")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{role}{source_file.suffix.lower()}"
        if target.resolve() == source_file:
            return target
        # Size and timestamp are not content identity: generated files can share both.
        # Replace atomically so OBS never reads a partially copied file.
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".import-", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(source_file, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        for stale in directory.glob(f"{role}.*"):
            if stale != target:
                stale.unlink(missing_ok=True)
        return target

    def _publish(self, relative: str, content: str) -> Path:
        path = self._data_dir() / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text("utf-8") != content:
            path.write_text(content, "utf-8")
        return path


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
