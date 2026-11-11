"""Pure logic: placements, the mark store, automatic marks, the approval policy, reel planning."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from onair.core import guard
from onair.core.automark import AutoMarker
from onair.core.errors import InvalidInput
from onair.core.reel import draft_wrapup, format_chapters, plan_reel, timestamp, youtube_chapters
from onair.core.weburl import public_web_url
from onair.core.stage import media_url, render_scene, scene_config, slugify
from onair.core.store import Store
from onair.core.studio import obs_color, placement_rect, placement_transform, source_settings


class PlacementTests(unittest.TestCase):
    def test_fullscreen_covers_the_canvas(self):
        transform = placement_transform("fullscreen", 1920, 1080)
        self.assertEqual((transform["positionX"], transform["positionY"]), (0, 0))
        self.assertEqual((transform["boundsWidth"], transform["boundsHeight"]), (1920, 1080))
        self.assertEqual(transform["boundsType"], "OBS_BOUNDS_SCALE_OUTER")

    def test_corners_stay_inside_the_canvas(self):
        for placement in ("top-left", "top-right", "bottom-left", "bottom-right", "center"):
            for scale in (None, 0.05, 0.5, 1.0, 9.0):
                rect = placement_rect(placement, scale)
                self.assertGreaterEqual(rect.x, 0, placement)
                self.assertGreaterEqual(rect.y, 0, placement)
                self.assertLessEqual(rect.w, 1)

    def test_bottom_right_is_bottom_right(self):
        rect = placement_rect("bottom-right", 0.25)
        self.assertGreater(rect.x, 0.5)
        self.assertGreater(rect.y, 0.5)

    def test_unknown_placement_lists_the_valid_ones(self):
        with self.assertRaises(InvalidInput) as failure:
            placement_rect("middle")
        self.assertIn("bottom-right", str(failure.exception))

    def test_colours(self):
        self.assertEqual(obs_color("#ff0000"), 0xFF0000FF)  # OBS stores ABGR
        self.assertEqual(obs_color("#0f0"), 0xFF00FF00)
        with self.assertRaises(InvalidInput):
            obs_color("red")

    def test_source_settings_say_what_is_missing(self):
        self.assertEqual(source_settings("browser", canvas=(1920, 1080), url="https://x.test")["width"], 1920)
        with self.assertRaises(InvalidInput):
            source_settings("browser", canvas=(1920, 1080))
        with self.assertRaises(InvalidInput):
            source_settings("image", canvas=(1920, 1080))


class StageHelperTests(unittest.TestCase):
    def test_scene_config_merges_kind_defaults_with_overrides(self):
        config = scene_config("starting-soon", title="Friday stream", countdown=90, style="matrix", accent="#00ff88")
        self.assertEqual((config["title"], config["countdown"], config["style"]), ("Friday stream", 90, "matrix"))
        self.assertEqual(config["kicker"], "Stream")

    def test_scene_config_rejects_bad_input(self):
        for bad in ({"kind": "nope"}, {"kind": "brb", "style": "plaid"}, {"kind": "brb", "accent": "blue"}, {"kind": "title"}):
            with self.assertRaises(InvalidInput):
                scene_config(**bad)

    def test_rendered_config_cannot_break_out_of_the_script_tag(self):
        html = render_scene('<script>const CONFIG = "__ONAIR_CONFIG__";</script>', {"title": "</script><b>x"})
        self.assertNotIn("</script><b>", html)
        self.assertEqual(html.count("</script>"), 1)

    def test_media_url_accepts_links_and_real_files_only(self):
        self.assertEqual(media_url("https://example.test/a.gif"), "https://example.test/a.gif")
        with tempfile.NamedTemporaryFile(suffix=".gif") as handle:
            self.assertTrue(media_url(handle.name).startswith("file://"))
        with self.assertRaises(InvalidInput):
            media_url("/no/such/file.gif")

    def test_media_url_refuses_file_links_private_hosts_and_non_images(self):
        for reference in ("file:///etc/passwd", "http://127.0.0.1:8080/a.gif", "http://private.test/a.gif",
                          "ftp://example.test/a.gif", "javascript://x"):
            with self.assertRaises(InvalidInput, msg=reference):
                media_url(reference)
        with tempfile.NamedTemporaryFile(suffix=".html") as handle, self.assertRaises(InvalidInput):
            media_url(handle.name)
        self.assertTrue(media_url("data:image/png;base64,AAAA").startswith("data:image/"))


class WebUrlTests(unittest.TestCase):
    def test_public_http_and_https_pass(self):
        for url in ("https://example.test/page", "http://example.test:8080/x?y=1", "https://93.184.216.34/"):
            self.assertEqual(public_web_url(url), url)

    def test_non_web_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "chrome://settings", "data:text/html,<b>x", "javascript:alert(1)",
                    "ftp://example.test/", "about:blank", "example.test/no-scheme", ""):
            with self.assertRaises(InvalidInput, msg=url):
                public_web_url(url)

    def test_private_loopback_and_link_local_addresses_are_refused(self):
        for url in ("http://127.0.0.1/", "http://localhost/", "http://10.1.2.3/", "http://192.168.1.1/admin",
                    "http://172.16.0.9/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                    "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://2130706433/",
                    "http://private.test/", "http://mixed.test/", "http://missing.test/", "http:///nohost",
                    "http://127.0.0.1\\@example.test/", "http://169.254.169.254\\@example.test/latest/",
                    "http://example.test\\@127.0.0.1/", "http://user:pw@example.test/", "http://127.0.0.1@example.test/",
                    "http://exa\tmple.test/", "http://example.test/a b", "http://example.test:99999/", "http://example.test:x/"):
            resolve = (lambda host: {"127.0.0.1"}) if "localhost" in url or "2130706433" in url else None
            with self.assertRaises(InvalidInput, msg=url):
                public_web_url(url, resolve)

    def test_slugs(self):
        self.assertEqual(slugify("Starting Soon!"), "starting-soon")
        self.assertEqual(slugify("???"), "scene")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.db"
        self.now = [1000.0]
        self.store = self.open()

    def open(self) -> Store:
        store = Store(lambda: sqlite3.connect(self.path, check_same_thread=False), clock=lambda: self.now[0])
        self.addCleanup(store.close)
        return store

    def test_starting_a_session_closes_a_forgotten_one(self):
        first = self.store.start_session("one")
        second = self.store.start_session("two")
        self.assertEqual(self.store.active_session().id, second.id)
        self.assertFalse(self.store.session(first.id).active)

    def test_a_saved_replay_goes_to_the_mark_that_asked_for_it(self):
        session = self.store.start_session()
        first = self.store.add_mark(session.id, "manual", "first", expects_clip=True)
        second = self.store.add_mark(session.id, "manual", "second", expects_clip=True)
        self.assertEqual(self.store.claim_clip(session.id, "/a.mkv", from_hotkey=False).id, first.id)
        self.assertEqual(self.store.claim_clip(session.id, "/b.mkv", from_hotkey=True).id, second.id)
        self.assertEqual([m.clip_path for m in self.store.marks(session.id)], ["/a.mkv", "/b.mkv"])

    def test_the_same_replay_reported_twice_is_one_mark(self):
        """Two processes both see ReplayBufferSaved; a second Store on the same file stands in for the other."""
        session = self.store.start_session()
        other = self.open()
        mark = self.store.add_mark(session.id, "manual", "note", expects_clip=True)
        self.assertEqual(other.claim_clip(session.id, "/a.mkv", from_hotkey=True).id, mark.id)
        self.assertEqual(self.store.claim_clip(session.id, "/a.mkv", from_hotkey=True).id, mark.id)
        self.assertEqual(self.store.claim_clip(session.id, "/a.mkv", from_hotkey=False).id, mark.id)
        self.assertEqual(len(self.store.marks(session.id)), 1)

    def test_the_obs_hotkey_makes_a_mark_of_its_own(self):
        session = self.store.start_session()
        mark = self.store.claim_clip(session.id, "/hot.mkv", from_hotkey=True)
        self.assertEqual((mark.kind, mark.clip_state), ("hotkey", "saved"))

    def test_a_poll_never_invents_a_mark(self):
        session = self.store.start_session()
        self.assertIsNone(self.store.claim_clip(session.id, "/a.mkv", from_hotkey=False))
        self.assertEqual(self.store.marks(session.id), [])

    def test_a_mark_that_waited_too_long_does_not_steal_a_later_clip(self):
        session = self.store.start_session()
        stale = self.store.add_mark(session.id, "manual", "stale", expects_clip=True)
        self.now[0] += 120
        claimed = self.store.claim_clip(session.id, "/late.mkv", from_hotkey=True)
        self.assertNotEqual(claimed.id, stale.id)
        self.assertEqual(self.store.update_mark(stale.id).clip_state, "none")

    def test_marks_can_be_edited_excluded_and_deleted(self):
        session = self.store.start_session()
        mark = self.store.add_mark(session.id, "manual", " draft ")
        self.assertEqual(mark.note, "draft")
        edited = self.store.update_mark(mark.id, note="final", included=False)
        self.assertEqual((edited.note, edited.included), ("final", False))
        self.assertTrue(self.store.delete_mark(mark.id))
        self.assertIsNone(self.store.update_mark(mark.id, note="gone"))

    def test_data_survives_reopening(self):
        session = self.store.start_session("kept")
        self.store.add_mark(session.id, "fix", "it works", position_ms=42_000)
        self.store.update_session(session.id, wrapup={"title": "T"})
        self.store.close()
        reopened = self.open()
        self.assertEqual(reopened.latest_session().wrapup, {"title": "T"})
        self.assertEqual(reopened.marks(session.id)[0].position_ms, 42_000)


class AutoMarkerTests(unittest.TestCase):
    def test_fail_then_fix_is_one_pair(self):
        marker = AutoMarker()
        self.assertEqual(marker.observe("c", "npm test", 1).kind, "fail")
        self.assertIsNone(marker.observe("c", "npm test", 1), "a retry that still fails is not a second mark")
        self.assertEqual(marker.observe("c", "npm  test", 0).kind, "fix", "spacing does not matter")
        self.assertIsNone(marker.observe("c", "npm test", 0), "passing again is not news")

    def test_a_pass_with_no_earlier_failure_is_not_marked(self):
        self.assertIsNone(AutoMarker().observe("c", "cargo build", 0))

    def test_uninteresting_commands_are_ignored(self):
        marker = AutoMarker()
        for command in ("grep foo bar.txt", "ls missing", "test -f x", "cd nowhere"):
            self.assertIsNone(marker.observe("c", command, 1), command)

    def test_conversations_do_not_share_failures(self):
        marker = AutoMarker()
        marker.observe("a", "pytest", 1)
        self.assertIsNone(marker.observe("b", "pytest", 0))

    def test_memory_is_bounded(self):
        marker = AutoMarker()
        for index in range(500):
            marker.observe(f"conversation-{index}", f"pytest tests/test_{index}.py", 1)
            marker.observe("one", f"pytest tests/test_{index}.py", 1)
        self.assertLessEqual(len(marker._failing), 64)
        self.assertLessEqual(len(marker._failing["one"]), 32)

    def test_missing_exit_code_is_ignored(self):
        self.assertIsNone(AutoMarker().observe("c", "pytest", None))


class GuardTests(unittest.TestCase):
    def review(self, tool, streaming=False, **args):
        return guard.review(tool, args, lambda: streaming)

    def test_going_live_and_ending_the_stream_always_ask(self):
        self.assertIn("Go live", self.review("live_output", action="start", output="stream"))
        self.assertIn("End the stream", self.review("live_output", action="stop", output="stream"))

    def test_starting_a_recording_does_not_ask_but_stopping_does(self):
        self.assertIsNone(self.review("live_output", action="start", output="record"))
        self.assertTrue(self.review("live_output", action="stop", output="record"))

    def test_scene_changes_ask_only_while_live(self):
        self.assertIsNone(self.review("live_scene", action="switch", name="Cam"))
        self.assertIn("You are live", self.review("live_scene", streaming=True, action="switch", name="Cam"))
        self.assertTrue(self.review("live_scene", action="remove", name="Cam"), "deleting always asks")

    def test_source_changes_ask_only_while_live(self):
        self.assertIsNone(self.review("live_source", action="hide", name="Cam"))
        self.assertTrue(self.review("live_source", streaming=True, action="hide", name="Cam"))
        self.assertTrue(self.review("live_source", action="remove", name="Cam", delete=True))

    def test_overlay_marks_and_reads_never_ask(self):
        for tool, args in (("live_overlay", {"action": "card"}), ("live_mark", {}), ("live_status", {}),
                           ("live_ask", {"question": "?"}), ("live_obs_request", {"request": "GetInputList"})):
            self.assertIsNone(guard.review(tool, args, lambda: True), tool)

    def test_raw_writes_always_ask_and_show_their_data(self):
        question = guard.safety("live_obs_request", {"request": "SetRecordDirectory", "data": {"recordDirectory": "/tmp/x"}})
        self.assertIn("SetRecordDirectory", question)
        self.assertIn("/tmp/x", question)
        long = guard.safety("live_obs_request", {"request": "SetInputSettings", "data": {"blob": "y" * 5000}})
        self.assertIn("truncated", long)
        self.assertLess(len(long), 500)
        self.assertIsNone(guard.safety("live_obs_request", {"request": "GetInputList"}))

    def test_local_and_agent_written_pages_always_ask(self):
        self.assertIn("local file", guard.safety("live_source", {"action": "add", "name": "Page", "kind": "browser", "file": "/tmp/a.html"}))
        update = {"action": "update", "name": "Page", "file": "/tmp/page.png"}
        self.assertIn("local file", guard.safety("live_source", update), "kind unknown: ask")
        self.assertIn("local file", guard.safety("live_source", update, lambda name: ""), "kind unknown: ask")
        self.assertIn("local file", guard.safety("live_source", update, lambda name: "browser_source"), "the extension does not matter")
        self.assertIsNone(guard.safety("live_source", update, lambda name: "image_source"))
        self.assertIn("written by the agent", guard.safety("live_design_scene", {"name": "Intro", "html": "<b>x</b>"}))

    def test_ordinary_sources_and_built_in_scenes_do_not_ask(self):
        for tool, args in (("live_source", {"action": "add", "name": "Web", "kind": "browser", "url": "https://example.test"}),
                           ("live_source", {"action": "add", "name": "Pic", "kind": "image", "file": "/tmp/a.png"}),
                           ("live_source", {"action": "update", "name": "Label", "text": "hi"}),
                           ("live_design_scene", {"name": "Intro", "kind": "starting-soon"}),
                           ("live_design_scene", {"name": "Intro", "background": "/tmp/a.mp4"})):
            self.assertIsNone(guard.safety(tool, args), args)


class ReelPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(lambda: sqlite3.connect(Path(self.directory.name) / "d.db", check_same_thread=False))
        self.addCleanup(self.store.close)
        self.session = self.store.start_session("Demo")

    def mark(self, note, *, position=None, clip="", kind="manual", included=True):
        mark = self.store.add_mark(self.session.id, kind, note, position_ms=position, expects_clip=bool(clip))
        if clip:
            self.store.claim_clip(self.session.id, clip, from_hotkey=False)
        if not included:
            self.store.update_mark(mark.id, included=False)
        return self.store.update_mark(mark.id)

    def test_a_marks_own_clip_wins_over_the_recording(self):
        marks = [self.mark("with clip", position=60_000, clip="/clip.mkv")]
        segments, skipped = plan_reel(marks, "/rec.mkv", exists=lambda path: True)
        self.assertEqual((segments[0].source, segments[0].start_s), ("/clip.mkv", None))
        self.assertEqual(skipped, [])

    def test_without_a_clip_the_moment_is_cut_from_the_recording(self):
        marks = [self.mark("late", position=60_000), self.mark("early", position=3_000)]
        segments, _ = plan_reel(marks, "/rec.mkv", lead_in=12, tail=6, exists=lambda path: True)
        self.assertEqual((segments[0].start_s, segments[0].duration_s), (48.0, 18.0))
        self.assertEqual((segments[1].start_s, segments[1].duration_s), (0.0, 9.0), "never before the start")

    def test_excluded_marks_and_marks_with_no_footage_are_left_out(self):
        marks = [self.mark("excluded", clip="/a.mkv", included=False), self.mark("nothing to cut from"),
                 self.mark("deleted file", clip="/gone.mkv")]
        segments, skipped = plan_reel(marks, "", exists=lambda path: False)
        self.assertEqual(segments, [])
        self.assertEqual(skipped, [marks[1].id, marks[2].id])

    def test_youtube_chapters_follow_youtube_rules(self):
        marks = [self.mark("Setup", position=45_000), self.mark("too close", position=50_000),
                 self.mark("", position=3_700_000, kind="fix")]
        chapters = youtube_chapters(marks)
        self.assertEqual(chapters, [(0, "Intro"), (45, "Setup"), (3700, "Fixed")])
        self.assertEqual(format_chapters(chapters), "0:00 Intro\n0:45 Setup\n1:01:40 Fixed")
        self.assertEqual(youtube_chapters([self.mark("no position")]), [])

    def test_timestamps(self):
        self.assertEqual([timestamp(s) for s in (0, 59, 61, 3600)], ["0:00", "0:59", "1:01", "1:00:00"])

    def test_wrapup_uses_the_model_and_survives_its_failure(self):
        marks = [self.mark("Null pointer in upload", position=30_000, kind="fail")]
        good = draft_wrapup(self.session, marks, lambda prompt: 'Sure! {"title": "Fixing the upload crash", "description": "We fix it."}')
        self.assertEqual(good["title"], "Fixing the upload crash")
        self.assertIn("0:30 Null pointer in upload", good["chapters"])

        def broken(prompt):
            raise RuntimeError("model offline")

        for model in (broken, lambda prompt: "no json here", None):
            fallback = draft_wrapup(self.session, marks, model)
            self.assertEqual(fallback["title"], "Demo")
            self.assertIn("It broke: Null pointer in upload", fallback["show_notes"])
            self.assertTrue(fallback["draft"])


if __name__ == "__main__":
    unittest.main()
