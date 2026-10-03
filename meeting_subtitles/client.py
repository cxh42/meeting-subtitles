"""WebSocket client that streams captured PCM into a WhisperLiveKit server.

The server must run with ``--pcm-input`` so it accepts the raw 16 kHz mono
s16le frames produced by :mod:`meeting_subtitles.audio` instead of a container
format. The transcript types it returns live in
:mod:`meeting_subtitles.model` and are re-exported here for convenience.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, build_opener

import websockets

from meeting_subtitles.model import SILENCE_SPEAKER, Line, Snapshot
from meeting_subtitles.serve import BACKEND_STATUS_PATH

logger = logging.getLogger(__name__)

__all__ = ["SILENCE_SPEAKER", "EngineTooOld", "Line", "Snapshot", "TranscriptionClient",
           "engine_status_url", "fetch_engine_status"]


class EngineTooOld(Exception):
    """The engine predates the failure report: it has no status route."""


def engine_status_url(server: str) -> str:
    """The status route on the same server as the ``ws://`` address."""
    parsed = urlparse(server)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}{BACKEND_STATUS_PATH}"


def fetch_engine_status(url: str) -> dict | None:
    """The engine's backend failure counters, or None when unreachable.

    Bypasses any configured proxy, as doctor's health check does: urllib would
    otherwise send 127.0.0.1 through the SOCKS proxy the desktop exports.
    """
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            status = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise EngineTooOld from exc
        return None
    except (URLError, OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


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
