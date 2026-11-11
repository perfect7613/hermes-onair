"""Automatic marks: a command fails, then the same command passes.

Pure bookkeeping, no I/O. ``observe`` is fed every terminal command the agent runs and answers
with the mark to drop, if any. The fail and the fix of one command are the two ends of a story
beat, so the fix is only reported for a command that was seen failing.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

# Only commands that build, test, lint or run something are worth a mark. `grep` exiting 1 is not a story.
_INTERESTING = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"pytest|tox|nox|unittest|ruff|mypy|pyright|flake8|"
    r"npm|pnpm|yarn|bun|npx|node|deno|tsc|eslint|vitest|jest|"
    r"cargo|rustc|go|make|cmake|ninja|gcc|g\+\+|clang|"
    r"gradle|gradlew|mvn|dotnet|swift|xcodebuild|"
    r"rspec|rake|bundle|mix|phpunit|composer|"
    r"docker|terraform|uv|poetry|python3?|pip3?"
    r")(?:\s|$)"
)

MAX_CONVERSATIONS = 64
MAX_OPEN_FAILURES = 32


@dataclass(frozen=True, slots=True)
class MarkIntent:
    kind: str  # "fail" | "fix"
    note: str


def command_key(command: str) -> str:
    """Two runs of the same command compare equal regardless of spacing."""
    return " ".join(command.split())


def is_interesting(command: str) -> bool:
    return bool(_INTERESTING.search(command))


class AutoMarker:
    def __init__(self) -> None:
        # conversation id -> (command key -> True), both bounded and least-recently-used first.
        self._failing: "OrderedDict[str, OrderedDict[str, bool]]" = OrderedDict()

    def observe(self, conversation: str, command: str, exit_code: Optional[int]) -> Optional[MarkIntent]:
        if exit_code is None or not command or not is_interesting(command):
            return None
        key = command_key(command)
        failing = self._failing.setdefault(conversation, OrderedDict())
        self._failing.move_to_end(conversation)
        while len(self._failing) > MAX_CONVERSATIONS:
            self._failing.popitem(last=False)
        if exit_code != 0:
            if key in failing:  # still broken: one mark per break, not one per retry
                failing.move_to_end(key)
                return None
            failing[key] = True
            while len(failing) > MAX_OPEN_FAILURES:
                failing.popitem(last=False)
            return MarkIntent("fail", _summary(key))
        if failing.pop(key, None):
            return MarkIntent("fix", _summary(key))
        return None


def _summary(command: str, limit: int = 60) -> str:
    return command if len(command) <= limit else command[: limit - 1] + "…"
