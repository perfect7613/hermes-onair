"""The reel renderer against the real FFmpeg and Pillow. Skipped where either is missing."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from onair.core.errors import ReelUnavailable
from onair.core.reel import CARD_SECONDS, ReelBuilder, Segment

HAVE_TOOLS = bool(shutil.which("ffmpeg") and shutil.which("ffprobe") and importlib.util.find_spec("PIL"))


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def streams(path: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return sorted(out.split())


@unittest.skipUnless(HAVE_TOOLS, "needs ffmpeg, ffprobe and Pillow")
class ReelRenderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def clip(self, name: str, seconds: int, *, audio: bool, size: str = "640x360") -> Path:
        path = self.dir / name
        args = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        subprocess.run([*args, "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    def test_clips_of_different_shapes_become_one_playable_reel(self):
        replay = self.clip("replay.mkv", 6, audio=True)                    # a replay-buffer clip
        recording = self.clip("recording.mp4", 12, audio=False, size="1280x720")  # a silent full recording
        segments = [Segment(1, "It broke", "The build fails on a missing import", str(replay), None, 4),
                    Segment(2, "Fixed", "Green again", str(recording), 5.0, 3.0)]
        before = (replay.stat().st_size, replay.stat().st_mtime, recording.stat().st_size, recording.stat().st_mtime)

        out = ReelBuilder().build(segments, self.dir / "reel.mp4", title="Fixing the build")

        self.assertEqual(streams(out), ["audio", "video"])
        expected = 3 * CARD_SECONDS + 4 + 3  # title card + two chapter cards + two cuts
        self.assertAlmostEqual(duration(out), expected, delta=1.0)
        after = (replay.stat().st_size, replay.stat().st_mtime, recording.stat().st_size, recording.stat().st_mtime)
        self.assertEqual(before, after, "source footage is never modified")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["recording.mp4", "reel.mp4", "replay.mkv"],
                         "no temporary files are left behind")

    def test_a_broken_source_is_reported_and_cleaned_up(self):
        bad = self.dir / "broken.mkv"
        bad.write_bytes(b"not a video")
        with self.assertRaises(ReelUnavailable) as failure:
            ReelBuilder().build([Segment(1, "Moment", "x", str(bad), None, 4)], self.dir / "reel.mp4")
        self.assertIn("untouched", failure.exception.hint)
        self.assertFalse((self.dir / "reel.mp4").exists())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["broken.mkv"])


class ReelAvailabilityTests(unittest.TestCase):
    def test_no_ffmpeg_and_no_footage_are_different_messages(self):
        with self.assertRaises(ReelUnavailable) as missing:
            ReelBuilder(which=lambda name: None).build([], Path("/tmp/x.mp4"))
        self.assertIn("not installed", str(missing.exception))
        with self.assertRaises(ReelUnavailable) as empty:
            ReelBuilder(which=lambda name: "/bin/x").build([], Path("/tmp/x.mp4"))
        self.assertIn("replay buffer", empty.exception.hint)


if __name__ == "__main__":
    unittest.main()
