"""Sessions and marks, in SQLite.

Several Hermes processes (the gateway, a CLI agent, the Desktop backend) share this database, so
it is the single source of truth: nothing about a session lives only in memory. The one piece of
real logic here is ``claim_clip``, which pairs a replay file OBS just saved with the mark that
asked for it, exactly once, whichever process sees the file first.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Optional

MARK_KINDS = ("manual", "hotkey", "fail", "fix", "ask", "chapter")

# A mark waits this long for its replay file before it is recorded as having none.
CLIP_CLAIM_WINDOW_S = 30.0

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL DEFAULT '',
    started_at     REAL    NOT NULL,
    ended_at       REAL,
    recording_path TEXT    NOT NULL DEFAULT '',
    wrapup         TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at  REAL    NOT NULL,
    kind        TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    position_ms INTEGER,
    clip_state  TEXT    NOT NULL DEFAULT 'none',
    clip_path   TEXT    UNIQUE,
    thumb_path  TEXT    NOT NULL DEFAULT '',
    included    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS marks_by_session ON marks(session_id, created_at);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(ended_at);
"""


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    title: str
    started_at: float
    ended_at: Optional[float]
    recording_path: str
    wrapup: dict[str, Any]

    @property
    def active(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "active": self.active}


@dataclass(frozen=True, slots=True)
class Mark:
    id: int
    session_id: int
    created_at: float
    kind: str
    note: str
    position_ms: Optional[int]  # offset into the recording, when one was running
    clip_state: str             # none | pending | saved
    clip_path: str
    thumb_path: str
    included: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SESSION_COLUMNS = "id, title, started_at, ended_at, recording_path, wrapup"
_MARK_COLUMNS = ("id, session_id, created_at, kind, note, position_ms, clip_state, clip_path, "
                 "thumb_path, included")


def _session(row: tuple) -> Session:
    try:
        wrapup = json.loads(row[5]) if row[5] else {}
    except ValueError:
        wrapup = {}
    return Session(row[0], row[1], row[2], row[3], row[4], wrapup)


def _mark(row: tuple) -> Mark:
    return Mark(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7] or "", row[8], bool(row[9]))


