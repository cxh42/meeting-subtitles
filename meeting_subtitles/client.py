"""WebSocket client that streams captured PCM into a WhisperLiveKit server.

The server must run with ``--pcm-input`` so it accepts the raw 16 kHz mono
s16le frames produced by :mod:`meeting.audio` instead of a container format.
Every server update is a full ``FrontData`` snapshot (see
``whisperlivekit/timed_objects.py``), so consumers can simply replace their
previous state rather than merging deltas.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

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


class TranscriptionClient:
    """Pump PCM into ``/asr`` and hand every snapshot to a callback."""

    def __init__(
        self,
        server: str = "ws://127.0.0.1:8000",
        language: str | None = None,
        target_language: str | None = None,
        context: str | None = None,
        token: str | None = None,
    ) -> None:
        params = {
            key: value
            for key, value in (
                ("language", language),
                ("target_language", target_language),
                ("context", context),
                ("token", token),
            )
            if value
        }
        query = f"?{urlencode(params)}" if params else ""
        self.url = f"{server.rstrip('/')}/asr{query}"
        self.websocket: Any | None = None
        self._ready_to_stop = asyncio.Event()

    async def run(
        self,
        pcm_source,
        on_snapshot: Callable[[Snapshot], None],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """Stream ``pcm_source`` (async iterator of bytes) until it is exhausted.

        Sends the empty frame the server treats as end-of-audio, then waits for
        ``ready_to_stop`` so the final words and their translations are not lost.
        """
        logger.info("Connecting to %s", self.url)
        async with websockets.connect(self.url, max_size=None) as websocket:
            self.websocket = websocket
            if on_status:
                on_status("connected")
            receiver = asyncio.create_task(self._receive(websocket, on_snapshot, on_status))
            try:
                async for chunk in pcm_source:
                    await websocket.send(chunk)
                logger.info("Audio source exhausted; signalling end of stream.")
                await websocket.send(b"")
                try:
                    await asyncio.wait_for(self._ready_to_stop.wait(), timeout=30)
                except TimeoutError:
                    logger.warning("Server did not acknowledge end of stream within 30s.")
            finally:
                receiver.cancel()
                try:
                    await receiver
                except asyncio.CancelledError:
                    pass
                self.websocket = None

    async def _receive(
        self,
        websocket,
        on_snapshot: Callable[[Snapshot], None],
        on_status: Callable[[str], None] | None,
    ) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed server message.")
                continue

            kind = message.get("type")
            if kind == "config":
                logger.info("Server config: %s", message)
                if not message.get("useAudioWorklet"):
                    logger.error(
                        "Server is NOT running with --pcm-input; raw PCM will be "
                        "misread as a container format and nothing will transcribe."
                    )
                continue
            if kind == "ready_to_stop":
                self._ready_to_stop.set()
                if on_status:
                    on_status("finished")
                continue
            if kind == "error" or message.get("error"):
                logger.error("Server error: %s", message.get("error"))
                if on_status:
                    on_status(f"error: {message.get('error')}")
                continue

            on_snapshot(Snapshot.from_message(message))
