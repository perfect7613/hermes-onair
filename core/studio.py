"""The studio: what a person does in OBS, expressed as a handful of operations.

Callers speak in scenes, sources, placements and outputs. This module owns the obs-websocket
request names, the per-platform input kinds, transforms, and the idempotency rules (adding a
source twice updates it; removing something absent is not an error).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Optional

from .errors import InvalidInput, NotFound, ObsRequestError
from .obs import ObsClient

# obs-websocket status codes this module reacts to.
CODE_RESOURCE_NOT_FOUND = 600
CODE_RESOURCE_ALREADY_EXISTS = 601
CODE_OUTPUT_RUNNING = 500
CODE_OUTPUT_NOT_RUNNING = 501

# Friendly source kind -> OBS input kinds, most preferred first. The first one this OBS build
# offers is used, which is what makes "add a text source" work on macOS, Windows and Linux alike.
SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    "browser": ("browser_source",),
    "image": ("image_source",),
    "media": ("ffmpeg_source",),
    "text": ("text_ft2_source_v2", "text_gdiplus_v3", "text_gdiplus_v2", "text_ft2_source"),
    "color": ("color_source_v3", "color_source_v2", "color_source"),
    "screen": ("screen_capture", "monitor_capture", "display_capture",
               "pipewire-desktop-capture-source", "xshm_input"),
    "window": ("window_capture", "xcomposite_input", "pipewire-window-capture-source"),
    "camera": ("macos-avcapture", "av_capture_input_v2", "av_capture_input", "dshow_input", "v4l2_input"),
}

OUTPUTS = ("record", "stream", "replay_buffer", "virtual_cam")

_ALIGN_TOP_LEFT = 5
_BOUNDS_FIT = "OBS_BOUNDS_SCALE_INNER"
_BOUNDS_COVER = "OBS_BOUNDS_SCALE_OUTER"


@dataclass(frozen=True, slots=True)
class Rect:
    """A region of the canvas in fractions (0..1) of its width and height."""

    x: float
    y: float
    w: float
    h: float


_MARGIN = 0.02
# Named placements. Corner and centre placements take their size from ``scale``.
_FIXED_PLACEMENTS: dict[str, Rect] = {
    "fullscreen": Rect(0, 0, 1, 1),
    "left-half": Rect(0, 0, 0.5, 1),
    "right-half": Rect(0.5, 0, 0.5, 1),
    "lower-third": Rect(0.05, 0.72, 0.9, 0.22),
}
_SCALED_PLACEMENTS = ("center", "top-left", "top-right", "bottom-left", "bottom-right")
PLACEMENTS = (*_FIXED_PLACEMENTS, *_SCALED_PLACEMENTS)


def placement_rect(placement: str, scale: Optional[float] = None) -> Rect:
    """Resolve a named placement to a canvas region. ``scale`` is the fraction of the canvas a
    corner or centred source occupies (default a quarter for corners, a half for centre)."""
    if placement in _FIXED_PLACEMENTS:
        return _FIXED_PLACEMENTS[placement]
    if placement not in _SCALED_PLACEMENTS:
        raise InvalidInput(f"Unknown placement '{placement}'. Use one of: {', '.join(PLACEMENTS)}")
    size = scale if scale is not None else (0.5 if placement == "center" else 0.25)
    size = min(max(size, 0.05), 1.0)
    if placement == "center":
        return Rect((1 - size) / 2, (1 - size) / 2, size, size)
    x = _MARGIN if "left" in placement else 1 - size - _MARGIN
    y = _MARGIN if "top" in placement else 1 - size - _MARGIN
    return Rect(max(x, 0.0), max(y, 0.0), size, size)


def placement_transform(placement: str, canvas_w: int, canvas_h: int,
                        scale: Optional[float] = None) -> dict[str, Any]:
    """The scene-item transform for a placement. Bounds-based, so it is right whatever the
    source's own pixel size turns out to be (browser sources report 0x0 until they load)."""
    rect = placement_rect(placement, scale)
    return {
        "positionX": round(rect.x * canvas_w, 2),
        "positionY": round(rect.y * canvas_h, 2),
        "alignment": _ALIGN_TOP_LEFT,
        "boundsType": _BOUNDS_COVER if placement == "fullscreen" else _BOUNDS_FIT,
        "boundsAlignment": 0,
        "boundsWidth": max(round(rect.w * canvas_w, 2), 1.0),
        "boundsHeight": max(round(rect.h * canvas_h, 2), 1.0),
        "rotation": 0,
    }


