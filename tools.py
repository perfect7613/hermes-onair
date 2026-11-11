"""The agent's tools. Each entry is the schema the model reads and the one-line call it maps to.

Twelve tools, each a noun with an ``action``, rather than one tool per OBS request: the model
picks correctly from a short list, and every tool returns the same shape (a JSON object, with
``error`` and ``hint`` on failure) so it can recover by itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .core.app import App
from .core.errors import OnAirError
from .core.stage import EFFECTS, SCENE_KINDS, STYLES
from .core.store import MARK_KINDS
from .core.studio import OUTPUTS, PLACEMENTS, SOURCE_KINDS

logger = logging.getLogger(__name__)

TOOLSET = "onair"
AppProvider = Callable[[], App]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    run: Callable[[App, dict[str, Any]], dict[str, Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.properties, "required": list(self.required)}}


def _str(description: str, enum: tuple[str, ...] = ()) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        spec["enum"] = list(enum)
    return spec


_PLACEMENT = _str("Where on the canvas. Default: fullscreen for pages, video and captures; center for images and text; bottom-right for a camera.", PLACEMENTS)
_SCALE = {"type": "number", "description": "Size for corner/center placements as a fraction of the canvas (0.05-1). Default 0.25."}
_SCENE = _str("Scene name. Default: the scene currently on air.")

TOOLS: tuple[Tool, ...] = (
    Tool("live_status",
         "Read the state of OBS in one call: scenes, the scene on air and its sources, whether it is "
         "recording / streaming / buffering replays, and the current live session. Call this first "
         "when asked to do anything in OBS, to learn the real scene and source names.",
         {}, (), lambda app, a: app.status()),

    Tool("live_scene",
         "Manage OBS scenes: list them, switch the scene on air, create an empty scene, rename or remove one. "
         "To create a scene that already looks good (starting soon, be right back, custom animation) use live_design_scene instead.",
         {"action": _str("What to do.", ("list", "switch", "create", "rename", "remove")),
          "name": _str("The scene to act on."),
          "new_name": _str("New name, for rename.")},
         ("action",), lambda app, a: app.scene(a["action"], a.get("name", ""), a.get("new_name", ""))),

    Tool("live_source",
         "Manage the sources inside an OBS scene: add a browser page, image or GIF file, video, text, colour block, "
         "screen capture, window capture or camera; show or hide a source; move it; update its url/file/text; remove it. "
         "Adding a source that already exists updates it instead of duplicating it. "
         "For a GIF from a web link use kind 'browser' with 'url'; for a GIF file on disk use kind 'image' with 'file'.",
         {"action": _str("What to do.", ("list", "add", "update", "show", "hide", "place", "remove")),
          "name": _str("Source name."),
          "scene": _SCENE,
          "kind": _str("Source type, for add.", tuple(SOURCE_KINDS)),
          "url": _str("Web address, for browser sources."),
          "file": _str("Absolute path of a local file, for image / media / browser sources."),
          "text": _str("The text, for text sources."),
          "color": _str("Colour as #RRGGBB, for color and text sources."),
          "placement": _PLACEMENT, "scale": _SCALE,
          "delete": {"type": "boolean", "description": "With remove: delete the source from OBS entirely, not just from this scene."}},
         ("action",),
         lambda app, a: app.source(a["action"], a.get("name", ""), scene=a.get("scene", ""), kind=a.get("kind", ""),
                                   url=a.get("url", ""), file=a.get("file", ""), text=a.get("text", ""),
                                   color=a.get("color", ""), placement=a.get("placement", ""),
                                   scale=a.get("scale"), delete=bool(a.get("delete")))),

    Tool("live_design_scene",
         "Create a finished, animated full-screen scene in OBS in one step, or restyle one you made earlier. "
         "Pick a 'kind' (starting-soon with a countdown, brb, ending, intermission, or a plain title) and an animated "
         "'style' (aurora, synthwave, matrix, starfield, confetti, bounce), with your own title, colours and an optional "
         "image or GIF. For anything the built-in styles cannot do, pass 'html': a complete self-contained HTML document "
         "(inline CSS/JS, any animation you like) and it becomes the scene. "
         "To build a scene from media another tool made, pass 'background': the path of an image, GIF or video file "
         "(from image_generate, video_generate, a rendered animation, a download) fills the screen, videos loop, and "
         "'title' is drawn across the lower third. With any scene, 'voiceover' (an audio file, e.g. from text_to_speech) "
         "is spoken once each time the scene goes on air and 'music' loops while it is on air. "
         "The scene is not put on air unless switch is true.",
         {"name": _str("Name of the scene to create or restyle, e.g. 'Starting Soon'."),
          "kind": _str("What the scene is for. Sets sensible default text.", tuple(SCENE_KINDS)),
          "style": _str("Animated background.", STYLES),
          "title": _str("Main text. Defaults to the kind's own (required for kind 'title')."),
          "subtitle": _str("Smaller line under the title."),
          "accent": _str("Accent colour as #RRGGBB."),
          "countdown": {"type": "integer", "description": "Countdown in seconds shown under the title. 0 for none."},
          "image": _str("Optional image or GIF shown above the title: a web link or an absolute file path."),
          "html": _str("A complete HTML document to use instead of the built-in designs."),
          "background": _str("Absolute path of an image, GIF or video file to use as the whole scene instead of a built-in design."),
          "voiceover": _str("Absolute path of an audio file spoken once whenever the scene goes on air."),
          "music": _str("Absolute path of an audio file looped while the scene is on air."),
          "switch": {"type": "boolean", "description": "Put the scene on air right away. Default false."}},
         ("name",),
         lambda app, a: app.design(
             a["name"], kind=a.get("kind") or "title", html=a.get("html", ""), switch=bool(a.get("switch")),
             background=a.get("background", ""), voiceover=a.get("voiceover", ""), music=a.get("music", ""),
             **({} if a.get("html") else {"title": a.get("title", "")} if a.get("background") else {
                 "style": a.get("style", ""), "title": a.get("title", ""), "subtitle": a.get("subtitle"),
                 "accent": a.get("accent", ""), "countdown": a.get("countdown"), "image": a.get("image", "")}))),

    Tool("live_output",
         "Start or stop OBS outputs: the recording, the stream, the replay buffer, the virtual camera. "
         "Also pause/resume the recording, or 'save' the replay buffer. Starting or stopping a stream asks the user first.",
         {"action": _str("What to do.", ("start", "stop", "pause", "resume", "save")),
          "output": _str("Which output.", OUTPUTS)},
         ("action", "output"), lambda app, a: app.output(a["action"], a["output"])),

    Tool("live_overlay",
         "Put something on the On Air overlay that viewers see: a short answer 'card' (two lines at most), a 'toast', "
         "a full-screen 'chapter' title, an 'effect' (confetti, fireworks, emoji rain), or a 'sticker' (an image or GIF "
         "popped in a corner for a few seconds). Everything hides itself after a few seconds. 'play' plays a 'file' once "
         "in the scene on air: an audio file (a spoken line from text_to_speech, a sound effect) or a video clip, which "
         "disappears when it ends. 'hide' clears the overlay now; 'install' adds the overlay to the scenes if it is missing.",
         {"action": _str("What to show.", ("card", "toast", "thinking", "chapter", "effect", "sticker", "play", "hide", "install")),
          "text": _str("The text for card, toast, thinking or chapter. Keep cards under 140 characters."),
          "title": _str("Small label above a card (default 'Hermes') or above a chapter title."),
          "effect": _str("Which effect.", EFFECTS),
          "emoji": _str("The emoji to rain, for effect 'emoji'."),
          "image": _str("Web link or absolute file path of the image/GIF, for sticker."),
          "file": _str("Absolute path of the audio or video file, for play."),
          "placement": _str("Sticker or clip position. Stickers default to bottom-right, clips to fullscreen.",
                            ("top-left", "top-right", "bottom-left", "bottom-right", "center", "fullscreen")),
          "scale": _SCALE,
          "seconds": {"type": "number", "description": "How long it stays on screen."}},
         ("action",),
         lambda app, a: app.overlay(a["action"], text=a.get("text", ""), title=a.get("title", ""),
                                    effect=a.get("effect") or "confetti", emoji=a.get("emoji", ""),
                                    image=a.get("image", ""), file=a.get("file", ""),
                                    placement=a.get("placement", ""), scale=a.get("scale"), seconds=a.get("seconds"))),

    Tool("live_screenshot",
         "Save a screenshot of the scene on air (or one named source) to a JPEG file and return its path, "
         "so you can look at it with your vision tools. Respects the user's capture pause and allowed-source list.",
         {"source": _str("Scene or source to capture. Default: the scene on air.")},
         (), lambda app, a: app.screenshot(a.get("source", ""))),

    Tool("live_ask",
         "Answer a question about what is on the user's screen right now ('what is this error?'). Takes a screenshot "
         "through OBS, reads it, returns the answer, and shows a two-line version on the overlay for viewers.",
         {"question": _str("The question about the screen."),
          "source": _str("Scene or source to look at. Default: the scene on air.")},
         ("question",), lambda app, a: app.ask(a["question"], a.get("source", ""))),

    Tool("live_mark",
         "Mark this moment of the recording or stream as a highlight. Saves the last seconds as a clip, a thumbnail, "
         "your note and the timestamp; marks become the highlight reel and the YouTube chapters. Mark when something "
         "worth rewatching just happened: a bug found, a fix landing, a result appearing. Failing and then passing "
         "commands are marked automatically, so do not mark those yourself.",
         {"note": _str("What just happened, in under ten words. Becomes the chapter title."),
          "kind": _str("Default manual. Use chapter for a new topic, ask for a question you answered.", MARK_KINDS)},
         (), lambda app, a: app.mark(a.get("note", ""), a.get("kind") or "manual")),

    Tool("live_session",
         "Start or stop a live session. 'start' begins collecting marks (and the recording, with record=true). "
         "'stop' ends it and returns the wrap-up: a highlight reel of the marked clips, chapter timestamps for the "
         "full recording, and a draft title and description. For guidance on directing a session, load the skill "
         "with skill_view('onair:director').",
         {"action": _str("What to do.", ("start", "stop", "status")),
          "title": _str("Working title for the session, for start."),
          "record": {"type": "boolean", "description": "Also start the OBS recording. Default false."}},
         ("action",), lambda app, a: _session(app, a)),

    Tool("live_reel",
         "Build (or rebuild) the highlight reel for the latest session from its marks, after including or excluding "
         "some of them or editing their notes. Returns the path of the new video. The original recording is never changed.",
         {"exclude": {"type": "array", "items": {"type": "integer"}, "description": "Mark ids to leave out."},
          "include": {"type": "array", "items": {"type": "integer"}, "description": "Mark ids to put back in."},
          "notes": {"type": "object", "description": "Mark id -> new note (the chapter card text).",
                    "additionalProperties": {"type": "string"}}},
         (), lambda app, a: _reel(app, a)),

    Tool("live_obs_request",
         "Escape hatch: send any obs-websocket v5 request by name with its data and get the raw response, for "
         "anything the other tools do not cover (filters, transitions, audio levels, hotkeys, profiles). "
         "Requests that change something ask the user first.",
         {"request": _str("The obs-websocket request type, e.g. GetInputList or SetInputVolume."),
          "data": {"type": "object", "description": "The request's fields."}},
         ("request",), lambda app, a: app.obs_request(a["request"], a.get("data") or {})),
)

TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def _session(app: App, args: dict[str, Any]) -> dict[str, Any]:
    action = args["action"]
    if action == "start":
        return app.start_session(args.get("title", ""), bool(args.get("record")))
    if action == "stop":
        return app.stop_session()
    session = app.director.current()
    return {"session": app.session_summary(session), "active": session is not None}


def _reel(app: App, args: dict[str, Any]) -> dict[str, Any]:
    for mark_id in args.get("exclude") or []:
        app.update_mark(int(mark_id), included=False)
    for mark_id in args.get("include") or []:
        app.update_mark(int(mark_id), included=True)
    for mark_id, note in (args.get("notes") or {}).items():
        app.update_mark(int(mark_id), note=str(note))
    return app.build_reel()


def handler(tool: Tool, app: AppProvider) -> Callable[..., str]:
    """Wrap a tool as a Hermes handler: JSON string out, never an exception."""

    def handle(args: dict[str, Any], **_context: Any) -> str:
        try:
            missing = [name for name in tool.required if args.get(name) in (None, "")]
            if missing:
                return json.dumps({"error": f"Missing required argument: {', '.join(missing)}"})
            return json.dumps({"ok": True, **tool.run(app(), args or {})}, default=str)
        except OnAirError as exc:
            return json.dumps(exc.to_dict())
        except Exception as exc:  # a tool must never raise into the agent loop
            logger.exception("%s failed", tool.name)
            return json.dumps({"error": f"{tool.name} failed unexpectedly: {exc}"})

    return handle


def register(ctx: Any, app: AppProvider) -> None:
    for tool in TOOLS:
        ctx.register_tool(name=tool.name, toolset=TOOLSET, schema=tool.schema, handler=handler(tool, app))
