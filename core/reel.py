"""After the session: the highlight reel, YouTube chapters, and a draft title and description.

Planning is pure (marks in, segments out) and is where the decisions live. Rendering shells out
to FFmpeg with fixed argument lists and only ever writes new files next to the session's data:
the recording and the replay clips are read, never modified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .errors import ReelUnavailable
from .store import Mark, Session

CARD_SECONDS = 1.8
KIND_LABELS = {"manual": "Moment", "hotkey": "Moment", "fail": "It broke", "fix": "Fixed",
               "ask": "Question", "chapter": "Chapter"}
YOUTUBE_MIN_GAP_S = 10

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class Segment:
    mark_id: int
    label: str                # small text on the chapter card
    title: str                # large text on the chapter card
    source: str               # file to cut from
    start_s: Optional[float]  # cut start, or None to take the last ``duration_s`` of the file
    duration_s: float


def mark_title(mark: Mark) -> str:
    return mark.note or KIND_LABELS.get(mark.kind, "Moment")


def plan_reel(marks: Sequence[Mark], recording_path: str = "", *, clip_seconds: float = 20,
              lead_in: float = 12, tail: float = 6,
              exists: Optional[Callable[[str], bool]] = None) -> tuple[list[Segment], list[int]]:
    """Choose what each included mark contributes. Returns ``(segments, skipped mark ids)``.

    A mark's own replay clip wins: it exists the moment the mark is made. Without one, the
    moment is cut out of the full recording around the mark's position."""
    exists = exists or os.path.exists
    segments: list[Segment] = []
    skipped: list[int] = []
    have_recording = bool(recording_path) and exists(recording_path)
    for mark in marks:
        if not mark.included:
            continue
        label, title = KIND_LABELS.get(mark.kind, "Moment"), mark_title(mark)
        if mark.clip_state == "saved" and mark.clip_path and exists(mark.clip_path):
            segments.append(Segment(mark.id, label, title, mark.clip_path, None, clip_seconds))
        elif have_recording and mark.position_ms is not None:
            start = max(mark.position_ms / 1000 - lead_in, 0.0)
            segments.append(Segment(mark.id, label, title, recording_path, start,
                                    mark.position_ms / 1000 - start + tail))
        else:
            skipped.append(mark.id)
    return segments, skipped


def timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, minutes, secs = total // 3600, total % 3600 // 60, total % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def youtube_chapters(marks: Sequence[Mark]) -> list[tuple[int, str]]:
    """``(second, title)`` chapters for the full recording, following YouTube's rules: the first
    chapter is at 0:00 and chapters are at least ten seconds apart."""
    chapters: list[tuple[int, str]] = [(0, "Intro")]
    for mark in sorted((m for m in marks if m.position_ms is not None), key=lambda m: m.position_ms or 0):
        second = int((mark.position_ms or 0) / 1000)
        if second - chapters[-1][0] >= YOUTUBE_MIN_GAP_S:
            chapters.append((second, mark_title(mark)))
    return chapters if len(chapters) > 1 else []


def format_chapters(chapters: Sequence[tuple[int, str]]) -> str:
    return "\n".join(f"{timestamp(second)} {title}" for second, title in chapters)


def draft_wrapup(session: Session, marks: Sequence[Mark],
                 ask_model: Optional[Callable[[str], str]] = None) -> dict[str, Any]:
    """Title, description, show notes and chapters. Always returns a usable draft: the model
    only improves the title and description, and its failure falls back to plain text."""
    chapters = youtube_chapters(marks)
    notes = [f"- {KIND_LABELS.get(m.kind, 'Moment')}: {m.note}" for m in marks if m.included and m.note]
    title = session.title or "Live session"
    description = f"{len(marks)} marked moments." if marks else "Recorded with Hermes On Air."
    if ask_model and marks:
        prompt = (
            "Write a title and a description for a screen recording.\n"
            f"Working title: {title}\nMarked moments, in order:\n" + "\n".join(notes or ["- (no notes)"]) +
            "\n\nReply with JSON only: {\"title\": \"...\", \"description\": \"...\"}. "
            "Title under 70 characters, plain and specific, no clickbait. "
            "Description: two or three plain sentences saying what happens.")
        try:
            reply = ask_model(prompt)
            data = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
            title = str(data.get("title") or title).strip()[:100]
            description = str(data.get("description") or description).strip()[:1200]
        except Exception:  # any model or parsing failure keeps the plain draft
            pass
    return {"title": title, "description": description, "show_notes": "\n".join(notes),
            "chapters": format_chapters(chapters), "draft": True}


