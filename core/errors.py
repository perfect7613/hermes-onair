"""Error taxonomy. Every error carries a ``hint``: one plain sentence telling the user what to do."""

from __future__ import annotations


class OnAirError(Exception):
    """Base class. ``hint`` is shown to the user next to the message."""

    default_hint = ""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint or self.default_hint

    def to_dict(self) -> dict[str, str]:
        body = {"error": str(self), "kind": type(self).__name__}
        if self.hint:
            body["hint"] = self.hint
        return body

    def __str__(self) -> str:  # message only; callers add the hint where they want it
        return super().__str__()


class ObsUnavailable(OnAirError):
    """OBS is not running or its WebSocket server is off."""

    default_hint = (
        "Open OBS, then Tools → WebSocket Server Settings → tick 'Enable WebSocket server'. "
        "Run /live doctor to re-check."
    )


class ObsAuthError(OnAirError):
    """OBS refused the password."""

    default_hint = (
        "Copy the password from OBS (Tools → WebSocket Server Settings → Show Connect Info) into "
        "Hermes Settings → Plugins → On Air → OBS password."
    )


class ObsTimeout(OnAirError):
    default_hint = "OBS did not answer in time. It may be busy; try again."


class ObsRequestError(OnAirError):
    """OBS answered, and the answer was a refusal."""

    def __init__(self, request_type: str, code: int, comment: str = "", *, hint: str = "") -> None:
        text = f"OBS refused {request_type} (code {code})"
        if comment:
            text += f": {comment}"
        super().__init__(text, hint=hint)
        self.request_type = request_type
        self.code = code
        self.comment = comment


class NotFound(OnAirError):
    """A scene, source or mark the caller named does not exist."""


class InvalidInput(OnAirError):
    """The caller's arguments are wrong; the message says which one."""


class ReelUnavailable(OnAirError):
    default_hint = "Install FFmpeg (macOS: brew install ffmpeg) and run the reel again. Your marks are safe."
