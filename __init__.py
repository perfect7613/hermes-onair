"""On Air: Hermes inside your OBS.

``register`` only wires things up. Nothing connects to OBS, opens the database or starts a thread
until a tool, command or the Desktop pane is first used.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    from . import commands, rpc, tools
    from .core.app import App
    from .hermes_host import HermesHost

    lock = threading.Lock()
    holder: dict[str, App] = {}

    def app() -> App:
        """The one application instance for this process, built on first use."""
        with lock:
            if "app" not in holder:
                holder["app"] = App(HermesHost(ctx))
            return holder["app"]

    def unload() -> None:
        with lock:
            instance = holder.pop("app", None)
        if instance is not None:
            instance.close()

    def before_tool(tool_name: str = "", args: Optional[dict] = None, **_kwargs: Any) -> Optional[dict]:
        """Ask the user before the agent does anything an audience would see."""
        if tool_name not in tools.TOOL_NAMES:
            return None
        args = args or {}
        question = app().needs_approval(tool_name, args)
        if not question:
            return None
        scope = ":".join(str(args.get(key, "")) for key in ("action", "output") if args.get(key))
        return {"action": "approve", "message": question, "rule_key": f"onair:{tool_name}:{scope}"}

    def after_tool(tool_name: str = "", args: Optional[dict] = None, result: str = "",
                   conversation_id: str = "", task_id: str = "", **_kwargs: Any) -> None:
        """Feed terminal commands to the automatic marker (fail, then fix)."""
        if tool_name != "terminal":
            return
        try:
            exit_code = json.loads(result).get("exit_code")
        except (ValueError, AttributeError, TypeError):
            return
        command = str((args or {}).get("command") or "")
        app().observe_command(conversation_id or task_id or "default", command,
                              exit_code if isinstance(exit_code, int) else None)

    tools.register(ctx, app)
    commands.register(ctx, app)
    rpc.register(ctx, app)
    ctx.register_hook("pre_tool_call", before_tool)
    ctx.register_hook("post_tool_call", after_tool)
    ctx.register_skill("director", Path(__file__).parent / "skills" / "director" / "SKILL.md")
    ctx.on_unload(unload)
