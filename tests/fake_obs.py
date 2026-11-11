"""An in-memory OBS that speaks obs-websocket v5, and a fake Hermes host.

The fake keeps real state (scenes, inputs, scene items, outputs, profile parameters), so tests
assert on what OBS would look like afterwards rather than on which requests were sent.
"""

from __future__ import annotations

import base64
import json
import queue
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from onair.core.app import Settings
from onair.core.errors import ObsUnavailable
from onair.core.obs import Endpoint, TransportClosed, auth_response

# A valid 1x1 JPEG is not needed: the code under test treats screenshots as opaque bytes.
FAKE_IMAGE = b"\xff\xd8fake-jpeg\xff\xd9"


class Refuse(Exception):
    def __init__(self, code: int, comment: str = "") -> None:
        super().__init__(comment)
        self.code, self.comment = code, comment


class _FakeTransport:
    def __init__(self, obs: "FakeObs") -> None:
        self._obs = obs
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self.closed = False
        self.identified = False

    def push(self, message: dict[str, Any]) -> None:
        self._inbox.put(json.dumps(message))

    def kill(self, code: Optional[int] = None) -> None:
        self.closed = True
        self._inbox.put(TransportClosed("fake close", code))

    def send(self, text: str) -> None:
        if self.closed:
            raise TransportClosed("fake socket is closed")
        self._obs.receive(self, json.loads(text))

    def recv(self, timeout: Optional[float] = None) -> str:
        try:
            item = self._inbox.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        if not self.closed:
            self.kill()


