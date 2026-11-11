"""The whole plugin against a fake OBS: application, tools, slash commands and Desktop RPC."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from onair import commands, rpc, tools
from onair.core.app import App
from onair.core.errors import InvalidInput, ObsUnavailable
from onair.core.reel import ReelBuilder
from onair.core.stage import OVERLAY_SOURCE

from .fake_obs import FAKE_IMAGE, FakeHost, FakeObs
from .test_obs import wait_for


class AppCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.obs = FakeObs()
        self.host = FakeHost(self.dir)
        self.reel_calls: list[list[str]] = []
        self.app = App(self.host, self.obs.connector, ReelBuilder(run=self.fake_ffmpeg, which=lambda name: f"/bin/{name}"))
        self.app.director._sleep = lambda seconds: None  # do not really wait for replay files
        self.addCleanup(self.app.close)

    def fake_ffmpeg(self, args):
        self.reel_calls.append(list(args))
        if args[0] == "ffmpeg" and "-encoders" not in args:
            Path(args[-1]).write_bytes(b"video")
        stdout = '{"streams": [{"width": 1280, "height": 720, "r_frame_rate": "30/1"}]}' if args[0] == "ffprobe" else "libx264"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def overlay_messages(self) -> list[dict]:
        return [event["event_data"] for event in self.obs.browser_events]

    def tool(self, tool_name: str, /, **args) -> dict:
        spec = next(tool for tool in tools.TOOLS if tool.name == tool_name)
        return json.loads(tools.handler(spec, lambda: self.app)(args))

    def live(self, text: str) -> str:
        return commands.run_live(lambda: self.app, text)


class SetupTests(AppCase):
    def test_setup_does_the_obs_clicking(self):
        self.obs.replay_available = False
        self.obs.scenes["Camera"] = []
        self.obs.scene_order.append("Camera")
        result = self.app.setup()
        for scene in ("Main", "Camera"):
            self.assertEqual([item["sourceName"] for item in self.obs.scenes[scene]], [OVERLAY_SOURCE])
        self.assertTrue(self.obs.replay_active, "replay buffer was switched on and started")
        self.assertEqual(self.obs.profile[("SimpleOutput", "RecRB")], "true")
        overlay_file = Path(self.obs.inputs[OVERLAY_SOURCE]["settings"]["local_file"])
        self.assertTrue(overlay_file.is_file())
        self.assertTrue(overlay_file.is_relative_to(self.dir), "the overlay lives in the data directory, not the install directory")
        self.assertTrue(all(check["ok"] for check in result["checks"]), result["checks"])

    def test_setup_twice_changes_nothing(self):
        self.app.setup()
        self.app.setup()
        self.assertEqual(len(self.obs.scenes["Main"]), 1)
        self.assertEqual(len(self.obs.inputs), 1)

    def test_doctor_with_obs_closed_gives_the_fix_and_stops(self):
        self.obs.down = True
        checks = self.app.doctor()
        self.assertEqual([check.name for check in checks], ["OBS connection", "FFmpeg"])
        self.assertFalse(checks[0].ok)
        self.assertIn("Enable WebSocket server", checks[0].fix)
        self.assertIn("✗ OBS connection", self.live("doctor"))


class StudioTests(AppCase):
    def test_design_scene_builds_a_finished_scene(self):
        result = self.tool("live_design_scene", name="Starting Soon", kind="starting-soon", style="synthwave", countdown=120)
        self.assertTrue(result["ok"], result)
        self.assertIn("Starting Soon", self.obs.scenes)
        self.assertEqual(self.obs.current, "Main", "the scene is not put on air unless asked")
        page = Path(result["file"]).read_text()
        self.assertIn('"style": "synthwave"', page)
        self.assertIn('"countdown": 120', page)
        item = self.obs.scenes["Starting Soon"][0]
        self.assertEqual(item["transform"]["boundsWidth"], 1920)

    def test_design_scene_accepts_the_agents_own_html_and_can_restyle(self):
        first = self.tool("live_design_scene", name="Party", html="<html><body>one</body></html>")
        second = self.tool("live_design_scene", name="Party", html="<html><body>two</body></html>", switch=True)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(Path(second["file"]).read_text(), "<html><body>two</body></html>")
        self.assertEqual(len(self.obs.scenes["Party"]), 1, "restyling reuses the source")
        self.assertEqual(self.obs.current, "Party")
        self.assertTrue(self.obs.called("PressInputPropertiesButton"), "the browser source was told to reload")

    def test_overlay_is_not_added_to_designed_scenes(self):
        self.tool("live_design_scene", name="BRB", kind="brb")
        self.app.setup()
        self.assertEqual(len(self.obs.scenes["BRB"]), 1)

    def test_sources_add_update_hide_and_remove(self):
        added = self.tool("live_source", action="add", name="Cam", kind="camera")
        self.assertTrue(added["created"])
        self.assertLess(self.obs.scenes["Main"][0]["transform"]["boundsWidth"], 1920, "a camera is not full screen by default")
        again = self.tool("live_source", action="add", name="Cam", kind="camera", placement="top-left")
        self.assertFalse(again["created"])
        self.assertEqual(len(self.obs.scenes["Main"]), 1)
        self.tool("live_source", action="hide", name="Cam")
        self.assertFalse(self.obs.scenes["Main"][0]["sceneItemEnabled"])
        self.tool("live_source", action="remove", name="Cam", delete=True)
        self.assertEqual(self.obs.inputs, {})

    def test_gif_from_disk_and_from_a_link(self):
        gif = self.dir / "party.gif"
        gif.write_bytes(b"GIF89a")
        self.assertTrue(self.tool("live_source", action="add", name="Party", kind="image", file=str(gif), placement="bottom-right")["ok"])
        self.assertEqual(self.obs.inputs["Party"]["settings"]["file"], str(gif.resolve()))
        self.assertTrue(self.tool("live_source", action="add", name="Web", kind="browser", url="https://example.test/a.gif")["ok"])
        missing = self.tool("live_source", action="add", name="Nope", kind="image", file="/no/such.gif")
        self.assertIn("no file", missing["error"])

    def test_errors_tell_the_model_how_to_recover(self):
        result = self.tool("live_scene", action="switch", name="Nope")
        self.assertEqual(result["kind"], "NotFound")
        self.assertIn("live_status", result["hint"])
        self.assertIn("Missing required argument", self.tool("live_scene")["error"])
        self.obs.down = True
        self.app.client.close()
        offline = App(self.host, self.obs.connector)
        self.addCleanup(offline.close)
        down = json.loads(tools.handler(tools.TOOLS[0], lambda: offline)({}))
        self.assertEqual(down["kind"], "ObsUnavailable")
        self.assertIn("WebSocket", down["hint"])

    def test_outputs_are_idempotent(self):
        self.assertTrue(self.tool("live_output", action="start", output="record")["changed"])
        self.assertFalse(self.tool("live_output", action="start", output="record")["changed"])
        stopped = self.tool("live_output", action="stop", output="record")
        self.assertEqual(stopped["path"], "/videos/recording.mkv")

    def test_the_last_scene_cannot_be_removed(self):
        self.assertIn("at least one scene", self.tool("live_scene", action="remove", name="Main")["error"])

    def test_raw_requests_pass_through(self):
        self.assertEqual(self.tool("live_obs_request", request="GetVersion")["response"]["platform"], "macos")

    def test_status_is_one_snapshot(self):
        self.app.setup()
        status = self.tool("live_status")
        self.assertEqual((status["scene"], status["canvas"]["width"]), ("Main", 1920))
        self.assertEqual(status["sources"][0]["name"], OVERLAY_SOURCE)
        self.assertIsNone(status["session"])


class ApprovalTests(AppCase):
    def test_public_actions_ask_and_the_setting_turns_it_off(self):
        self.assertIn("Go live", self.app.needs_approval("live_output", {"action": "start", "output": "stream"}))
        self.assertIsNone(self.app.needs_approval("live_scene", {"action": "switch", "name": "Main"}))
        self.obs.streaming = True
        self.assertIn("You are live", self.app.needs_approval("live_scene", {"action": "switch", "name": "Main"}))
        self.host.configure(confirm_public_actions=False)
        self.assertIsNone(self.app.needs_approval("live_output", {"action": "start", "output": "stream"}))

    def test_an_unreachable_obs_does_not_block_the_check(self):
        self.obs.down = True
        self.assertIsNone(self.app.needs_approval("live_scene", {"action": "switch", "name": "Main"}))


class SessionTests(AppCase):
    def test_a_mark_saves_a_clip_a_thumbnail_and_a_toast(self):
        self.app.setup()
        self.obs.recording, self.obs.record_ms = True, 83_000
        self.app.start_session("Demo")
        result = self.tool("live_mark", note="The fix lands")
        mark = result["mark"]
        self.assertTrue(result["clip"])
        self.assertEqual((mark["clip_path"], mark["position_ms"]), ("/videos/Replay 1.mkv", 83_000))
        self.assertEqual(Path(mark["thumb_path"]).read_bytes(), FAKE_IMAGE)
        self.assertEqual(self.overlay_messages()[-1]["body"], "Marked · The fix lands")
        self.assertTrue(self.obs.called("CreateRecordChapter"))
        self.assertIn(("changed", {"reason": "mark", "mark_id": mark["id"]}), self.host.events)

    def test_the_obs_save_replay_hotkey_is_a_mark(self):
        self.app.setup()
        self.app.start_session()
        self.obs.press_save_replay_hotkey()
        session = self.app.store.active_session()
        self.assertTrue(wait_for(lambda: len(self.app.store.marks(session.id)) == 1))
        self.assertTrue(wait_for(lambda: self.app.store.marks(session.id)[0].thumb_path))
        self.assertEqual(self.app.store.marks(session.id)[0].kind, "hotkey")

    def test_a_mark_made_by_the_plugin_is_not_doubled_by_its_own_event(self):
        self.app.setup()
        self.app.start_session()
        self.app.mark("one")
        self.app.mark("two")
        time.sleep(0.15)  # let ReplayBufferSaved events drain
        marks = self.app.store.marks(self.app.store.active_session().id)
        self.assertEqual([m.note for m in marks], ["one", "two"])
        self.assertEqual({m.clip_state for m in marks}, {"saved"})

    def test_marks_survive_obs_being_closed(self):
        self.obs.down = True
        result = self.app.mark("still here")
        self.assertFalse(result["clip"])
        self.assertEqual(result["mark"]["note"], "still here")
        self.assertIn("/mark", commands.help_text())
        self.assertIn("★ Marked: again", commands.run_mark(lambda: self.app, "again"))

    def test_a_session_starts_even_when_obs_is_closed(self):
        self.obs.down = True
        result = self.app.start_session("Offline")
        self.assertTrue(result["session"]["active"])
        self.assertTrue(result["warnings"])

    def test_fail_then_fix_commands_become_marks(self):
        self.app.setup()
        self.app.start_session()
        session = self.app.store.active_session()
        self.app.observe_command("conv", "npm test", 1)
        self.app.observe_command("conv", "ls", 1)
        self.app.observe_command("conv", "npm test", 0)
        self.assertTrue(wait_for(lambda: len(self.app.store.marks(session.id)) == 2))
        self.assertEqual([m.kind for m in self.app.store.marks(session.id)], ["fail", "fix"])
        tones = [message.get("tone") for message in self.overlay_messages() if message["type"] == "toast"]
        self.assertEqual(tones[-2:], ["fail", "fix"])

    def test_no_automatic_marks_outside_a_session_or_when_switched_off(self):
        self.app.observe_command("conv", "npm test", 1)
        self.app.start_session()
        self.host.configure(auto_marks=False)
        self.app.observe_command("conv", "pytest", 1)
        time.sleep(0.1)
        self.assertEqual(self.app.store.marks(self.app.store.active_session().id), [])

    def test_stop_returns_the_whole_wrapup(self):
        self.app.setup()
        self.obs.recording = True
        self.app.start_session("Demo")
        for index, note in enumerate(("Bug found", "Bug fixed")):
            self.obs.record_ms = 30_000 * (index + 1)
            self.app.mark(note)
        self.app.reel_builder._run = self.fake_ffmpeg
        with unittest.mock.patch("onair.core.reel.os.path.exists", return_value=True), \
                unittest.mock.patch("onair.core.reel.render_card", lambda path, *a, **k: Path(path).write_bytes(b"png")):
            self.host.model_reply = '{"title": "Hunting the bug", "description": "Found and fixed."}'
            result = self.tool("live_session", action="stop")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["wrapup"]["title"], "Hunting the bug")
        self.assertEqual(result["wrapup"]["chapters"], "0:00 Intro\n0:30 Bug found\n1:00 Bug fixed")
        self.assertEqual(result["wrapup"]["recording"], "/videos/recording.mkv")
        self.assertEqual(result["reel"]["clips"], 2)
        self.assertTrue(Path(result["reel"]["path"]).is_file())
        self.assertTrue(Path(result["wrapup"]["file"]).read_text().startswith("# Hunting the bug"))
        self.assertFalse(self.obs.recording, "the recording was stopped")
        self.assertIsNone(self.app.store.active_session())
        for call in self.reel_calls:
            self.assertNotIn("/videos/recording.mkv", call[-1:], "the recording is never an output")

    def test_stop_without_ffmpeg_keeps_everything_else(self):
        self.app.reel_builder = ReelBuilder(which=lambda name: None)
        self.app.setup()
        self.app.start_session("Demo")
        self.app.mark("moment")
        text = self.live("stop")
        self.assertIn("Session ended with 1 marks", text)
        self.assertIn("No reel yet", text)
        self.assertIn("Install FFmpeg", text)

    def test_stopping_with_no_session_explains_itself(self):
        self.assertIn("/live start", self.live("stop"))

    def test_rebuilding_the_reel_honours_exclusions(self):
        self.app.setup()
        self.app.start_session("Demo")
        keep = self.app.mark("keep")["mark"]
        drop = self.app.mark("drop")["mark"]
        with unittest.mock.patch("onair.core.reel.os.path.exists", return_value=True), \
                unittest.mock.patch("onair.core.reel.render_card", lambda path, *a, **k: None):
            result = self.tool("live_reel", exclude=[drop["id"]], notes={str(keep["id"]): "Kept and renamed"})
        self.assertEqual(result["clips"], 1)
        self.assertEqual(self.app.store.update_mark(keep["id"]).note, "Kept and renamed")


class ScreenTests(AppCase):
    def test_ask_reads_the_screen_and_shows_a_card(self):
        self.app.setup()
        result = self.tool("live_ask", question="what is this error?")
        self.assertEqual(result["answer"], "It is a missing import on line 3.")
        self.assertTrue(result["shown_on_overlay"])
        _, question, image = self.host.model_calls[-1]
        self.assertEqual((question, image), ("what is this error?", FAKE_IMAGE))
        kinds = [message["type"] for message in self.overlay_messages()]
        self.assertEqual(kinds[-2:], ["thinking", "card"])
        self.assertEqual(self.overlay_messages()[-1]["body"], "Missing import on line 3")

    def test_a_plain_text_reply_still_works(self):
        self.host.model_reply = "It is a typo."
        self.assertEqual(self.app.ask("what?")["card"], "It is a typo.")

    def test_a_failing_model_clears_the_thinking_card(self):
        self.host.model_reply = RuntimeError("no vision")
        result = self.tool("live_ask", question="what?")
        self.assertIn("could not answer", result["error"])
        self.assertEqual(self.overlay_messages()[-1]["type"], "hide")

    def test_pause_blocks_every_capture_meant_for_the_model(self):
        self.live("pause")
        self.assertIn("paused", self.tool("live_ask", question="what?")["error"])
        self.assertIn("paused", self.tool("live_screenshot")["error"])
        self.assertEqual(self.host.model_calls, [])
        self.live("resume")
        self.assertTrue(self.tool("live_screenshot")["ok"])

    def test_the_capture_list_is_enforced(self):
        self.tool("live_scene", action="create", name="Code")
        self.host.configure(capture_sources=("Code",))
        self.assertEqual(self.app.capture()[1], "Code", "no source named: the first allowed one is used")
        with self.assertRaises(InvalidInput):
            self.app.capture("Main")


class CommandTests(AppCase):
    def test_bare_live_is_a_status_line_or_a_diagnosis(self):
        self.assertIn("scene: Main", self.live(""))
        self.assertIn("No session running", self.live("status"))
        self.obs.down = True
        self.app.client.close()
        self.app.client._closed = False
        self.assertIn("✗ OBS connection", self.live(""))

    def test_typing_half_a_command_never_goes_live(self):
        self.assertIn("Usage", self.live("stream"))
        self.assertFalse(self.obs.streaming)
        self.assertIn("Stream started", self.live("stream start"))
        self.assertTrue(self.obs.streaming)

    def test_scene_names_are_matched_forgivingly(self):
        self.tool("live_scene", action="create", name="Camera Wide")
        self.tool("live_scene", action="create", name="Camera Close")
        self.assertEqual(self.live("scene wide"), "On air: Camera Wide")
        self.assertIn("matches several", self.live("scene camera"))
        self.assertIn("no scene like", self.live("scene zzz"))
        self.assertIn("[Camera Wide]", self.live("scenes"))

    def test_design_command(self):
        self.assertIn("Kinds:", self.live("design"))
        self.assertIn("Scene 'Friday build' created (synthwave)", self.live("design starting-soon synthwave Friday build"))
        self.assertIn("Scene 'Brb' created", self.live("design brb"))

    def test_fun_commands_reach_the_overlay(self):
        self.app.setup()
        self.live("celebrate")
        self.live("celebrate 🔥")
        self.live("gif https://example.test/party.gif")
        self.live("card Ship it")
        self.live("chapter Part two")
        shown = [(m["type"], m.get("name") or m.get("url") or m.get("body") or m.get("title")) for m in self.overlay_messages()[-5:]]
        self.assertEqual(shown, [("effect", "confetti"), ("effect", "emoji"), ("sticker", "https://example.test/party.gif"),
                                 ("card", "Ship it"), ("chapter", "Part two")])

    def test_help_and_unknown(self):
        self.assertIn("/live setup", self.live("help"))
        self.assertIn("Unknown: /live nonsense", self.live("nonsense"))


class RpcTests(AppCase):
    def call(self, method: str, /, **params) -> dict:
        return rpc.serve(rpc.METHODS[method], lambda: self.app)(params, session_id=None)

    def test_state_is_everything_the_pane_needs(self):
        self.app.setup()
        self.app.start_session("Demo")
        self.app.mark("moment")
        state = self.call("state")
        self.assertTrue(state["ok"])
        self.assertTrue(state["obs"]["connected"])
        self.assertEqual(state["session"]["mark_count"], 1)
        self.assertTrue(state["marks"][0]["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertIn("synthwave", state["design"]["styles"])
        json.dumps(state)

    def test_state_with_obs_closed_still_answers(self):
        self.obs.down = True
        state = self.call("state")
        self.assertTrue(state["ok"])
        self.assertFalse(state["obs"]["connected"])
        self.assertIn("WebSocket", state["obs"]["hint"])

    def test_errors_come_back_as_data(self):
        result = self.call("scene", action="switch", name="Nope")
        self.assertFalse(result["ok"])
        self.assertIn("no scene", result["error"])

    def test_pane_actions(self):
        self.assertTrue(self.call("preview")["image"].startswith("data:image/jpeg"))
        self.assertTrue(self.call("output", action="start", output="record")["ok"])
        mark = self.call("mark", note="from the pane")["mark"]
        self.assertFalse(self.call("mark.update", id=mark["id"], included=False)["mark"]["included"])
        self.assertTrue(self.call("capture", paused=True)["capture_paused"])
        self.assertTrue(self.call("design", name="BRB", kind="brb")["created"])

    def test_reel_builds_in_the_background_and_reports_by_event(self):
        self.app.setup()
        self.app.start_session("Demo")
        self.app.mark("moment")
        with unittest.mock.patch("onair.core.reel.os.path.exists", return_value=True), \
                unittest.mock.patch("onair.core.reel.render_card", lambda path, *a, **k: None):
            self.assertTrue(self.call("reel")["started"])
            self.assertTrue(wait_for(lambda: any(event == "reel" for event, _ in self.host.events)))
        payload = next(payload for event, payload in self.host.events if event == "reel")
        self.assertTrue(payload["ok"], payload)


class ManifestTests(unittest.TestCase):
    def test_manifest_lists_exactly_the_registered_tools(self):
        root = Path(__file__).resolve().parent.parent
        declared = [line.strip()[2:] for line in (root / "plugin.yaml").read_text().splitlines()
                    if line.strip().startswith("- live_")]
        self.assertEqual(sorted(declared), sorted(tool.name for tool in tools.TOOLS))

    def test_every_tool_schema_is_well_formed(self):
        for tool in tools.TOOLS:
            schema = tool.schema
            self.assertGreater(len(schema["description"]), 60, tool.name)
            for name in tool.required:
                self.assertIn(name, schema["parameters"]["properties"], tool.name)

    def test_register_wires_everything_without_touching_obs(self):
        class Ctx:
            def __init__(self):
                self.tools, self.commands, self.rpcs, self.hooks, self.skills, self.unload = [], [], [], [], [], []

            def register_tool(self, **kw): self.tools.append(kw["name"])
            def register_command(self, name, handler, **kw): self.commands.append(name)
            def register_rpc(self, name, handler): self.rpcs.append(name)
            def register_hook(self, name, callback): self.hooks.append((name, callback))
            def register_skill(self, name, path): self.skills.append((name, Path(path)))
            def on_unload(self, callback): self.unload.append(callback)

        import onair
        ctx = Ctx()
        onair.register(ctx)
        self.assertEqual(len(ctx.tools), 12)
        self.assertEqual(ctx.commands, ["live", "onair", "mark"])
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_tool_call", "post_tool_call"])
        self.assertTrue(ctx.skills[0][1].is_file())
        # Hooks ignore other plugins' tools without building anything.
        for _, hook in ctx.hooks:
            self.assertIsNone(hook(tool_name="read_file", args={}, result="{}"))
        ctx.unload[0]()


import unittest.mock  # noqa: E402  (used by the patch() calls above)

if __name__ == "__main__":
    unittest.main()
