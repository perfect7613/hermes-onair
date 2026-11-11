"""The Hermes adapter: the only module that imports Hermes.

Everything the application needs from its host (settings, the OBS password, storage, the model,
events to the Desktop pane) goes through here, using Hermes's public plugin surfaces only.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .core.app import Settings

logger = logging.getLogger(__name__)

PLUGIN_ID = "onair"
PASSWORD_ENV = "ONAIR_OBS_PASSWORD"


class HermesHost:
    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._data_dir: Optional[Path] = None
        self._password = ""

    def settings(self) -> Settings:
        get = self._ctx.get_config
        defaults = Settings()
        sources = get("capture_sources", default=[]) or []
        if isinstance(sources, str):
            sources = [part.strip() for part in sources.split(",")]
        return Settings(
            host=str(get("obs_host", default=defaults.host) or defaults.host),
            port=_int(get("obs_port", default=defaults.port), defaults.port),
            accent=str(get("accent", default=defaults.accent) or defaults.accent),
            auto_marks=bool(get("auto_marks", default=defaults.auto_marks)),
            confirm_public_actions=bool(get("confirm_public_actions", default=defaults.confirm_public_actions)),
            clip_seconds=min(max(_int(get("clip_seconds", default=defaults.clip_seconds), defaults.clip_seconds), 3), 120),
            replay_seconds=min(max(_int(get("replay_seconds", default=defaults.replay_seconds), defaults.replay_seconds), 5), 600),
            capture_sources=tuple(str(name) for name in sources if str(name).strip()),
        )

    def obs_password(self) -> str:
        """The password from Hermes's secret store. Background threads (the reconnect loop) may
        run outside a secret scope; they reuse the last value a scoped call resolved."""
        try:
            from agent.secret_scope import get_secret

            self._password = get_secret(PASSWORD_ENV) or ""
        except Exception:
            logger.debug("Secret scope unavailable on this thread; reusing the cached OBS password")
        return self._password

    def data_dir(self) -> Path:
        if self._data_dir is None:
            from plugins.plugin_storage import plugin_data_dir

            self._data_dir = plugin_data_dir(PLUGIN_ID)
        return self._data_dir

    def connect_db(self) -> sqlite3.Connection:
        from plugins.plugin_storage import plugin_db

        self.data_dir()  # resolve the profile's data directory on a scoped thread first
        return plugin_db(PLUGIN_ID)

    def ask_model(self, instructions: str, text: str, image: Optional[bytes] = None) -> str:
        """One call to the user's own model through Hermes. Screenshots go nowhere else."""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image:
            blocks.append({"type": "image", "data": image, "mime_type": "image/jpeg"})
        result = self._ctx.llm.complete_structured(
            instructions=instructions, input=blocks, json_mode=True, max_tokens=700, timeout=60,
            purpose="onair")
        return result.text or ""

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        """Tell the Desktop pane something changed. Losing one is harmless: the pane also polls."""
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event

            broadcast_plugin_event(PLUGIN_ID, event, payload)
        except Exception:
            logger.debug("Could not broadcast %s", event, exc_info=True)

    def get_state(self, key: str, default: Any = None) -> Any:
        try:
            return self._ctx.state.get(key, default=default)
        except Exception:
            return default

    def set_state(self, key: str, value: Any) -> None:
        self._ctx.state.set(key, value)


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
