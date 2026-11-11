"""Which agent actions need a human yes first.

The rule: the agent never changes what an audience sees, and never ends something that cannot be
resumed, without asking. Overlay cards and marks are exempt; putting those on screen is the
product. ``review`` is pure so the policy can be read and tested in one place.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Requests that only read state are always allowed through the raw escape hatch.
_RAW_READ_PREFIXES = ("Get",)


def review(tool: str, args: dict[str, Any], is_streaming: Callable[[], bool]) -> Optional[str]:
    """Return the question to put to the user, or ``None`` when the call may proceed."""
    action = str(args.get("action") or "")

    if tool == "live_output":
        output = str(args.get("output") or "")
        if output == "stream":
            return ("Go live now? This starts the stream to your audience." if action == "start"
                    else "End the stream? Viewers will be disconnected.") if action in ("start", "stop") else None
        if output == "record" and action == "stop":
            return "Stop the recording?"
        if output == "virtual_cam" and action == "start":
            return "Start the virtual camera? Apps using it will show your OBS output."
        return None

    if tool == "live_session" and action == "stop":
        return "End the live session? This stops the recording and drafts the wrap-up."

    if tool == "live_obs_request":
        request = str(args.get("request") or "")
        return None if request.startswith(_RAW_READ_PREFIXES) else f"Send the raw OBS request '{request}'?"

    if tool == "live_scene":
        if action == "remove":
            return f"Delete the scene '{args.get('name', '')}' from OBS?"
        if action in ("switch", "rename") and is_streaming():
            return f"You are live. Switch viewers to scene '{args.get('name', '')}'?" if action == "switch" \
                else "You are live. Rename this scene?"
        return None

    if tool == "live_source":
        if action == "remove" and args.get("delete"):
            return f"Delete the source '{args.get('name', '')}' from OBS entirely?"
        if action in ("add", "remove", "show", "hide", "place", "update") and is_streaming():
            return f"You are live. Change source '{args.get('name', '')}' ({action}) on stream?"
        return None

    if tool == "live_design_scene" and args.get("switch") and is_streaming():
        return f"You are live. Switch viewers to the new scene '{args.get('name', '')}'?"

    return None