class FakeObs:
    def __init__(self, password: str = "") -> None:
        self.password = password
        self.down = False            # connection attempts are refused
        self.mute = False            # requests are swallowed (for timeout tests)
        self.connections: list[_FakeTransport] = []
        self.connect_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.RLock()

        self.scenes: dict[str, list[dict[str, Any]]] = {"Main": []}  # name -> items, bottom first
        self.scene_order = ["Main"]                                  # top first, as the user sees it
        self.current = "Main"
        self.inputs: dict[str, dict[str, Any]] = {}
        self.next_item_id = 1
        self.recording = self.streaming = self.replay_active = self.vcam = False
        self.replay_available = True
        self.profile: dict[tuple[str, str], str] = {("Output", "Mode"): "Simple"}
        self.last_replay = ""
        self.replay_counter = 0
        self.record_ms = 0
        self.browser_events: list[dict[str, Any]] = []
        self.kinds = ["browser_source", "image_source", "ffmpeg_source", "text_ft2_source_v2",
                      "color_source_v3", "screen_capture", "window_capture", "av_capture_input_v2"]

    # ------------------------------------------------------------------ transport side

    def connector(self, endpoint: Endpoint, timeout: float) -> _FakeTransport:
        if self.down:
            raise ObsUnavailable(f"Could not reach OBS at {endpoint.url}")
        transport = _FakeTransport(self)
        with self.lock:
            self.connections.append(transport)
            self.connect_count += 1
        hello: dict[str, Any] = {"obsWebSocketVersion": "5.6.3", "obsStudioVersion": "33.0.1", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"salt": "salt", "challenge": "challenge"}
        transport.push({"op": 0, "d": hello})
        return transport

    def receive(self, transport: _FakeTransport, message: dict[str, Any]) -> None:
        op, data = message["op"], message.get("d", {})
        if op == 1:
            if self.password and data.get("authentication") != auth_response(self.password, "salt", "challenge"):
                transport.kill(4009)
                return
            transport.identified = True
            transport.push({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        elif op == 6 and not self.mute:
            transport.push({"op": 7, "d": {"requestId": data["requestId"], "requestType": data["requestType"],
                                           **self._answer(data["requestType"], data.get("requestData") or {})}})
        elif op == 8 and not self.mute:
            results = [{"requestType": r["requestType"], **self._answer(r["requestType"], r.get("requestData") or {})}
                       for r in data["requests"]]
            transport.push({"op": 9, "d": {"requestId": data["requestId"], "results": results}})

    def _answer(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.calls.append((name, data))
            method = getattr(self, f"do_{name}", None)
            if method is None:
                return {"requestStatus": {"result": False, "code": 204, "comment": f"unknown request {name}"}}
            try:
                return {"requestStatus": {"result": True, "code": 100}, "responseData": method(data) or {}}
            except Refuse as refusal:
                return {"requestStatus": {"result": False, "code": refusal.code, "comment": refusal.comment}}

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        for transport in list(self.connections):
            if transport.identified and not transport.closed:
                transport.push({"op": 5, "d": {"eventType": event_type, "eventIntent": 1, "eventData": data}})

    def drop_connections(self) -> None:
        for transport in list(self.connections):
            if not transport.closed:
                transport.kill()

    def called(self, name: str) -> list[dict[str, Any]]:
        return [data for request, data in self.calls if request == name]

    # ------------------------------------------------------------------ helpers

    def _scene(self, name: str) -> list[dict[str, Any]]:
        if name not in self.scenes:
            raise Refuse(600, f"No source was found by the name of `{name}`.")
        return self.scenes[name]

    def _item(self, scene: str, item_id: int) -> dict[str, Any]:
        for item in self._scene(scene):
            if item["sceneItemId"] == item_id:
                return item
        raise Refuse(600, "No scene items were found.")

    def _add_item(self, scene: str, source: str, enabled: bool = True) -> int:
        item = {"sceneItemId": self.next_item_id, "sourceName": source, "sceneItemEnabled": enabled,
                "sceneItemLocked": False, "inputKind": self.inputs.get(source, {}).get("kind"), "transform": {}}
        self.next_item_id += 1
        self._scene(scene).append(item)
        return item["sceneItemId"]

    def press_save_replay_hotkey(self) -> str:
        """What happens when the user hits OBS's Save Replay hotkey."""
        with self.lock:
            self.replay_counter += 1
            self.last_replay = f"/videos/Replay {self.replay_counter}.mkv"
            path = self.last_replay
        self.emit("ReplayBufferSaved", {"savedReplayPath": path})
        return path

    # ------------------------------------------------------------------ requests

    def do_GetVersion(self, d): return {"obsVersion": "33.0.1", "obsWebSocketVersion": "5.6.3", "platform": "macos"}
    def do_GetVideoSettings(self, d): return {"baseWidth": 1920, "baseHeight": 1080, "fpsNumerator": 60, "fpsDenominator": 1}
    def do_GetStudioModeEnabled(self, d): return {"studioModeEnabled": False}
    def do_GetInputKindList(self, d): return {"inputKinds": list(self.kinds)}
    def do_GetRecordDirectory(self, d): return {"recordDirectory": tempfile.gettempdir()}

    def do_GetSceneList(self, d):
        return {"currentProgramSceneName": self.current,
                "scenes": [{"sceneName": name, "sceneIndex": i} for i, name in enumerate(reversed(self.scene_order))]}

    def do_GetCurrentProgramScene(self, d): return {"currentProgramSceneName": self.current}

    def do_SetCurrentProgramScene(self, d):
        self._scene(d["sceneName"])
        self.current = d["sceneName"]

    def do_CreateScene(self, d):
        if d["sceneName"] in self.scenes:
            raise Refuse(601, "A source already exists by that scene name.")
        self.scenes[d["sceneName"]] = []
        self.scene_order.append(d["sceneName"])

    def do_RemoveScene(self, d):
        self._scene(d["sceneName"])
        del self.scenes[d["sceneName"]]
        self.scene_order.remove(d["sceneName"])
        if self.current == d["sceneName"]:
            self.current = self.scene_order[0]

    def do_SetSceneName(self, d):
        items = self._scene(d["sceneName"])
        del self.scenes[d["sceneName"]]
        self.scenes[d["newSceneName"]] = items
        self.scene_order[self.scene_order.index(d["sceneName"])] = d["newSceneName"]
        if self.current == d["sceneName"]:
            self.current = d["newSceneName"]

    def do_GetSceneItemList(self, d):
        return {"sceneItems": [{**{k: v for k, v in item.items() if k != "transform"}, "sceneItemIndex": index}
                               for index, item in enumerate(self._scene(d["sceneName"]))]}

    def do_GetSceneItemId(self, d):
        for item in self._scene(d["sceneName"]):
            if item["sourceName"] == d["sourceName"]:
                return {"sceneItemId": item["sceneItemId"]}
        raise Refuse(600, "No scene items were found in the specified scene by that name.")

    def do_GetInputList(self, d):
        return {"inputs": [{"inputName": name, "inputKind": value["kind"]} for name, value in self.inputs.items()]}

    def do_CreateInput(self, d):
        if d["inputName"] in self.inputs:
            raise Refuse(601, "A source already exists by that input name.")
        if d["inputKind"] not in self.kinds:
            raise Refuse(400, "The specified input kind is invalid.")
        self._scene(d["sceneName"])
        self.inputs[d["inputName"]] = {"kind": d["inputKind"], "settings": dict(d.get("inputSettings") or {})}
        return {"sceneItemId": self._add_item(d["sceneName"], d["inputName"], d.get("sceneItemEnabled", True))}

    def do_CreateSceneItem(self, d):
        if d["sourceName"] not in self.inputs:
            raise Refuse(600, "No source was found.")
        return {"sceneItemId": self._add_item(d["sceneName"], d["sourceName"], d.get("sceneItemEnabled", True))}

    def do_SetInputSettings(self, d):
        if d["inputName"] not in self.inputs:
            raise Refuse(600, "No source was found.")
        self.inputs[d["inputName"]]["settings"].update(d["inputSettings"])

    def do_RemoveInput(self, d):
        if d["inputName"] not in self.inputs:
            raise Refuse(600, "No source was found.")
        del self.inputs[d["inputName"]]
        for items in self.scenes.values():
            items[:] = [item for item in items if item["sourceName"] != d["inputName"]]

    def do_RemoveSceneItem(self, d):
        item = self._item(d["sceneName"], d["sceneItemId"])
        self._scene(d["sceneName"]).remove(item)

    def do_SetSceneItemEnabled(self, d):
        self._item(d["sceneName"], d["sceneItemId"])["sceneItemEnabled"] = d["sceneItemEnabled"]

    def do_SetSceneItemTransform(self, d):
        self._item(d["sceneName"], d["sceneItemId"])["transform"] = d["sceneItemTransform"]

    def do_SetSceneItemIndex(self, d):
        items = self._scene(d["sceneName"])
        item = self._item(d["sceneName"], d["sceneItemId"])
        items.remove(item)
        items.insert(d["sceneItemIndex"], item)

    def do_PressInputPropertiesButton(self, d): return {}

    def do_TriggerMediaInputAction(self, d):
        if d["inputName"] not in self.inputs:
            raise Refuse(600, "No source was found.")
        self.inputs[d["inputName"]]["restarts"] = self.inputs[d["inputName"]].get("restarts", 0) + 1

    def do_GetSourceScreenshot(self, d):
        if d["sourceName"] not in self.scenes and d["sourceName"] not in self.inputs:
            raise Refuse(600, "No source was found.")
        return {"imageData": "data:image/jpg;base64," + base64.b64encode(FAKE_IMAGE).decode()}

    def do_CallVendorRequest(self, d):
        if d["vendorName"] != "obs-browser":
            raise Refuse(600, "No vendor was found.")
        self.browser_events.append(d["requestData"])
        return {"vendorName": "obs-browser", "requestType": d["requestType"], "responseData": {}}

    def do_GetRecordStatus(self, d):
        return {"outputActive": self.recording, "outputPaused": False, "outputDuration": self.record_ms,
                "outputTimecode": "00:00:00.000"}

    def do_GetStreamStatus(self, d): return {"outputActive": self.streaming, "outputReconnecting": False}
    def do_GetVirtualCamStatus(self, d): return {"outputActive": self.vcam}

    def do_GetReplayBufferStatus(self, d):
        if not self.replay_available:
            raise Refuse(604, "Replay buffer is not available.")
        return {"outputActive": self.replay_active}

    def _toggle(self, attribute: str, on: bool) -> None:
        if getattr(self, attribute) == on:
            raise Refuse(500 if on else 501, "Output is already in that state.")
        setattr(self, attribute, on)

    def do_StartRecord(self, d): self._toggle("recording", True)

    def do_StopRecord(self, d):
        self._toggle("recording", False)
        return {"outputPath": "/videos/recording.mkv"}

    def do_StartStream(self, d): self._toggle("streaming", True)
    def do_StopStream(self, d): self._toggle("streaming", False)
    def do_StartVirtualCam(self, d): self._toggle("vcam", True)
    def do_StopVirtualCam(self, d): self._toggle("vcam", False)

    def do_StartReplayBuffer(self, d):
        if not self.replay_available:
            raise Refuse(604, "Replay buffer is not available.")
        self._toggle("replay_active", True)

    def do_StopReplayBuffer(self, d): self._toggle("replay_active", False)

    def do_SaveReplayBuffer(self, d):
        if not self.replay_active:
            raise Refuse(501, "Replay buffer is not active.")
        self.press_save_replay_hotkey()

    def do_GetLastReplayBufferReplay(self, d): return {"savedReplayPath": self.last_replay}
    def do_CreateRecordChapter(self, d): return {}

    def do_GetProfileParameter(self, d):
        return {"parameterValue": self.profile.get((d["parameterCategory"], d["parameterName"]))}

    def do_SetProfileParameter(self, d):
        self.profile[(d["parameterCategory"], d["parameterName"])] = d["parameterValue"]
        if d["parameterName"] == "RecRB" and d["parameterValue"] == "true":
            self.replay_available = True


class FakeHost:
    """The ``Host`` the application needs, backed by a temporary directory."""

    def __init__(self, directory: Path, **settings: Any) -> None:
        self.directory = directory
        self._settings = Settings(**settings)
        self.password = ""
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.state: dict[str, Any] = {}
        self.model_reply = '{"answer": "It is a missing import on line 3.", "card": "Missing import on line 3"}'
        self.model_calls: list[tuple[str, str, Optional[bytes]]] = []

    def configure(self, **settings: Any) -> None:
        from dataclasses import replace
        self._settings = replace(self._settings, **settings)

    def settings(self) -> Settings: return self._settings
    def obs_password(self) -> str: return self.password
    def data_dir(self) -> Path: return self.directory

    def connect_db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.directory / "data.db", check_same_thread=False)

    def ask_model(self, instructions: str, text: str, image: Optional[bytes] = None) -> str:
        self.model_calls.append((instructions, text, image))
        if isinstance(self.model_reply, Exception):
            raise self.model_reply
        return self.model_reply

    def notify(self, event: str, payload: dict[str, Any]) -> None: self.events.append((event, payload))
    def get_state(self, key: str, default: Any = None) -> Any: return self.state.get(key, default)
    def set_state(self, key: str, value: Any) -> None: self.state[key] = value
