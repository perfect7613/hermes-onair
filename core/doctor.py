"""Setup checks. Each one names the gap and the exact fix, in the order a person would fix them."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from .errors import OnAirError
from .obs import Endpoint, ObsClient
from .reel import ReelBuilder
from .stage import Stage
from .studio import Studio


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_checks(client: ObsClient, studio: Studio, stage: Stage, reel: ReelBuilder,
               endpoint: Endpoint) -> list[Check]:
    checks: list[Check] = []
    try:
        status = studio.status()
    except OnAirError as exc:
        checks.append(Check("OBS connection", False, f"{exc} ({endpoint.url})", exc.hint))
        checks.append(_ffmpeg(reel))
        return checks  # everything else needs OBS; listing it as failed would only be noise

    obs = status["obs"]
    checks.append(Check("OBS connection", True, f"OBS {obs['version']}, obs-websocket {obs['websocket']}"))

    try:
        studio.input_kind("browser")
        checks.append(Check("Browser sources", True, "browser source type available"))
    except OnAirError:
        checks.append(Check("Browser sources", False, "this OBS build has no browser source",
                            "Install the official OBS build from obsproject.com; it includes browser sources."))

    if stage.overlay_installed():
        checks.append(Check("Overlay", True, f"in scene '{status['scene']}'"))
    else:
        checks.append(Check("Overlay", False, f"not in scene '{status['scene']}'", "Run /live setup."))

    replay = status["replay_buffer"]
    if replay["active"]:
        checks.append(Check("Replay buffer", True, "running, marks will save clips"))
    else:
        detail = "enabled but stopped" if replay["available"] else "switched off in OBS"
        checks.append(Check("Replay buffer", False, detail, "Run /live setup to switch it on."))

    try:
        folder = client.call("GetRecordDirectory").get("recordDirectory", "")
        writable = bool(folder) and os.path.isdir(folder) and os.access(folder, os.W_OK)
        checks.append(Check("Recording folder", writable, folder or "not set",
                            "" if writable else "In OBS open Settings → Output and choose a recording path that exists."))
    except OnAirError as exc:
        checks.append(Check("Recording folder", False, str(exc)))

    checks.append(_ffmpeg(reel))
    return checks


def _ffmpeg(reel: ReelBuilder) -> Check:
    if reel.available():
        return Check("FFmpeg", True, "found, highlight reels can be rendered")
    return Check("FFmpeg", False, "not found (marks still work; only the reel waits)",
                 "Install FFmpeg: macOS 'brew install ffmpeg', Windows 'winget install ffmpeg', Linux use your package manager.")


def format_report(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        lines.append(f"{'✓' if check.ok else '✗'} {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"    Fix: {check.fix}")
    failed = sum(not check.ok for check in checks)
    lines.append("")
    lines.append("All set. Try /live start." if not failed else f"{failed} thing(s) to fix above.")
    return "\n".join(lines)
