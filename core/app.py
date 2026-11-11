"""The application: every operation On Air offers, once.

Tools, slash commands and the Desktop pane are three thin skins over this class, so each
behaviour (capture privacy, the wrap-up, what "setup" means) is decided in one place. Results
are plain dicts; failures are ``OnAirError`` with a hint.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import doctor, guard
from .automark import AutoMarker
from .director import Director
from .errors import InvalidInput, NotFound, OnAirError
from .obs import Connector, Endpoint, ObsClient, websocket_connector
from .reel import ReelBuilder, draft_wrapup, plan_reel
from .stage import OVERLAY_SOURCE, Stage, media_file
from .store import MARK_KINDS, Session, Store
from .studio import Studio, source_settings

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
ASK_IMAGE_WIDTH = 1568
PREVIEW_WIDTH = 960
# Where a new source lands when the caller does not say: backgrounds fill the canvas, the rest do not.
_DEFAULT_PLACEMENT = {"image": "center", "text": "center", "camera": "bottom-right"}
MAX_THUMBS = 60
MAX_SCREENSHOTS = 50

_ASK_INSTRUCTIONS = (
    "You are looking at a screenshot of the user's screen, taken from their OBS output while they "
    "record or stream. Answer their question about it directly and specifically. "
    "Reply with JSON only: {\"answer\": \"the full answer, a few sentences at most\", "
    "\"card\": \"the answer in at most two short lines, under 140 characters, for an on-screen card\"}.")


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 4455
    accent: str = "#ffb000"
    auto_marks: bool = True
    confirm_public_actions: bool = True
    clip_seconds: int = 20
    replay_seconds: int = 30
    capture_sources: tuple[str, ...] = ()


class Host(Protocol):
    """What the application needs from whatever is hosting it (Hermes in production)."""

    def settings(self) -> Settings: ...
    def obs_password(self) -> str: ...
    def data_dir(self) -> Path: ...
    def connect_db(self) -> sqlite3.Connection: ...
    def ask_model(self, instructions: str, text: str, image: Optional[bytes] = None) -> str: ...
    def notify(self, event: str, payload: dict[str, Any]) -> None: ...
    def get_state(self, key: str, default: Any = None) -> Any: ...
    def set_state(self, key: str, value: Any) -> None: ...


class App:
    def __init__(self, host: Host, connector: Connector = websocket_connector,
                 reel: Optional[ReelBuilder] = None) -> None:
        self.host = host
        self.client = ObsClient(self._endpoint, connector)
        self.studio = Studio(self.client)
        self.stage = Stage(self.studio, ASSETS_DIR, host.data_dir, lambda: host.settings().accent)
        self.store = Store(host.connect_db)
        self.director = Director(self.client, self.studio, self.stage, self.store, host.data_dir, host.notify)
        self.reel_builder = reel or ReelBuilder()
        self._automarker = AutoMarker()
        self._background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="onair-bg")
        self._reel_lock = threading.Lock()

    def close(self) -> None:
        self._background.shutdown(wait=False, cancel_futures=True)
        self.client.close()
        self.store.close()

    def _endpoint(self) -> Endpoint:
        settings = self.host.settings()
        return Endpoint(settings.host, settings.port, self.host.obs_password())

    # ------------------------------------------------------------------ status and setup

    def status(self) -> dict[str, Any]:
        """OBS, the scene on air and its sources, and the live session."""
        status = self.studio.status()
        status["sources"] = self.studio.sources(status["scene"]) if status["scene"] else []
        status["overlay_installed"] = any(source["name"] == OVERLAY_SOURCE for source in status["sources"])
        status["session"] = self.session_summary(self.director.current())
        status["capture_paused"] = self.capture_paused()
        return status

    def doctor(self) -> list[doctor.Check]:
        return doctor.run_checks(self.client, self.studio, self.stage, self.reel_builder, self._endpoint())

    def setup(self) -> dict[str, Any]:
        """Everything that used to be manual OBS clicking: overlay into the scenes, replay buffer on."""
        steps: list[str] = []
        overlay = self.stage.install_overlay()
        steps.append(f"Overlay added to {len(overlay['scenes'])} scene(s).")
        try:
            replay = self.studio.ensure_replay_buffer(self.host.settings().replay_seconds)
            steps.append("Replay buffer switched on in your OBS profile and started." if replay["enabled_in_profile"]
                         else "Replay buffer is running.")
        except OnAirError as exc:
            steps.append(f"Replay buffer could not be started: {exc} {exc.hint}".strip())
        try:
            self.stage.toast("On Air is connected", ttl=5)
        except OnAirError:
            pass
        return {"steps": steps, "checks": [check.to_dict() for check in self.doctor()]}

    # ------------------------------------------------------------------ scenes, sources, outputs

    def scene(self, action: str, name: str = "", new_name: str = "") -> dict[str, Any]:
        if action == "list":
            return {"scenes": self.studio.scenes(), "current": self.studio.current_scene()}
        if not name:
            raise InvalidInput("Which scene? Pass 'name'.")
        if action == "switch":
            self.studio.switch_scene(name)
        elif action == "create":
            return {"scene": name, "created": self.studio.create_scene(name)}
        elif action == "remove":
            self.studio.remove_scene(name)
        elif action == "rename":
            if not new_name:
                raise InvalidInput("Rename needs 'new_name'.")
            self.studio.rename_scene(name, new_name)
        else:
            raise InvalidInput(f"Unknown scene action '{action}'. Use list, switch, create, remove or rename.")
        self.host.notify("changed", {"reason": "scene"})
        return {"scene": new_name or name, "action": action, "ok": True}

    def source(self, action: str, name: str = "", *, scene: str = "", kind: str = "", url: str = "",
               file: str = "", text: str = "", color: str = "", placement: str = "",
               scale: Optional[float] = None, delete: bool = False) -> dict[str, Any]:
        if action == "list":
            return {"scene": scene or self.studio.current_scene(), "sources": self.studio.sources(scene)}
        if not name:
            raise InvalidInput("Which source? Pass 'name'.")
        if action == "add":
            if not kind:
                raise InvalidInput("Adding a source needs 'kind' (browser, image, media, text, color, screen, window or camera).")
            if file:
                file = str(_existing_file(file))
            settings = source_settings(kind, canvas=self.studio.canvas(), url=url, file=file, text=text, color=color)
            result = self.studio.add_source(scene, name, kind, settings, scale=scale,
                                            placement=placement or _DEFAULT_PLACEMENT.get(kind, "fullscreen"))
        elif action == "update":
            settings = {key: value for key, value in (("url", url), ("text", text), ("file", file), ("local_file", file)) if value}
            if not settings:
                raise InvalidInput("Update needs 'url', 'file' or 'text'.")
            self.studio.update_source(name, settings)
            result = {"source": name, "updated": sorted(settings)}
        elif action in ("show", "hide"):
            self.studio.set_visible(scene, name, action == "show")
            result = {"source": name, "visible": action == "show"}
        elif action == "place":
            if not placement:
                raise InvalidInput("Place needs 'placement'.")
            self.studio.place_source(scene, name, placement, scale)
            result = {"source": name, "placement": placement}
        elif action == "remove":
            self.studio.remove_source(scene, name, delete=delete)
            result = {"source": name, "removed": True, "deleted_from_obs": delete}
        else:
            raise InvalidInput(f"Unknown source action '{action}'. Use list, add, update, show, hide, place or remove.")
        self.host.notify("changed", {"reason": "source"})
        return result

    def output(self, action: str, output: str) -> dict[str, Any]:
        if action in ("start", "stop"):
            result = self.studio.set_output(output, action == "start")
        elif action in ("pause", "resume") and output == "record":
            self.studio.pause_recording(action == "pause")
            result = {"output": "record", "paused": action == "pause"}
        elif action == "save" and output == "replay_buffer":
            return {"mark": self.director.mark("Replay saved").to_dict()}
        else:
            raise InvalidInput(f"Cannot '{action}' the {output or 'output'}. Use start or stop (pause/resume for record).")
        self.host.notify("changed", {"reason": "output"})
        return result

    def design(self, name: str, **options: Any) -> dict[str, Any]:
        result = self.stage.design_scene(name, **options)
        self.host.notify("changed", {"reason": "scene"})
        return result

    def overlay(self, action: str, *, text: str = "", title: str = "", effect: str = "confetti", emoji: str = "",
                image: str = "", file: str = "", placement: str = "", scale: Optional[float] = None,
                seconds: Optional[float] = None) -> dict[str, Any]:
        stage = self.stage
        if action == "card":
            stage.card(_required(text, "text"), title or "Hermes", seconds or 12)
        elif action == "toast":
            stage.toast(_required(text, "text"), ttl=seconds or 4)
        elif action == "thinking":
            stage.thinking(text or "thinking", title or "Hermes")
        elif action == "chapter":
            stage.chapter(_required(text, "text"), title or "Chapter")
        elif action == "effect":
            stage.effect(effect, emoji, seconds or 4)
        elif action == "sticker":
            stage.sticker(_required(image, "image"), placement or "bottom-right", scale or 0.25, seconds or 6)
        elif action == "play":
            reference = _required(file or image, "file")
            path, media = media_file(reference, "file", ("image", "audio", "video"))
            if media == "image":
                path = stage._import("", "sticker", path)
                stage.sticker(str(path), placement or "bottom-right", scale or 0.25, seconds or 6)
                return {"shown": "sticker", "overlay_in_scene": self.stage.overlay_installed()}
            return stage.play(reference, placement=placement or "fullscreen", scale=scale)
        elif action == "hide":
            stage.hide()
        elif action == "install":
            return self.stage.install_overlay()
        else:
            raise InvalidInput(f"Unknown overlay action '{action}'. Use card, toast, thinking, chapter, effect, sticker, play, hide or install.")
        return {"shown": action, "overlay_in_scene": self.stage.overlay_installed()}

    def obs_request(self, request: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {"request": request, "response": self.studio.raw(_required(request, "request"), data)}

    def needs_approval(self, tool: str, args: dict[str, Any]) -> Optional[str]:
        """The question to ask the user before the agent runs ``tool``, if any."""
        if not self.host.settings().confirm_public_actions:
            return None

        def streaming() -> bool:
            try:
                return self.studio.is_streaming()
            except OnAirError:
                return False  # OBS is unreachable: the call will fail by itself, nothing to protect

        return guard.review(tool, args, streaming)

    # ------------------------------------------------------------------ seeing the screen

    def capture_paused(self) -> bool:
        return bool(self.host.get_state("capture_paused", False))

    def set_capture_paused(self, paused: bool) -> dict[str, Any]:
        self.host.set_state("capture_paused", bool(paused))
        self.host.notify("changed", {"reason": "capture"})
        return {"capture_paused": bool(paused)}

    def capture(self, source: str = "", *, width: int = 0) -> tuple[bytes, str]:
        """A JPEG of ``source`` for the model, subject to the user's privacy settings.

        Returns ``(image, source name)``. Thumbnails and the Desktop preview do not go through
        here: they stay on this machine."""
        if self.capture_paused():
            raise InvalidInput("Screen capture is paused.", hint="Resume with /live resume.")
        allowed = self.host.settings().capture_sources
        if allowed:
            if not source:
                source = allowed[0]
            elif source not in allowed:
                raise InvalidInput(f"'{source}' is not on your capture list.",
                                   hint=f"Allowed: {', '.join(allowed)}. Change it in Settings → Plugins → On Air.")
        source = source or self.studio.current_scene()
        return self.studio.screenshot(source, width=width), source

    def screenshot(self, source: str = "") -> dict[str, Any]:
        image, name = self.capture(source)
        folder = self.host.data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        path.write_bytes(image)
        for old in sorted(folder.glob("shot-*.jpg"))[:-MAX_SCREENSHOTS]:  # keep the folder from growing forever
            old.unlink(missing_ok=True)
        return {"path": str(path), "source": name, "bytes": len(image)}

    def ask(self, question: str, source: str = "") -> dict[str, Any]:
        """Look at the screen, answer ``question``, and put the short answer on the overlay."""
        question = _required(question, "question").strip()
        image, name = self.capture(source, width=ASK_IMAGE_WIDTH)
        self._quietly(lambda: self.stage.thinking("looking at the screen"))
        try:
            reply = self.host.ask_model(_ASK_INSTRUCTIONS, question, image)
        except Exception as exc:
            self._quietly(self.stage.hide)
            raise OnAirError(f"The model could not answer: {exc}",
                             hint="Check that your Hermes model accepts images.") from exc
        answer, card = _parse_answer(reply)
        on_screen = self._quietly(lambda: self.stage.card(card, "Hermes", 14))
        return {"answer": answer, "card": card, "source": name, "shown_on_overlay": on_screen}

    def preview(self) -> dict[str, Any]:
        """The scene on air as a data URL, for the Desktop pane."""
        image = self.studio.screenshot(width=PREVIEW_WIDTH, quality=60)
        return {"image": "data:image/jpeg;base64," + base64.b64encode(image).decode()}

    # ------------------------------------------------------------------ sessions, marks, wrap-up

    def start_session(self, title: str = "", record: bool = False) -> dict[str, Any]:
        return self.director.start(title, record=record)

    def stop_session(self, *, build_reel: bool = True) -> dict[str, Any]:
        """Stop, then hand back the wrap-up: chapters, title, description, and the reel if it can be built."""
        result = self.director.stop()
        session = self.store.session(result["session"]["id"])
        assert session is not None
        result["wrapup"] = self._write_wrapup(session)
        if build_reel:
            try:
                result["reel"] = self.build_reel(session.id)
            except OnAirError as exc:
                result["reel"] = exc.to_dict()
        return result

    def mark(self, note: str = "", kind: str = "manual") -> dict[str, Any]:
        if kind not in MARK_KINDS:
            raise InvalidInput(f"Unknown mark kind '{kind}'. Use one of: {', '.join(MARK_KINDS)}")
        mark = self.director.mark(note, kind)
        return {"mark": mark.to_dict(), "clip": mark.clip_state == "saved",
                "note": "" if mark.clip_state == "saved" else
                "Saved without a clip (replay buffer off or OBS unreachable). It can still be cut from the recording."}

    def update_mark(self, mark_id: int, *, note: Optional[str] = None, included: Optional[bool] = None,
                    delete: bool = False) -> dict[str, Any]:
        if delete:
            if not self.store.delete_mark(mark_id):
                raise NotFound(f"There is no mark {mark_id}.")
            self.host.notify("changed", {"reason": "mark"})
            return {"deleted": mark_id}
        mark = self.store.update_mark(mark_id, note=note, included=included)
        if mark is None:
            raise NotFound(f"There is no mark {mark_id}.")
        self.host.notify("changed", {"reason": "mark"})
        return {"mark": mark.to_dict()}

    def build_reel(self, session_id: Optional[int] = None) -> dict[str, Any]:
        """Render the highlight reel for a session (default: the latest) from its included marks."""
        session = self.store.session(session_id) if session_id else self.store.latest_session()
        if session is None:
            raise NotFound("There is no session to build a reel from.", hint="Start one with /live start.")
        settings = self.host.settings()
        segments, skipped = plan_reel(self.store.marks(session.id), session.recording_path,
                                      clip_seconds=settings.clip_seconds)
        folder = self.host.data_dir() / "sessions" / str(session.id)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"reel-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        with self._reel_lock:  # one render at a time; FFmpeg already uses every core
            self.reel_builder.build(segments, out, title=session.wrapup.get("title") or session.title,
                                    accent=settings.accent)
        wrapup = {**session.wrapup, "reel": str(out)}
        self.store.update_session(session.id, wrapup=wrapup)
        self.host.notify("changed", {"reason": "reel"})
        return {"path": str(out), "clips": len(segments), "skipped_marks": skipped, "draft": True}

    def build_reel_in_background(self, session_id: Optional[int] = None) -> None:
        def job() -> None:
            try:
                self.host.notify("reel", {"ok": True, **self.build_reel(session_id)})
            except OnAirError as exc:
                self.host.notify("reel", {"ok": False, **exc.to_dict()})
            except Exception as exc:  # a crashed render must still tell the pane it ended
                logger.exception("Reel build failed")
                self.host.notify("reel", {"ok": False, "error": str(exc)})

        self._background.submit(job)

    def snapshot(self) -> dict[str, Any]:
        """Everything the Desktop pane shows, in one call. Never raises for an unreachable OBS."""
        session = self.director.current() or self.store.latest_session()
        marks = self.store.marks(session.id) if session else []
        body: dict[str, Any] = {
            "session": self.session_summary(session),
            "marks": [{**mark.to_dict(), "thumb": _thumb(mark.thumb_path) if index >= len(marks) - MAX_THUMBS else ""}
                      for index, mark in enumerate(marks)],
            "capture_paused": self.capture_paused(),
            "reel_available": self.reel_builder.available(),
        }
        try:
            body["obs"] = {"connected": True, **self.status()}
        except OnAirError as exc:
            body["obs"] = {"connected": False, **exc.to_dict()}
        return body

    # ------------------------------------------------------------------ automatic marks

    def observe_command(self, conversation: str, command: str, exit_code: Optional[int]) -> None:
        """Called after every terminal command the agent runs. Cheap unless a mark is due."""
        if not self.host.settings().auto_marks or self.store.active_session() is None:
            return
        intent = self._automarker.observe(conversation, command, exit_code)
        if intent is not None:
            # Off the agent's thread: saving the clip waits on OBS for up to a few seconds.
            self._background.submit(self._safe_mark, intent.note, intent.kind)

    def _safe_mark(self, note: str, kind: str) -> None:
        try:
            self.director.mark(note, kind)
        except Exception:
            logger.exception("Automatic mark failed")

    # ------------------------------------------------------------------ helpers

    def _write_wrapup(self, session: Session) -> dict[str, Any]:
        marks = self.store.marks(session.id)
        wrapup = draft_wrapup(session, marks, lambda prompt: self.host.ask_model(
            "You write plain, accurate video titles and descriptions.", prompt))
        wrapup["recording"] = session.recording_path
        self.store.update_session(session.id, wrapup=wrapup)
        folder = self.host.data_dir() / "sessions" / str(session.id)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "wrapup.md").write_text(
                f"# {wrapup['title']}\n\n{wrapup['description']}\n\n## Chapters\n\n{wrapup['chapters'] or '(no recording positions)'}"
                f"\n\n## Show notes\n\n{wrapup['show_notes'] or '(no notes)'}\n", "utf-8")
            wrapup["file"] = str(folder / "wrapup.md")
        except OSError as exc:
            logger.warning("Could not write wrapup.md: %s", exc)
        return wrapup

    def session_summary(self, session: Optional[Session]) -> Optional[dict[str, Any]]:
        """A session with its mark counts, without the marks themselves."""
        if session is None:
            return None
        marks = self.store.marks(session.id)
        return {**session.to_dict(), "mark_count": len(marks), "clips": sum(m.clip_state == "saved" for m in marks)}

    @staticmethod
    def _quietly(step: Callable[[], Any]) -> bool:
        try:
            step()
            return True
        except OnAirError:
            return False


def _required(value: str, name: str) -> str:
    if not value or not str(value).strip():
        raise InvalidInput(f"'{name}' is required.")
    return value


def _existing_file(reference: str) -> Path:
    path = Path(reference).expanduser()
    if not path.is_file():
        raise InvalidInput(f"There is no file at '{reference}'.")
    return path.resolve()


def _parse_answer(reply: str) -> tuple[str, str]:
    """Pull ``answer`` and ``card`` out of the model's reply; fall back to the raw text."""
    import json

    text = (reply or "").strip()
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        answer = str(data.get("answer") or "").strip()
        card = str(data.get("card") or "").strip()
    except ValueError:
        answer, card = text, ""
    answer = answer or text or "I could not read the screen."
    return answer, (card or answer)[:160]


def _thumb(path: str) -> str:
    if not path:
        return ""
    try:
        return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except OSError:
        return ""
