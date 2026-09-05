"""Capture the meeting's audio through PulseAudio or PipeWire.

The transcript needs both sides of the call: the remote participants arrive on
the *monitor* of the output sink (everything the speakers play), and the local
speaker arrives on the microphone source. ffmpeg opens both, downmixes each to
16 kHz mono and mixes them into the single PCM stream the server expects
(``--pcm-input``: 16 kHz, mono, signed 16-bit little-endian).

Capturing the sink's monitor rather than a specific application means the
source of the audio does not matter: Zoom, Teams, a browser tab, a local video
player -- anything the machine is playing is transcribed, with no plugin or
permission needed from the program producing it.

PipeWire needs no special handling here. It ships a PulseAudio-compatible
server (``pipewire-pulse``) that every Ubuntu since 22.10 enables by default,
so ``pactl`` and ffmpeg's ``pulse`` input work unchanged on both.
"""

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE

#: Size of one chunk handed to the WebSocket client, in milliseconds.
CHUNK_MS = 100

#: Shown in ``--list-devices`` and in ``--monitor-source``'s help text.
BACKEND = "PulseAudio/PipeWire"
SOURCE_HINT = "PulseAudio 源名"


@dataclass
class Source:
    """A capture source as reported by ``pactl list short sources``.

    ``is_monitor`` marks the sources that carry what the speakers are playing,
    which is where the other meeting participants come from.
    """

    name: str
    description: str
    is_monitor: bool

    def __str__(self) -> str:
        kind = "monitor" if self.is_monitor else "input"
        return f"{self.name}  [{kind}]  {self.description}"


def _pactl(*args: str) -> str:
    """Run pactl and return stdout, or "" when PulseAudio is unreachable.

    The locale is forced to C: pactl translates its field labels, so a Chinese
    desktop prints "名称：" where the parser expects "Name:".
    """
    if shutil.which("pactl") is None:
        raise RuntimeError(
            "pactl not found. Install pulseaudio-utils: sudo apt install pulseaudio-utils"
        )
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        out = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5,
            check=True, env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("pactl %s failed: %s", " ".join(args), exc)
        return ""
    return out.stdout.strip()


def _descriptions() -> dict:
    """Map source name -> human readable description."""
    result = {}
    current = None
    for raw in _pactl("list", "sources").splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Description:") and current:
            result[current] = line.split(":", 1)[1].strip()
    return result


def list_sources() -> list[Source]:
    """Return every capture source, monitors included.

    Names come from the short form, which is not localised; descriptions are
    looked up in the long form and are optional.
    """
    descriptions = _descriptions()
    sources: list[Source] = []
    for raw in _pactl("list", "short", "sources").splitlines():
        fields = raw.split("\t")
        if len(fields) < 2:
            continue
        name = fields[1].strip()
        if not name:
            continue
        sources.append(
            Source(name, descriptions.get(name, name), name.endswith(".monitor"))
        )
    return sources


def default_monitor_source() -> str:
    """Monitor of the default sink -- i.e. everything you hear."""
    sink = _pactl("get-default-sink")
    if sink and sink != "@DEFAULT_SINK@":
        return f"{sink}.monitor"
    for source in list_sources():
        if source.is_monitor:
            return source.name
    raise RuntimeError("No monitor source found; is PulseAudio/PipeWire running?")


def default_mic_source() -> str:
    """Default microphone source, skipping monitors."""
    source = _pactl("get-default-source")
    if source and not source.endswith(".monitor"):
        return source
    for candidate in list_sources():
        if not candidate.is_monitor:
            return candidate.name
    raise RuntimeError("No microphone source found.")


def build_ffmpeg_command(
    monitor: str | None,
    mic: str | None,
    wav_path: Path | None = None,
    mic_gain: float = 1.0,
    monitor_gain: float = 1.0,
) -> list[str]:
    """Build the ffmpeg command that produces s16le PCM on stdout.

    With both inputs present they are mixed with ``normalize=0`` so neither side
    gets quieter when the other is silent -- ``amix``'s default normalisation
    halves every input's amplitude, which is exactly wrong for speech that
    alternates between the two. A limiter guards the resulting headroom.
    """
    if not monitor and not mic:
        raise ValueError("At least one of monitor/mic must be given.")

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
    ]

    inputs = []
    if monitor:
        # 20 ms fragments keep capture latency low instead of ffmpeg's default buffer.
        cmd += ["-f", "pulse", "-fragment_size", "1920", "-i", monitor]
        inputs.append(("monitor", monitor_gain))
    if mic:
        cmd += ["-f", "pulse", "-fragment_size", "1920", "-i", mic]
        inputs.append(("mic", mic_gain))

    chains = []
    labels = []
    for idx, (_name, gain) in enumerate(inputs):
        label = f"a{idx}"
        chains.append(
            f"[{idx}:a]aresample=async=1:first_pts=0,aformat=sample_fmts=fltp:"
            f"sample_rates={SAMPLE_RATE}:channel_layouts=mono,volume={gain}[{label}]"
        )
        labels.append(f"[{label}]")

    if len(inputs) == 1:
        mixed = f"{labels[0]}alimiter=limit=0.95"
    else:
        mixed = (
            f"{''.join(labels)}amix=inputs={len(inputs)}:duration=longest:"
            f"dropout_transition=0:normalize=0,alimiter=limit=0.95"
        )

    # A filter output label feeds exactly one -map, so recording to disk needs
    # its own branch rather than mapping [out] twice.
    if wav_path is not None:
        chains.append(f"{mixed},asplit=2[out][wav]")
    else:
        chains.append(f"{mixed}[out]")

    cmd += ["-filter_complex", ";".join(chains)]
    cmd += ["-map", "[out]", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "pipe:1"]

    if wav_path is not None:
        cmd += ["-map", "[wav]", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", "-y", str(wav_path)]

    return cmd


class AudioCapture:
    """Async iterator over captured PCM chunks."""

    def __init__(
        self,
        monitor: str | None,
        mic: str | None,
        wav_path: Path | None = None,
        chunk_ms: int = CHUNK_MS,
        mic_gain: float = 1.0,
        monitor_gain: float = 1.0,
    ) -> None:
        self.command = build_ffmpeg_command(monitor, mic, wav_path, mic_gain, monitor_gain)
        self.chunk_bytes = BYTES_PER_SECOND * chunk_ms // 1000
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closing = False

    async def __aenter__(self) -> "AudioCapture":
        logger.info("ffmpeg: %s", " ".join(self.command))
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        async for line in self.process.stderr:
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            if self._closing:
                # Tearing down mid-write always produces "Immediate exit
                # requested" muxer errors; they say nothing about the recording.
                logger.debug("ffmpeg (shutdown): %s", text)
            else:
                logger.warning("ffmpeg: %s", text)

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield fixed-size PCM chunks until ffmpeg stops."""
        assert self.process is not None and self.process.stdout is not None
        while True:
            data = await self.process.stdout.read(self.chunk_bytes)
            if not data:
                break
            yield data

    async def close(self) -> None:
        if self.process is None:
            return
        self._closing = True
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        self.process = None
