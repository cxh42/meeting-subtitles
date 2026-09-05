"""Persist meeting transcripts to disk.

Server updates are full snapshots, so the recorder keeps only the newest one and
rewrites its output files from it. Writes are atomic (temp file + rename) and
throttled, so a crash or a hard kill mid-meeting still leaves a readable
transcript on disk rather than a truncated one.
"""

import json
import logging
import os
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from meeting_subtitles.model import Line, Snapshot

logger = logging.getLogger(__name__)


def _srt_timestamp(value: str) -> str:
    """Convert the server's ``H:MM:SS`` / ``MM:SS`` stamp into SRT format."""
    parts = [p for p in str(value).split(":") if p != ""]
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return "00:00:00,000"
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    hours, minutes, seconds = numbers[-3], numbers[-2], numbers[-1]
    total = hours * 3600 + minutes * 60 + seconds
    hh, rem = divmod(int(total), 3600)
    mm, ss = divmod(rem, 60)
    ms = int(round((total - int(total)) * 1000))
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


class TranscriptRecorder:
    """Write ``transcript.md``, ``transcript.srt`` and ``transcript.json``."""

    #: Everything the recorder knows how to write. Markdown is the default and
    #: usually the only one wanted: it reads well and pastes straight into an
    #: LLM for a summary. srt only helps alongside a video, json only helps
    #: another program.
    FORMATS = ("md", "srt", "json")

    def __init__(
        self,
        directory: Path,
        title: str = "Meeting",
        min_interval: float = 3.0,
        formats: Sequence[str] = ("md",),
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.title = title
        unknown = [f for f in formats if f not in self.FORMATS]
        if unknown:
            raise ValueError(f"未知的转录格式: {', '.join(unknown)}")
        self.formats = tuple(formats) or ("md",)
        self.min_interval = min_interval
        self.started_at = datetime.now()
        self._snapshot: Snapshot | None = None
        self._last_write = 0.0
        self._dirty = False

    @property
    def markdown_path(self) -> Path:
        return self.directory / "transcript.md"

    @property
    def srt_path(self) -> Path:
        return self.directory / "transcript.srt"

    @property
    def json_path(self) -> Path:
        return self.directory / "transcript.json"

    @property
    def audio_path(self) -> Path:
        return self.directory / "audio.wav"

    def update(self, snapshot: Snapshot) -> None:
        """Record a snapshot, flushing at most every ``min_interval`` seconds."""
        self._snapshot = snapshot
        self._dirty = True
        if time.monotonic() - self._last_write >= self.min_interval:
            self.flush()

    def flush(self) -> None:
        if self._snapshot is None or not self._dirty:
            return
        lines = self._snapshot.speech_lines
        writers = {
            "md": (self.markdown_path, self._render_markdown),
            "srt": (self.srt_path, self._render_srt),
            "json": (self.json_path, self._render_json),
        }
        try:
            for name in self.formats:
                path, render = writers[name]
                self._atomic_write(path, render(lines))
        except OSError as exc:
            logger.error("Could not write transcript to %s: %s", self.directory, exc)
            return
        self._last_write = time.monotonic()
        self._dirty = False

    def _atomic_write(self, path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def _render_markdown(self, lines: list[Line]) -> str:
        out = [
            f"# {self.title}",
            "",
            f"- 开始时间: {self.started_at:%Y-%m-%d %H:%M:%S}",
            f"- 更新时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 段落数: {len(lines)}",
            "",
            "---",
            "",
        ]
        for line in lines:
            stamp = f"{line.start} - {line.end}" if line.start else ""
            out.append(f"**[{stamp}]**" if stamp else "**[--]**")
            out.append("")
            out.append(line.text.strip())
            if line.translation:
                out.append("")
                out.append(f"> {line.translation}")
            out.append("")
        return "\n".join(out)

    def _render_srt(self, lines: list[Line]) -> str:
        blocks = []
        for index, line in enumerate(lines, start=1):
            text = line.text.strip()
            if line.translation:
                text = f"{text}\n{line.translation}"
            blocks.append(
                f"{index}\n"
                f"{_srt_timestamp(line.start)} --> {_srt_timestamp(line.end)}\n"
                f"{text}\n"
            )
        return "\n".join(blocks)

    def _render_json(self, lines: list[Line]) -> str:
        payload = {
            "title": self.title,
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now().isoformat(),
            "segments": [
                {
                    "speaker": line.speaker,
                    "start": line.start,
                    "end": line.end,
                    "text": line.text.strip(),
                    "translation": line.translation,
                }
                for line in lines
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def close(self) -> str:
        """Final flush; returns a short human-readable summary."""
        self._dirty = True
        self.flush()
        count = len(self._snapshot.speech_lines) if self._snapshot else 0
        return f"{count} 段转录已保存到 {self.directory}"