class Store:
    def __init__(self, connect: Callable[[], sqlite3.Connection],
                 clock: Callable[[], float] = time.time) -> None:
        self._connect = connect
        self._clock = clock
        self._db: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ sessions

    def start_session(self, title: str = "") -> Session:
        """Open a session. Any session left open (a crash, a forgotten stop) is closed first."""
        now = self._clock()
        with self._write() as db:
            db.execute("UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL", (now,))
            cursor = db.execute("INSERT INTO sessions (title, started_at) VALUES (?, ?)", (title.strip(), now))
            return self._get_session(db, cursor.lastrowid)

    def end_session(self, session_id: int, recording_path: str = "") -> Session:
        with self._write() as db:
            db.execute("UPDATE sessions SET ended_at = COALESCE(ended_at, ?), "
                       "recording_path = CASE WHEN ? != '' THEN ? ELSE recording_path END WHERE id = ?",
                       (self._clock(), recording_path, recording_path, session_id))
            return self._get_session(db, session_id)

    def active_session(self) -> Optional[Session]:
        with self._read() as db:
            row = db.execute(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE ended_at IS NULL "
                             "ORDER BY id DESC LIMIT 1").fetchone()
        return _session(row) if row else None

    def latest_session(self) -> Optional[Session]:
        with self._read() as db:
            row = db.execute(f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return _session(row) if row else None

    def session(self, session_id: int) -> Optional[Session]:
        with self._read() as db:
            row = db.execute(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _session(row) if row else None

    def update_session(self, session_id: int, *, title: Optional[str] = None,
                       recording_path: Optional[str] = None, wrapup: Optional[dict[str, Any]] = None) -> None:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title.strip()
        if recording_path is not None:
            fields["recording_path"] = recording_path
        if wrapup is not None:
            fields["wrapup"] = json.dumps(wrapup)
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self._write() as db:
            db.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id))

    # ------------------------------------------------------------------ marks

    def add_mark(self, session_id: int, kind: str, note: str = "", *, position_ms: Optional[int] = None,
                 expects_clip: bool = False) -> Mark:
        if kind not in MARK_KINDS:
            raise ValueError(f"unknown mark kind: {kind}")
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO marks (session_id, created_at, kind, note, position_ms, clip_state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, self._clock(), kind, note.strip(), position_ms,
                 "pending" if expects_clip else "none"))
            return self._get_mark(db, cursor.lastrowid)

    def claim_clip(self, session_id: int, clip_path: str, *, from_hotkey: bool) -> Optional[Mark]:
        """Record that OBS saved the replay ``clip_path`` and return the mark that now owns it.

        The file goes to the oldest mark still waiting for one. When no mark is waiting and the
        save came from OBS itself (``from_hotkey``: the user pressed the Save Replay hotkey) a new
        mark is created for it. A path that is already owned is a no-op, which is what makes it
        safe for every connected process to report the same save.
        """
        if not clip_path:
            return None
        now = self._clock()
        with self._write() as db:
            owner = db.execute(f"SELECT {_MARK_COLUMNS} FROM marks WHERE clip_path = ?", (clip_path,)).fetchone()
            if owner:
                return _mark(owner)
            db.execute("UPDATE marks SET clip_state = 'none' WHERE session_id = ? AND clip_state = 'pending' "
                       "AND created_at < ?", (session_id, now - CLIP_CLAIM_WINDOW_S))
            waiting = db.execute("SELECT id FROM marks WHERE session_id = ? AND clip_state = 'pending' "
                                 "ORDER BY id LIMIT 1", (session_id,)).fetchone()
            if waiting:
                mark_id = waiting[0]
                db.execute("UPDATE marks SET clip_state = 'saved', clip_path = ? WHERE id = ?",
                           (clip_path, mark_id))
            elif from_hotkey:
                mark_id = db.execute(
                    "INSERT INTO marks (session_id, created_at, kind, note, clip_state, clip_path) "
                    "VALUES (?, ?, 'hotkey', 'Replay saved', 'saved', ?)",
                    (session_id, now, clip_path)).lastrowid
            else:
                return None
            return self._get_mark(db, mark_id)

    def update_mark(self, mark_id: int, *, note: Optional[str] = None, included: Optional[bool] = None,
                    thumb_path: Optional[str] = None, position_ms: Optional[int] = None) -> Optional[Mark]:
        fields: dict[str, Any] = {}
        if note is not None:
            fields["note"] = note.strip()
        if included is not None:
            fields["included"] = int(included)
        if thumb_path is not None:
            fields["thumb_path"] = thumb_path
        if position_ms is not None:
            fields["position_ms"] = position_ms
        with self._write() as db:
            if fields:
                assignments = ", ".join(f"{column} = ?" for column in fields)
                db.execute(f"UPDATE marks SET {assignments} WHERE id = ?", (*fields.values(), mark_id))
            row = db.execute(f"SELECT {_MARK_COLUMNS} FROM marks WHERE id = ?", (mark_id,)).fetchone()
        return _mark(row) if row else None

    def delete_mark(self, mark_id: int) -> bool:
        with self._write() as db:
            return db.execute("DELETE FROM marks WHERE id = ?", (mark_id,)).rowcount > 0

    def marks(self, session_id: int) -> list[Mark]:
        with self._read() as db:
            rows = db.execute(f"SELECT {_MARK_COLUMNS} FROM marks WHERE session_id = ? ORDER BY created_at, id",
                              (session_id,)).fetchall()
        return [_mark(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ plumbing

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            db = self._connect()
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            if db.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                db.executescript(_SCHEMA)
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                db.commit()
            self._db = db
        return self._db

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._open()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction: the write lock is taken up front, so a read-then-write
        sequence inside it cannot interleave with another process."""
        with self._lock:
            db = self._open()
            if db.in_transaction:
                db.commit()
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            db.commit()

    @staticmethod
    def _get_session(db: sqlite3.Connection, session_id: Any) -> Session:
        return _session(db.execute(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",
                                   (session_id,)).fetchone())

    @staticmethod
    def _get_mark(db: sqlite3.Connection, mark_id: Any) -> Mark:
        return _mark(db.execute(f"SELECT {_MARK_COLUMNS} FROM marks WHERE id = ?", (mark_id,)).fetchone())