class ReelBuilder:
    def __init__(self, run: Optional[Runner] = None, which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self._run = run or _run
        self._which = which

    def available(self) -> bool:
        return bool(self._which("ffmpeg") and self._which("ffprobe"))

    def build(self, segments: Sequence[Segment], out_path: Path, *, title: str = "",
              accent: str = "#ffb000") -> Path:
        """Render ``segments`` (each behind a chapter card) into ``out_path``."""
        if not self.available():
            raise ReelUnavailable("FFmpeg is not installed, so the reel cannot be rendered.")
        if not segments:
            raise ReelUnavailable("None of the marks has a clip or a recording to cut from.",
                                  hint="Start the replay buffer or record during the session (/live setup does both).")
        work = out_path.parent / f".{out_path.stem}-parts"
        work.mkdir(parents=True, exist_ok=True)
        try:
            width, height, fps = self._video_shape(segments[0].source)
            encoder = self._encoder()
            parts: list[Path] = []
            if title:
                parts.append(self._card(work / "title", "Highlights", title, width, height, fps, encoder, accent))
            for index, segment in enumerate(segments):
                parts.append(self._card(work / f"card-{index}", segment.label, segment.title,
                                        width, height, fps, encoder, accent))
                parts.append(self._cut(segment, work / f"clip-{index}.mp4", width, height, fps, encoder))
            listing = work / "parts.txt"
            listing.write_text("".join(f"file '{part.as_posix()}'\n" for part in parts), "utf-8")
            self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                          "-movflags", "+faststart", str(out_path)], "joining the reel")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return out_path

    # ------------------------------------------------------------------ steps

    def _video_shape(self, source: str) -> tuple[int, int, int]:
        result = self._run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate", "-of", "json", source])
        try:
            stream = json.loads(result.stdout)["streams"][0]
            num, _, den = str(stream.get("r_frame_rate", "30/1")).partition("/")
            fps = round(float(num) / float(den or 1))
            return int(stream["width"]) // 2 * 2, int(stream["height"]) // 2 * 2, min(max(fps, 15), 60)
        except (ValueError, KeyError, IndexError, ZeroDivisionError):
            return 1920, 1080, 30

    def _has_audio(self, source: str) -> bool:
        result = self._run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_type", "-of", "csv=p=0", source])
        return "audio" in (result.stdout or "")

    def _encoder(self) -> list[str]:
        listed = self._run(["ffmpeg", "-hide_banner", "-encoders"]).stdout or ""
        if "libx264" in listed:
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        if "h264_videotoolbox" in listed:
            return ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
        return ["-c:v", "mpeg4", "-q:v", "4"]

    def _output_args(self, width: int, height: int, fps: int, encoder: list[str]) -> list[str]:
        scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1")
        return ["-vf", scale, *encoder, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                "-video_track_timescale", "90000"]

    def _cut(self, segment: Segment, out: Path, width: int, height: int, fps: int, encoder: list[str]) -> Path:
        seek = (["-sseof", f"-{segment.duration_s:g}"] if segment.start_s is None
                else ["-ss", f"{segment.start_s:g}", "-t", f"{segment.duration_s:g}"])
        args = [*seek, "-i", segment.source]
        if self._has_audio(segment.source):
            args += ["-map", "0:v:0", "-map", "0:a:0"]
        else:  # give silent clips a silent track so every part has the same streams
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                     "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        self._ffmpeg([*args, *self._output_args(width, height, fps, encoder), str(out)],
                     f"cutting the clip for '{segment.title}'")
        return out

    def _card(self, stem: Path, label: str, title: str, width: int, height: int, fps: int,
              encoder: list[str], accent: str) -> Path:
        image, video = stem.with_suffix(".png"), stem.with_suffix(".mp4")
        render_card(image, label, title, width, height, accent)
        self._ffmpeg(["-loop", "1", "-framerate", str(fps), "-t", f"{CARD_SECONDS:g}", "-i", str(image),
                      "-f", "lavfi", "-t", f"{CARD_SECONDS:g}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                      "-map", "0:v:0", "-map", "1:a:0", *self._output_args(width, height, fps, encoder),
                      "-shortest", str(video)], "rendering a chapter card")
        return video

    def _ffmpeg(self, args: list[str], doing: str) -> None:
        result = self._run(["ffmpeg", "-y", "-v", "error", "-nostdin", *args])
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()[-1:] or ["no error output"]
            raise ReelUnavailable(f"FFmpeg failed while {doing}: {detail[0]}",
                                  hint="Your marks and clips are untouched. Fix the problem and rebuild the reel.")


def render_card(path: Path, label: str, title: str, width: int, height: int, accent: str = "#ffb000") -> None:
    """Draw a chapter card with Pillow (ships with Hermes), so no FFmpeg text filter is needed."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), "#0b0c10")
    draw = ImageDraw.Draw(image)
    unit = width / 100
    title_font = ImageFont.load_default(size=int(unit * 5.2))
    label_font = ImageFont.load_default(size=int(unit * 1.5))
    lines = _wrap(draw, title, title_font, width * 0.8)[:3]
    line_height = int(unit * 6.2)
    top = (height - line_height * len(lines)) / 2
    draw.rectangle([unit * 10, top - unit * 3.2, unit * 10 + unit * 5, top - unit * 2.8], fill=accent)
    draw.text((unit * 10, top - unit * 2.2), " ".join(label.upper()), font=label_font, fill=accent)
    for index, line in enumerate(lines):
        draw.text((unit * 10, top + unit * 0.6 + index * line_height), line, font=title_font, fill="#f5f3ee")
    image.save(path)


def _wrap(draw: Any, text: str, font: Any, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return [*lines, current] if current else lines or [""]


def _run(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(args), 1, "", str(exc))
