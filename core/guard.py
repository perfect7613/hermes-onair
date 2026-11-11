"""Which agent actions need a human yes first.

The rule: the agent never changes what an audience sees, and never ends something that cannot be
resumed, without asking. Overlay cards and marks are exempt; putting those on screen is the
product. Sounds and clips are not: an audience hears those, so playing one while live asks first.

``safety`` covers a second kind of question, which is asked even when the user has switched the
audience prompts off: anything that runs a local or model-written page in OBS's browser (page
script there can read local files and reach the network), and raw OBS requests that change
something. Both functions are pure so the policy can be read and tested in one place.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

# Requests that only read state are always allowed through the raw escape hatch.
_RAW_READ_PREFIXES = ("Get",)
_RAW_DATA_SHOWN = 300


def safety(tool: str, args: dict[str, Any], source_kind: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """Questions that are always asked, whatever the audience-prompt setting says.

    ``source_kind`` returns the OBS kind of an existing source (``""`` when it cannot be told).
    Pointing a source at a local file asks unless that source is known not to be a browser: the
    file's name says nothing about what a browser would do with it."""
    action = str(args.get("action") or "")

    if tool == "live_obs_request":
        request = str(args.get("request") or "")
        if request.startswith(_RAW_READ_PREFIXES):
            return None
        data = args.get("data") or {}
        try:
            shown = json.dumps(data, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            shown = str(data)
        if len(shown) > _RAW_DATA_SHOWN:
            shown = shown[:_RAW_DATA_SHOWN] + "… (truncated)"
        return f"Send the raw OBS request '{request}' with data {shown}?"

    if tool == "live_source":
        file = str(args.get("file") or "")
        if action == "add" and str(args.get("kind") or "") == "browser" and file:
            return (f"Open the local file '{file}' in OBS's browser as source '{args.get('name', '')}'? "
                    "A page there can run scripts, read local files and use the network.")
        if action == "update" and file:
            kind = source_kind(str(args.get("name") or "")) if source_kind else ""
            if not kind or "browser" in kind:
                return (f"Open the local file '{file}' in OBS's browser through source '{args.get('name', '')}'? "
                        "A page there can run scripts, read local files and use the network.")

    if tool == "live_design_scene" and args.get("html"):
        return (f"Run a page written by the agent in OBS's browser as scene '{args.get('name', '')}'? "
                "Its script can read local files and use the network.")

    return None


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

    if tool == "live_overlay" and action == "play" and is_streaming():
        return "You are live. Play this sound or clip to your viewers?"

    if tool == "live_design_scene" and is_streaming():
        return f"You are live. Create or restyle scene '{args.get('name', '')}'? This can change a scene already on air."

    return None