def obs_color(value: str) -> int:
    """``#RRGGBB`` (or ``#RGB``) to the 0xAABBGGRR integer OBS stores."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise InvalidInput(f"'{value}' is not a colour. Use #RRGGBB.")
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise InvalidInput(f"'{value}' is not a colour. Use #RRGGBB.") from exc
    return (0xFF << 24) | (b << 16) | (g << 8) | r


def source_settings(kind: str, *, canvas: tuple[int, int], url: str = "", file: str = "",
                    text: str = "", color: str = "", width: int = 0, height: int = 0,
                    font_size: int = 96) -> dict[str, Any]:
    """Translate friendly arguments into the settings object OBS expects for ``kind``."""
    w, h = width or canvas[0], height or canvas[1]
    if kind == "browser":
        if file:
            return {"is_local_file": True, "local_file": file, "width": w, "height": h}
        if url:
            return {"is_local_file": False, "url": url, "width": w, "height": h}
        raise InvalidInput("A browser source needs 'url' or 'file'.")
    if kind == "image":
        if not file:
            raise InvalidInput("An image source needs 'file' (a local image or GIF path).")
        return {"file": file}
    if kind == "media":
        if not file:
            raise InvalidInput("A media source needs 'file' (a local video or audio path).")
        return {"is_local_file": True, "local_file": file, "looping": True}
    if kind == "text":
        if not text:
            raise InvalidInput("A text source needs 'text'.")
        settings: dict[str, Any] = {"text": text, "font": {"face": "Helvetica", "size": font_size}}
        if color:
            settings.update(color1=obs_color(color), color2=obs_color(color), color=obs_color(color))
        return settings
    if kind == "color":
        return {"color": obs_color(color or "#101418"), "width": w, "height": h}
    return {}  # captures and cameras: OBS defaults pick the main display / default device


class Studio:
    def __init__(self, client: ObsClient) -> None:
        self._obs = client

    # ------------------------------------------------------------------ reading

    def status(self) -> dict[str, Any]:
        """One snapshot of everything a director looks at."""
        names = ["GetVersion", "GetVideoSettings", "GetSceneList", "GetRecordStatus", "GetStreamStatus",
                 "GetReplayBufferStatus", "GetVirtualCamStatus", "GetStudioModeEnabled"]
        version, video, scenes, record, stream, replay, vcam, studio_mode = (
            {} if isinstance(result, ObsRequestError) else result
            for result in self._obs.call_batch([(name, {}) for name in names]))
        fps_den = video.get("fpsDenominator") or 1
        return {
            "obs": {"version": version.get("obsVersion", ""),
                    "websocket": version.get("obsWebSocketVersion", ""),
                    "platform": version.get("platform", "")},
            "canvas": {"width": video.get("baseWidth", 0), "height": video.get("baseHeight", 0),
                       "fps": round((video.get("fpsNumerator") or 0) / fps_den, 2)},
            "scene": scenes.get("currentProgramSceneName", ""),
            "scenes": _scene_names(scenes),
            "recording": {"active": bool(record.get("outputActive")),
                          "paused": bool(record.get("outputPaused")),
                          "timecode": record.get("outputTimecode", ""),
                          "duration_ms": int(record.get("outputDuration") or 0)},
            "streaming": {"active": bool(stream.get("outputActive")),
                          "reconnecting": bool(stream.get("outputReconnecting")),
                          "timecode": stream.get("outputTimecode", "")},
            # An OBS without a replay buffer configured refuses the status request outright.
            "replay_buffer": {"available": bool(replay), "active": bool(replay.get("outputActive"))},
            "virtual_cam": {"active": bool(vcam.get("outputActive"))},
            "studio_mode": bool(studio_mode.get("studioModeEnabled")),
        }

    def scenes(self) -> list[str]:
        return _scene_names(self._obs.call("GetSceneList"))

    def current_scene(self) -> str:
        return self._obs.call("GetCurrentProgramScene").get("currentProgramSceneName", "")

    def sources(self, scene: str = "") -> list[dict[str, Any]]:
        """Sources in ``scene`` (default: the one on air), topmost first."""
        scene = scene or self.current_scene()
        items = self._call_scene("GetSceneItemList", scene).get("sceneItems", [])
        return [{"id": item["sceneItemId"], "name": item["sourceName"],
                 "kind": item.get("inputKind") or item.get("sourceType", ""),
                 "visible": bool(item.get("sceneItemEnabled")), "locked": bool(item.get("sceneItemLocked"))}
                for item in sorted(items, key=lambda it: -it.get("sceneItemIndex", 0))]

    def canvas(self) -> tuple[int, int]:
        video = self._obs.call("GetVideoSettings")
        return int(video.get("baseWidth") or 1920), int(video.get("baseHeight") or 1080)

    def is_streaming(self) -> bool:
        return bool(self._obs.call("GetStreamStatus").get("outputActive"))

    def record_position_ms(self) -> Optional[int]:
        """Milliseconds into the current recording, or ``None`` when not recording."""
        status = self._obs.call("GetRecordStatus")
        return int(status.get("outputDuration") or 0) if status.get("outputActive") else None

    def screenshot(self, source: str = "", *, width: int = 0, quality: int = 80,
                   image_format: str = "jpg") -> bytes:
        """Image of ``source`` (default: the scene on air). JPEG unless ``image_format`` says otherwise."""
        data: dict[str, Any] = {"sourceName": source or self.current_scene(), "imageFormat": image_format,
                                "imageCompressionQuality": quality}
        if width:
            data["imageWidth"] = max(8, min(width, 4096))
        try:
            image = self._obs.call("GetSourceScreenshot", data, timeout=15).get("imageData", "")
        except ObsRequestError as exc:
            if exc.code == CODE_RESOURCE_NOT_FOUND:
                raise NotFound(f"There is no scene or source named '{data['sourceName']}'.",
                               hint="Use live_status to list scene and source names.") from exc
            raise
        return base64.b64decode(image.split(",", 1)[-1])

    # ------------------------------------------------------------------ scenes

    def switch_scene(self, name: str) -> None:
        self._call_scene("SetCurrentProgramScene", name)

    def create_scene(self, name: str) -> bool:
        """Create ``name``. Returns False when it already existed."""
        try:
            self._obs.call("CreateScene", {"sceneName": name})
            return True
        except ObsRequestError as exc:
            if exc.code == CODE_RESOURCE_ALREADY_EXISTS:
                return False
            raise

    def remove_scene(self, name: str) -> None:
        if len(self.scenes()) <= 1:
            raise InvalidInput("OBS needs at least one scene, so the last one cannot be removed.")
        self._call_scene("RemoveScene", name)

    def rename_scene(self, name: str, new_name: str) -> None:
        self._call_scene("SetSceneName", name, newSceneName=new_name)

    # ------------------------------------------------------------------ sources

    def add_source(self, scene: str, name: str, kind: str, settings: dict[str, Any], *,
                   placement: str = "", scale: Optional[float] = None, visible: bool = True,
                   on_top: bool = True) -> dict[str, Any]:
        """Put source ``name`` into ``scene``. If a source of that name exists its settings are
        updated and it is reused, so calling this twice never duplicates anything."""
        scene = scene or self.current_scene()
        existing = {item["inputName"] for item in self._obs.call("GetInputList").get("inputs", [])}
        created = name not in existing
        if created:
            item_id = self._call_scene("CreateInput", scene, inputName=name, inputKind=self.input_kind(kind),
                                       inputSettings=settings, sceneItemEnabled=visible)["sceneItemId"]
        else:
            if settings:
                self._obs.call("SetInputSettings", {"inputName": name, "inputSettings": settings})
            item_id = self._item_id(scene, name, missing_ok=True)
            if item_id is None:
                item_id = self._call_scene("CreateSceneItem", scene, sourceName=name,
                                           sceneItemEnabled=visible)["sceneItemId"]
        if placement:
            w, h = self.canvas()
            self._obs.call("SetSceneItemTransform", {
                "sceneName": scene, "sceneItemId": item_id,
                "sceneItemTransform": placement_transform(placement, w, h, scale)})
        if on_top:
            count = len(self._obs.call("GetSceneItemList", {"sceneName": scene}).get("sceneItems", []))
            self._obs.call("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": item_id,
                                                 "sceneItemIndex": max(count - 1, 0)})
        return {"scene": scene, "source": name, "created": created, "item_id": item_id}

    def place_source(self, scene: str, name: str, placement: str, scale: Optional[float] = None) -> None:
        scene = scene or self.current_scene()
        w, h = self.canvas()
        self._obs.call("SetSceneItemTransform", {
            "sceneName": scene, "sceneItemId": self._item_id(scene, name),
            "sceneItemTransform": placement_transform(placement, w, h, scale)})

    def set_visible(self, scene: str, name: str, visible: bool) -> None:
        scene = scene or self.current_scene()
        self._obs.call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": self._item_id(scene, name),
                                               "sceneItemEnabled": visible})

    def update_source(self, name: str, settings: dict[str, Any]) -> None:
        try:
            self._obs.call("SetInputSettings", {"inputName": name, "inputSettings": settings})
        except ObsRequestError as exc:
            raise self._not_found(exc, f"There is no source named '{name}'.") from exc

    def remove_source(self, scene: str, name: str, *, delete: bool = False) -> None:
        """Take ``name`` out of ``scene``; with ``delete`` remove the source from OBS entirely."""
        scene = scene or self.current_scene()
        if delete:
            try:
                self._obs.call("RemoveInput", {"inputName": name})
            except ObsRequestError as exc:
                raise self._not_found(exc, f"There is no source named '{name}'.") from exc
            return
        self._obs.call("RemoveSceneItem", {"sceneName": scene, "sceneItemId": self._item_id(scene, name)})

    def restart_media(self, name: str) -> None:
        """Play a media source from its start."""
        try:
            self._obs.call("TriggerMediaInputAction", {
                "inputName": name, "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
        except ObsRequestError as exc:
            raise self._not_found(exc, f"There is no source named '{name}'.") from exc

    def source_kind(self, name: str) -> str:
        """The OBS kind of the existing source ``name`` (for example ``browser_source``)."""
        try:
            return str(self._obs.call("GetInputSettings", {"inputName": name}).get("inputKind") or "")
        except ObsRequestError as exc:
            raise self._not_found(exc, f"There is no source named '{name}'.") from exc

    def input_kind(self, kind: str) -> str:
        """The concrete OBS input kind for a friendly kind on this machine."""
        if kind not in SOURCE_KINDS:
            raise InvalidInput(f"Unknown source kind '{kind}'. Use one of: {', '.join(SOURCE_KINDS)}")
        available = set(self._obs.call("GetInputKindList").get("inputKinds", []))
        for candidate in SOURCE_KINDS[kind]:
            if candidate in available:
                return candidate
        raise NotFound(f"This OBS has no '{kind}' source type.",
                       hint="It may need an OBS plugin or a newer OBS version.")

    def emit_browser_event(self, name: str, detail: dict[str, Any]) -> None:
        """Dispatch a JavaScript ``CustomEvent`` in every browser source (obs-browser's own channel)."""
        self._obs.call("CallVendorRequest", {
            "vendorName": "obs-browser", "requestType": "emit_event",
            "requestData": {"event_name": name, "event_data": detail}})

    # ------------------------------------------------------------------ outputs

    def set_output(self, output: str, running: bool) -> dict[str, Any]:
        """Start or stop record / stream / replay_buffer / virtual_cam. Asking for the state an
        output is already in succeeds quietly."""
        if output not in OUTPUTS:
            raise InvalidInput(f"Unknown output '{output}'. Use one of: {', '.join(OUTPUTS)}")
        if output == "replay_buffer" and running:
            return self.ensure_replay_buffer()
        request = {"record": "Record", "stream": "Stream", "replay_buffer": "ReplayBuffer",
                   "virtual_cam": "VirtualCam"}[output]
        try:
            result = self._obs.call(("Start" if running else "Stop") + request, timeout=20)
        except ObsRequestError as exc:
            if exc.code not in (CODE_OUTPUT_RUNNING, CODE_OUTPUT_NOT_RUNNING):
                raise
            return {"output": output, "running": running, "changed": False}
        response = {"output": output, "running": running, "changed": True}
        if result.get("outputPath"):
            response["path"] = result["outputPath"]
        return response

    def pause_recording(self, paused: bool) -> None:
        self._obs.call("PauseRecord" if paused else "ResumeRecord")

    def ensure_replay_buffer(self, seconds: int = 30) -> dict[str, Any]:
        """Make sure the replay buffer is running, switching it on in the OBS profile if needed."""
        enabled_now = False
        try:
            active = bool(self._obs.call("GetReplayBufferStatus").get("outputActive"))
        except ObsRequestError:
            mode = self._obs.call("GetProfileParameter", {
                "parameterCategory": "Output", "parameterName": "Mode"}).get("parameterValue") or "Simple"
            category = "AdvOut" if mode == "Advanced" else "SimpleOutput"
            for parameter, value in (("RecRB", "true"), ("RecRBTime", str(seconds))):
                self._obs.call("SetProfileParameter", {"parameterCategory": category,
                                                       "parameterName": parameter, "parameterValue": value})
            enabled_now, active = True, False
        if not active:
            try:
                self._obs.call("StartReplayBuffer", timeout=20)
            except ObsRequestError as exc:
                if exc.code != CODE_OUTPUT_RUNNING:
                    exc.hint = ("The replay buffer is now enabled in your OBS profile, but OBS has not picked it up. "
                                "Restart OBS (or tick Settings → Output → Replay Buffer → Enable) and run /live setup again."
                                if enabled_now else
                                "In OBS open Settings → Output → Replay Buffer, tick 'Enable Replay Buffer', "
                                "then run /live setup again.")
                    raise
        return {"output": "replay_buffer", "running": True, "changed": not active, "enabled_in_profile": enabled_now}

    def save_replay(self) -> None:
        self._obs.call("SaveReplayBuffer")

    def last_replay_path(self) -> str:
        try:
            return self._obs.call("GetLastReplayBufferReplay").get("savedReplayPath", "")
        except ObsRequestError:
            return ""

    def add_record_chapter(self, name: str) -> bool:
        """Drop a chapter marker into the recording file where the container supports it."""
        try:
            self._obs.call("CreateRecordChapter", {"chapterName": name})
            return True
        except ObsRequestError:
            return False

    def raw(self, request_type: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Escape hatch: any obs-websocket request."""
        return self._obs.call(request_type, data or {})

    # ------------------------------------------------------------------ helpers

    def _call_scene(self, request: str, scene: str, **data: Any) -> dict[str, Any]:
        try:
            return self._obs.call(request, {"sceneName": scene, **data})
        except ObsRequestError as exc:
            raise self._not_found(exc, f"There is no scene named '{scene}'.") from exc

    def _item_id(self, scene: str, source: str, *, missing_ok: bool = False) -> Any:
        try:
            return self._obs.call("GetSceneItemId", {"sceneName": scene, "sourceName": source})["sceneItemId"]
        except ObsRequestError as exc:
            if exc.code != CODE_RESOURCE_NOT_FOUND:
                raise
            if missing_ok:
                return None
            raise NotFound(f"Scene '{scene}' has no source named '{source}'.",
                           hint="Use live_status to list scene and source names.") from exc

    @staticmethod
    def _not_found(exc: ObsRequestError, message: str) -> Exception:
        if exc.code == CODE_RESOURCE_NOT_FOUND:
            return NotFound(message, hint="Use live_status to list scene and source names.")
        return exc


def _scene_names(scene_list: dict[str, Any]) -> list[str]:
    # OBS lists scenes bottom-up; people read the list top-down.
    return [scene["sceneName"] for scene in reversed(scene_list.get("scenes", []))]
