"""The transcript data model.

Kept apart from :mod:`meeting_subtitles.client` so that reading and reshaping
a transcript does not drag in the WebSocket library. Segmentation, refinement,
recording and the overlay all work on these types and none of them talk to the
network.

A server update is a full snapshot (WhisperLiveKit's ``FrontData``), not a
delta, so consumers replace their previous state rather than merging.
"""

from dataclasses import dataclass, field
from typing import Any

SILENCE_SPEAKER = -2


@dataclass
class Line:
    """One transcript segment as rendered by the server."""

    speaker: int
    text: str
    start: str
    end: str
    translation: str = ""
    #: True once the whole sentence has been re-translated (see meeting.refine).
    refined: bool = False

    @property
    def is_silence(self) -> bool:
        return self.speaker == SILENCE_SPEAKER

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Line":
        translation = payload.get("translation") or ""
        if not isinstance(translation, str):
            translation = str(translation)
        return cls(
            speaker=int(payload.get("speaker", 1)),
            text=payload.get("text") or "",
            start=payload.get("start") or "",
            end=payload.get("end") or "",
            translation=translation.strip(),
        )


@dataclass
class Snapshot:
    """Latest full transcript state received from the server."""

    lines: list[Line] = field(default_factory=list)
    buffer_transcription: str = ""
    buffer_translation: str = ""
    status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def speech_lines(self) -> list[Line]:
        return [line for line in self.lines if not line.is_silence and line.text.strip()]

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "Snapshot":
        return cls(
            lines=[Line.from_dict(item) for item in message.get("lines", [])],
            buffer_transcription=message.get("buffer_transcription") or "",
            buffer_translation=message.get("buffer_translation") or "",
            status=message.get("status") or "",
            raw=message,
        )
