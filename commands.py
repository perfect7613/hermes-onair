"""Slash commands: ``/live <subcommand>`` and ``/mark``.

Commands are typed by the user, so they act immediately (no approval step) and answer in plain
text. They work from any Hermes surface, including Telegram on a phone.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .core.app import App
from .core.doctor import format_report
from .core.errors import InvalidInput, OnAirError
from .core.reel import timestamp
from .core.stage import SCENE_KINDS, STYLES

logger = logging.getLogger(__name__)

AppProvider = Callable[[], App]


@dataclass(frozen=True, slots=True)
class Sub:
    usage: str
    summary: str
    run: Callable[[App, str], str]


def _status(app: App, _rest: str) -> str:
    try:
        status = app.status()
    except OnAirError:
        return format_report(app.doctor())
    flags = [label for label, on in (("● REC", status["recording"]["active"]),
                                     ("● LIVE", status["streaming"]["active"]),
                                     ("replay buffer on", status["replay_buffer"]["active"])) if on]
    lines = [f"OBS {status['obs']['version']} · scene: {status['scene']} · {', '.join(flags) or 'idle'}",
             "Scenes: " + ", ".join(f"[{name}]" if name == status["scene"] else name for name in status["scenes"])]
    session = status["session"]
    if session:
        lines.append(f"Session '{session['title'] or 'untitled'}': {session['mark_count']} marks, {session['clips']} clips.")
    else:
        lines.append("No session running. /live start begins one.")
    if status["capture_paused"]:
        lines.append("Screen capture is paused (/live resume).")
    return "\n".join(lines)


def _setup(app: App, _rest: str) -> str:
    result = app.setup()
    failed = [check for check in result["checks"] if not check["ok"]]
    lines = [f"✓ {step}" for step in result["steps"]]
    lines += [f"✗ {check['name']}: {check['detail']}\n    Fix: {check['fix']}" for check in failed]
    lines.append("Ready. /live start begins a session; /mark saves a moment." if not failed else "Fix the items above, then run /live setup again.")
    return "\n".join(lines)


def _start(app: App, rest: str) -> str:
    words = rest.split()
    record = "--record" in words or "-r" in words
    title = " ".join(word for word in words if word not in ("--record", "-r"))
    result = app.start_session(title, record)
    lines = [f"Session started{': ' + title if title else ''}." + (" Recording." if result["recording_started"] else "")]
    lines += [f"! {warning}" for warning in result["warnings"]]
    lines.append("Use /mark <note> for moments worth keeping. /live stop wraps up.")
    return "\n".join(lines)


def _stop(app: App, _rest: str) -> str:
    result = app.stop_session()
    wrapup, reel = result["wrapup"], result.get("reel", {})
    lines = [f"Session ended with {len(result['marks'])} marks.", "", f"Title: {wrapup['title']}", wrapup["description"]]
    if wrapup["chapters"]:
        lines += ["", "Chapters (paste into YouTube):", wrapup["chapters"]]
    if reel.get("path"):
        lines += ["", f"Highlight reel (draft, {reel['clips']} clips): {reel['path']}"]
    elif reel.get("error"):
        lines += ["", f"No reel yet: {reel['error']} {reel.get('hint', '')}".rstrip()]
    if wrapup.get("recording"):
        lines.append(f"Recording (untouched): {wrapup['recording']}")
    if wrapup.get("file"):
        lines.append(f"Notes saved to {wrapup['file']}")
    lines += [f"! {warning}" for warning in result["warnings"]]
    return "\n".join(lines)


def _mark(app: App, rest: str) -> str:
    result = app.mark(rest.strip())
    mark = result["mark"]
    where = f" at {timestamp(mark['position_ms'] / 1000)}" if mark["position_ms"] is not None else ""
    return f"★ Marked{where}{': ' + mark['note'] if mark['note'] else ''}." + ("" if result["clip"] else f" {result['note']}")


def _scene(app: App, rest: str) -> str:
    name = rest.strip()
    if not name:
        listing = app.scene("list")
        return "Scenes: " + ", ".join(f"[{s}]" if s == listing["current"] else s for s in listing["scenes"]) + \
            "\nSwitch with /live scene <name>."
    match = _closest(name, app.scene("list")["scenes"])
    app.scene("switch", match)
    return f"On air: {match}"


def _output(output: str, label: str) -> Callable[[App, str], str]:
    def run(app: App, rest: str) -> str:
        words = rest.split()
        if not words:  # never start a stream because someone typed half a command
            return f"Usage: /live {SUBCOMMANDS[_KEY[output]].usage}"
        action = words[0].lower()
        result = app.output(action, output)
        if "mark" in result:
            return "★ Replay saved and marked."
        if "paused" in result:
            return f"{label} {'paused' if result['paused'] else 'resumed'}."
        state = "started" if result["running"] else "stopped"
        text = f"{label} {state}." if result["changed"] else f"{label} was already {'running' if result['running'] else 'stopped'}."
        return text + (f" Saved to {result['path']}" if result.get("path") else "")
    return run


def _design(app: App, rest: str) -> str:
    words = shlex.split(rest)
    if not words or words[0] not in SCENE_KINDS:
        return ("Usage: /live design <kind> [style] [title...]\n"
                f"Kinds: {', '.join(SCENE_KINDS)}\nStyles: {', '.join(STYLES)}\n"
                "Example: /live design starting-soon synthwave Friday build stream")
    kind, words = words[0], words[1:]
    style = words.pop(0) if words and words[0] in STYLES else ""
    title = " ".join(words)
    name = title or kind.replace("-", " ").title()
    result = app.design(name, kind=kind, style=style, title=title)
    return (f"Scene '{result['scene']}' {'created' if result['created'] else 'updated'} ({result['style']}). "
            f"Put it on air with /live scene {result['scene']}")


def _overlay(action: str, done: str, **fixed: Any) -> Callable[[App, str], str]:
    def run(app: App, rest: str) -> str:
        app.overlay(action, text=rest.strip(), **fixed)
        return done
    return run


def _celebrate(app: App, rest: str) -> str:
    words = rest.split()
    effect = words[0] if words and words[0] in ("confetti", "fireworks", "emoji") else "confetti"
    emoji = next((word for word in words if word != effect), "")
    app.overlay("effect", effect="emoji" if emoji and effect == "confetti" else effect, emoji=emoji)
    return "🎉 On screen."


def _gif(app: App, rest: str) -> str:
    if not rest.strip():
        return "Usage: /live gif <web link or file path>"
    app.overlay("sticker", image=rest.strip())
    return "Sticker on screen for a few seconds."


def _ask(app: App, rest: str) -> str:
    if not rest.strip():
        return "Usage: /live ask <question about what is on screen>"
    result = app.ask(rest)
    return result["answer"] + ("" if result["shown_on_overlay"] else "\n(The overlay is not in this scene, so viewers did not see it. /live setup adds it.)")


def _shot(app: App, rest: str) -> str:
    return f"Screenshot saved: {app.screenshot(rest.strip())['path']}"


def _reel(app: App, _rest: str) -> str:
    result = app.build_reel()
    skipped = f" {len(result['skipped_marks'])} mark(s) had nothing to cut from." if result["skipped_marks"] else ""
    return f"Highlight reel (draft, {result['clips']} clips): {result['path']}{skipped}"


_KEY = {"record": "record", "stream": "stream", "replay_buffer": "replay"}

SUBCOMMANDS: dict[str, Sub] = {
    "status": Sub("status", "What OBS and the session are doing", _status),
    "doctor": Sub("doctor", "Check the setup and say how to fix each gap", lambda app, _: format_report(app.doctor())),
    "setup": Sub("setup", "Add the overlay to your scenes and switch on the replay buffer", _setup),
    "start": Sub("start [title] [--record]", "Start a session (and the recording with --record)", _start),
    "stop": Sub("stop", "End the session: reel, chapters, title", _stop),
    "scene": Sub("scene [name]", "List scenes, or switch to one", _scene),
    "record": Sub("record start|stop|pause|resume", "Control the recording", _output("record", "Recording")),
    "stream": Sub("stream start|stop", "Go live or end the stream", _output("stream", "Stream")),
    "replay": Sub("replay start|stop|save", "Control the replay buffer", _output("replay_buffer", "Replay buffer")),
    "design": Sub("design <kind> [style] [title]", "Create an animated scene (starting-soon, brb, ending…)", _design),
    "card": Sub("card <text>", "Show a card on the overlay", _overlay("card", "Card on screen.")),
    "chapter": Sub("chapter <title>", "Flash a full-screen chapter title", _overlay("chapter", "Chapter title on screen.")),
    "celebrate": Sub("celebrate [confetti|fireworks|<emoji>]", "Confetti, fireworks or emoji rain", _celebrate),
    "gif": Sub("gif <link or path>", "Pop an image or GIF on screen", _gif),
    "hide": Sub("hide", "Clear the overlay now", _overlay("hide", "Overlay cleared.")),
    "ask": Sub("ask <question>", "Ask about what is on screen; the answer shows on the overlay", _ask),
    "shot": Sub("shot [source]", "Save a screenshot", _shot),
    "reel": Sub("reel", "Rebuild the highlight reel from the latest session", _reel),
    "pause": Sub("pause", "Stop Hermes from looking at the screen", lambda app, _: (app.set_capture_paused(True), "Screen capture paused. Hermes cannot see your screen until /live resume.")[1]),
    "resume": Sub("resume", "Let Hermes look at the screen again", lambda app, _: (app.set_capture_paused(False), "Screen capture resumed.")[1]),
}
_ALIASES = {"scenes": "scene", "rec": "record", "live": "stream", "check": "doctor", "screenshot": "shot",
            "confetti": "celebrate", "sticker": "gif", "clear": "hide", "end": "stop", "begin": "start"}


def help_text() -> str:
    width = max(len(sub.usage) for sub in SUBCOMMANDS.values())
    rows = [f"  /live {sub.usage.ljust(width)}  {sub.summary}" for sub in SUBCOMMANDS.values()]
    return "\n".join(["On Air: Hermes inside your OBS.", *rows, f"  {'/mark [note]'.ljust(width + 6)}  Save this moment",
                      "", "New here? Run /live setup."])


def _closest(name: str, options: list[str]) -> str:
    """Resolve what the user typed to a real scene name: exact, then case-insensitive, then prefix."""
    lowered = name.lower()
    for candidate in options:
        if candidate == name:
            return candidate
    matches = [c for c in options if c.lower() == lowered] or [c for c in options if c.lower().startswith(lowered)] \
        or [c for c in options if lowered in c.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise InvalidInput(f"There is no scene like '{name}'.", hint="Scenes: " + ", ".join(options))
    raise InvalidInput(f"'{name}' matches several scenes: {', '.join(matches)}.", hint="Type more of the name.")


def run_live(app: AppProvider, raw_args: str) -> str:
    name, _, rest = (raw_args or "").strip().partition(" ")
    name = name.lower() or "status"
    if name in ("help", "-h", "--help", "?"):
        return help_text()
    sub = SUBCOMMANDS.get(_ALIASES.get(name, name))
    if sub is None:
        return f"Unknown: /live {name}\n\n{help_text()}"
    return _guarded(lambda: sub.run(app(), rest))


def run_mark(app: AppProvider, raw_args: str) -> str:
    return _guarded(lambda: _mark(app(), raw_args or ""))


def _guarded(action: Callable[[], str]) -> str:
    try:
        return action()
    except OnAirError as exc:
        return f"✗ {exc}" + (f"\n  {exc.hint}" if exc.hint else "")
    except Exception as exc:
        logger.exception("Command failed")
        return f"✗ Something went wrong: {exc}"


def register(ctx: Any, app: AppProvider) -> None:
    def live(raw_args: str) -> str:
        return run_live(app, raw_args)

    def mark(raw_args: str) -> str:
        return run_mark(app, raw_args)

    ctx.register_command("live", live, description="Control OBS: scenes, recording, overlay, marks, highlight reel",
                         args_hint="[setup|start|stop|scene|design|ask|…]")
    ctx.register_command("onair", live, description="Same as /live", args_hint="[subcommand]")
    ctx.register_command("mark", mark, description="Save this moment of your recording or stream", args_hint="[note]")
