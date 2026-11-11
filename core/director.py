"""The live session: start, mark, stop.

A mark is written to the database first and decorated afterwards (clip, thumbnail, recording
chapter, on-screen toast). Every decoration is best effort, so a mark survives OBS being closed,
the replay buffer being off, or the overlay missing.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import InvalidInput, OnAirError
from .obs import ObsClient
from .stage import Stage
from .store import Mark, Session, Store
from .studio import Studio

logger = logging.getLogger(__name__)

THUMB_WIDTH = 480
CLIP_WAIT_S = 3.0
_TOAST = {"fail": ("Marked · it broke", "fail"), "fix": ("Marked · fixed", "fix")}

Notify = Callable[[str, dict[str, Any]], None]


class Director:
    def __init__(self, client: ObsClient, studio: Studio, stage: Stage, store: Store,
                 data_dir: Callable[[], Path], notify: Notify = lambda event, payload: None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._studio = studio
        self._stage = stage
        self._store = store
        self._data_dir = data_dir
        self._notify = notify
        self._sleep = sleep
        self._attached = False
        self._attach_lock = threading.Lock()

    # ------------------------------------------------------------------ session lifecycle

    def start(self, title: str = "", *, record: bool = False) -> dict[str, Any]:
        """Open a session. It opens even when OBS is unreachable; ``warnings`` says what is missing."""
        session = self._store.start_session(title)
        self._attach()
        warnings: list[str] = []
        recording = False
        try:
            self._studio.ensure_replay_buffer()
        except OnAirError as exc:
            warnings.append(f"No replay buffer, so marks will not save clips ({exc}). {exc.hint}".strip())
        if record:
            try:
                recording = self._studio.set_output("record", True)["running"]
            except OnAirError as exc:
                warnings.append(f"Recording did not start ({exc}).")
        if not self._stage.overlay_installed():
            warnings.append("The overlay is not in the current scene. Run /live setup to add it.")
        self._notify("changed", {"reason": "session.start"})
        return {"session": session.to_dict(), "recording_started": recording, "warnings": warnings}

    def stop(self, *, stop_recording: bool = True) -> dict[str, Any]:
        """Close the session. Recording is stopped (so the file is final); a stream is left alone."""
        session = self._store.active_session()
        if session is None:
            raise InvalidInput("No live session is running.", hint="Start one with /live start.")
        recording_path, warnings = "", []
        try:
            status = self._studio.status()
            if stop_recording and status["recording"]["active"]:
                recording_path = self._studio.set_output("record", False).get("path", "")
            if status["streaming"]["active"]:
                warnings.append("You are still streaming. The stream was left running.")
        except OnAirError as exc:
            warnings.append(f"Could not reach OBS to stop the recording ({exc}).")
        session = self._store.end_session(session.id, recording_path)
        self._client.hold(False)
        self._notify("changed", {"reason": "session.stop"})
        return {"session": session.to_dict(), "marks": [m.to_dict() for m in self._store.marks(session.id)],
                "warnings": warnings}

    def current(self) -> Optional[Session]:
        session = self._store.active_session()
        if session is not None:
            self._attach()  # a restarted process picks the session's event stream back up
        return session

    # ------------------------------------------------------------------ marks

    def mark(self, note: str = "", kind: str = "manual") -> Mark:
        """Record a moment now. Starts a session implicitly if none is running."""
        session = self._store.active_session() or self._store.start_session("")
        self._attach()
        position, replay_running, reachable = None, False, True
        try:
            status = self._studio.status()
            if status["recording"]["active"]:
                position = status["recording"]["duration_ms"]
            replay_running = status["replay_buffer"]["active"]
        except OnAirError:
            reachable = False
        mark = self._store.add_mark(session.id, kind, note, position_ms=position, expects_clip=replay_running)
        if reachable:
            self._decorate(mark, save_clip=replay_running)
        self._notify("changed", {"reason": "mark", "mark_id": mark.id})
        return self._store.update_mark(mark.id) or mark

    def _decorate(self, mark: Mark, *, save_clip: bool) -> None:
        self._best_effort(lambda: self._save_thumbnail(mark))
        if save_clip:
            self._best_effort(lambda: self._save_clip(mark))
        self._best_effort(lambda: self._studio.add_record_chapter(mark.note or mark.kind))
        label, tone = _TOAST.get(mark.kind, ("Marked", ""))
        text = f"{label} · {mark.note}" if mark.note and mark.kind not in _TOAST else label
        self._best_effort(lambda: self._stage.toast(text, tone))

    def _save_thumbnail(self, mark: Mark) -> None:
        image = self._studio.screenshot(width=THUMB_WIDTH, quality=70)
        folder = self._data_dir() / "sessions" / str(mark.session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"mark-{mark.id}.jpg"
        path.write_bytes(image)
        self._store.update_mark(mark.id, thumb_path=str(path))

    def _save_clip(self, mark: Mark) -> None:
        """Ask OBS for the replay and wait briefly for the file name. The event listener may
        claim the same file first; ``claim_clip`` makes both paths agree."""
        before = self._studio.last_replay_path()
        self._studio.save_replay()
        waited = 0.0
        while waited < CLIP_WAIT_S:
            self._sleep(0.25)
            waited += 0.25
            path = self._studio.last_replay_path()
            if path and path != before:
                self._store.claim_clip(mark.session_id, path, from_hotkey=False)
                return

    # ------------------------------------------------------------------ OBS events

    def _attach(self) -> None:
        """Listen for replay saves and recording stops, and keep the connection alive. Once."""
        with self._attach_lock:
            if not self._attached:
                self._client.subscribe("ReplayBufferSaved", self._on_replay_saved)
                self._client.subscribe("RecordStateChanged", self._on_record_state)
                self._attached = True
        self._client.hold(True)

    def _on_replay_saved(self, event: dict[str, Any]) -> None:
        session = self._store.active_session()
        if session is None:
            return
        mark = self._store.claim_clip(session.id, event.get("savedReplayPath", ""), from_hotkey=True)
        if mark and mark.kind == "hotkey" and not mark.thumb_path:
            # The user pressed OBS's own Save Replay hotkey: that is a mark too.
            self._best_effort(lambda: self._save_thumbnail(mark))
            self._best_effort(lambda: self._stage.toast("Marked"))
        self._notify("changed", {"reason": "clip"})

    def _on_record_state(self, event: dict[str, Any]) -> None:
        if event.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED" and event.get("outputPath"):
            session = self._store.active_session() or self._store.latest_session()
            if session is not None and not session.recording_path:
                self._store.update_session(session.id, recording_path=event["outputPath"])
        self._notify("changed", {"reason": "record"})

    @staticmethod
    def _best_effort(step: Callable[[], Any]) -> None:
        try:
            step()
        except OnAirError as exc:
            logger.info("Skipped a mark decoration: %s", exc)
        except OSError as exc:
            logger.warning("Could not write mark file: %s", exc)
