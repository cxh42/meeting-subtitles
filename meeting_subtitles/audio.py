"""Capture the meeting's audio through PulseAudio or PipeWire.

The transcript needs both sides of the call: the remote participants arrive on
the *monitor* of the output sink (everything the speakers play), and the local
speaker arrives on the microphone source. ffmpeg opens both, downmixes each to
16 kHz mono and mixes them into the single PCM stream the server expects
(``--pcm-input``: 16 kHz, mono, signed 16-bit little-endian).

Capturing a sink's monitor rather than a specific application means the source
of the audio does not matter: Zoom, Teams, a browser tab, a local video player
-- anything that sink plays is transcribed, with no plugin or permission needed
from the program producing it. Zoom cannot hide its playback from a monitor;
the only way to lose the other side is to monitor the wrong sink, which is why
the sink is chosen by :func:`meeting_sink` and kept current by
:class:`MeetingAudioRouter` rather than read once from the default.

PipeWire needs no special handling here. It ships a PulseAudio-compatible
server (``pipewire-pulse``) that every Ubuntu since 22.10 enables by default,
so ``pactl`` and ffmpeg's ``pulse`` input work unchanged on both.
"""

import array
import asyncio
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
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

#: Application names our two ffmpeg inputs give the sound server. The router
#: finds its own capture stream by the first one, and both keep WirePlumber's
#: per-application restore state for our streams apart from every other ffmpeg
#: on the machine, which all share the key "Lavf<version>".
SYSTEM_STREAM_NAME = "meeting-subtitles-system"
MIC_STREAM_NAME = "meeting-subtitles-mic"

#: The virtual sinks Zoom loads for "share computer sound" (共享电脑声音). While
#: sharing, the combine sink becomes the system default and other programs are
#: moved onto it, but Zoom's own playback -- the other participants -- stays on
#: the real device, so the combine sink's monitor has everything except the
#: meeting.
ZOOM_SHARE_SINKS = ("zoomcombine", "zoomrecord")
ZOOM_SHARE_DESCRIPTIONS = ("zoom_combine_device", "zoom_recording")


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


@dataclass
class Sink:
    """An output device, as ``pactl list sinks`` describes it."""

    index: int
    name: str
    description: str
    monitor: str

    @property
    def is_zoom_share(self) -> bool:
        return (self.name in ZOOM_SHARE_SINKS
                or self.description in ZOOM_SHARE_DESCRIPTIONS)


@dataclass
class PlaybackStream:
    """One program's playback stream (a PulseAudio sink input)."""

    index: int
    sink: int
    application: str
    binary: str
    corked: bool
    muted: bool
    #: Percent, averaged over channels. This is the stream's own volume, which
    #: -- unlike the sink's -- is applied before the monitor and so scales
    #: what we record.
    volume: int

    @property
    def is_zoom(self) -> bool:
        return self.binary == "zoom" or self.application.startswith("ZOOM VoiceEngine")


@dataclass
class CaptureStream:
    """One program's recording stream (a PulseAudio source output)."""

    index: int
    source: int
    pid: int | None
    application: str


def _run_pactl(*args: str) -> subprocess.CompletedProcess | None:
    """Run pactl, or return None when it fails or PulseAudio is unreachable.

    The locale is forced to C: pactl translates its field labels, so a Chinese
    desktop prints "名称：" where the parser expects "Name:".
    """
    if shutil.which("pactl") is None:
        raise RuntimeError(
            "pactl not found. Install pulseaudio-utils: sudo apt install pulseaudio-utils"
        )
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5,
            check=True, env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("pactl %s failed: %s", " ".join(args), exc)
        return None


def _pactl(*args: str) -> str:
    """Run pactl and return stdout, or "" when it fails."""
    result = _run_pactl(*args)
    return result.stdout.strip() if result is not None else ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _parse_blocks(text: str, kind: str) -> list[dict]:
    """Split ``pactl list`` output into its numbered blocks.

    Each block becomes ``{"index", "fields", "props"}``: the one-tab
    ``Key: value`` lines, and the ``key = "value"`` lines under
    ``Properties:``. Other two-tab sections (ports, formats) are skipped.
    Only the labels need the C locale; descriptions stay localised, which is
    what the user should see anyway.

    Deliberately not ``pactl -f json``: pactl 17 prints ``(null)`` for every
    string containing non-ASCII, so device names on a Chinese desktop vanish.
    """
    blocks: list[dict] = []
    current: dict | None = None
    in_props = False
    header = f"{kind} #"
    for raw in text.splitlines():
        if raw.startswith(header):
            try:
                index = int(raw[len(header):].strip())
            except ValueError:
                current = None
                continue
            current = {"index": index, "fields": {}, "props": {}}
            blocks.append(current)
            in_props = False
            continue
        line = raw.strip()
        if current is None or not line:
            continue
        if raw.startswith("\t\t"):
            if in_props:
                key, sep, value = line.partition(" = ")
                if sep:
                    current["props"][key] = _unquote(value)
            continue
        in_props = line == "Properties:"
        key, sep, value = line.partition(":")
        if sep and not in_props:
            current["fields"][key.strip()] = value.strip()
    return blocks


def _int(value: str | None) -> int | None:
    try:
        return int(_unquote(value or ""))
    except ValueError:
        return None


def parse_sinks(text: str) -> list[Sink]:
    sinks = []
    for block in _parse_blocks(text, "Sink"):
        fields = block["fields"]
        name = fields.get("Name", "")
        if not name:
            continue
        sinks.append(Sink(
            index=block["index"],
            name=name,
            description=fields.get("Description", "") or name,
            monitor=fields.get("Monitor Source", "") or f"{name}.monitor",
        ))
    return sinks


def parse_playback_streams(text: str) -> list[PlaybackStream]:
    streams = []
    for block in _parse_blocks(text, "Sink Input"):
        fields, props = block["fields"], block["props"]
        sink = _int(fields.get("Sink"))
        if sink is None:
            continue
        percents = [int(p) for p in re.findall(r"(\d+)%", fields.get("Volume", ""))]
        streams.append(PlaybackStream(
            index=block["index"],
            sink=sink,
            application=props.get("application.name", ""),
            binary=props.get("application.process.binary", ""),
            corked=fields.get("Corked") == "yes",
            muted=fields.get("Mute") == "yes",
            volume=round(sum(percents) / len(percents)) if percents else 100,
        ))
    return streams


def parse_capture_streams(text: str) -> list[CaptureStream]:
    streams = []
    for block in _parse_blocks(text, "Source Output"):
        source = _int(block["fields"].get("Source"))
        if source is None:
            continue
        streams.append(CaptureStream(
            index=block["index"],
            source=source,
            pid=_int(block["props"].get("application.process.id")),
            application=block["props"].get("application.name", ""),
        ))
    return streams


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


def list_sinks() -> list[Sink]:
    return parse_sinks(_pactl("list", "sinks"))


def default_sink_name() -> str:
    return _pactl("get-default-sink")


def playback_streams() -> list[PlaybackStream]:
    return parse_playback_streams(_pactl("list", "sink-inputs"))


def default_monitor_source() -> str:
    """Monitor of the default sink -- i.e. everything you hear."""
    sink = _pactl("get-default-sink")
    if sink and sink != "@DEFAULT_SINK@":
        return f"{sink}.monitor"
    for source in list_sources():
        if source.is_monitor:
            return source.name
    raise RuntimeError("No monitor source found; is PulseAudio/PipeWire running?")


def choose_meeting_sink(
    sinks: list[Sink], streams: list[PlaybackStream], default: str,
) -> tuple[Sink, str] | None:
    """The sink whose monitor carries the other side of the meeting, and why.

    Zoom's own playback decides whenever it exists. Zoom plays to the speaker
    chosen in *its* settings, which need not be the system default, and
    "share computer sound" moves the default onto Zoom's combine sink while
    Zoom's voices stay where they were. Without a Zoom stream the default is
    right, unless it is one of those share sinks.
    """
    by_index = {sink.index: sink for sink in sinks}
    zoom = [stream for stream in streams if stream.is_zoom and stream.sink in by_index]
    if zoom:
        # A corked stream still says where Zoom plays; a running one is surer.
        stream = min(zoom, key=lambda candidate: candidate.corked)
        return by_index[stream.sink], "Zoom 正在这里播放"
    chosen = next((sink for sink in sinks if sink.name == default), None)
    if chosen is not None and not chosen.is_zoom_share:
        return chosen, "系统默认输出"
    real = [sink for sink in sinks if not sink.is_zoom_share]
    if not real:
        return None
    if chosen is not None:
        return real[0], "默认输出是 Zoom「共享电脑声音」的虚拟设备，改录实际的输出设备"
    return real[0], "没有可用的默认输出，改录第一个输出设备"


def meeting_sink() -> tuple[Sink, str]:
    """:func:`choose_meeting_sink` applied to the live sound server."""
    choice = choose_meeting_sink(list_sinks(), playback_streams(), default_sink_name())
    if choice is None:
        raise RuntimeError("No output device found; is PulseAudio/PipeWire running?")
    return choice


def meeting_monitor_source() -> tuple[str, str]:
    """(monitor source name, reason) for the system-audio input."""
    sink, reason = meeting_sink()
    return sink.monitor, reason


def default_mic_source() -> str:
    """Default microphone source, skipping monitors."""
    source = _pactl("get-default-source")
    if source and not source.endswith(".monitor"):
        return source
    for candidate in list_sources():
        if not candidate.is_monitor:
            return candidate.name
    raise RuntimeError("No microphone source found.")


def output_channels(monitor: str | None, mic: str | None) -> int:
    """Channels ffmpeg writes: the mix alone, or the mix plus each input.

    With both inputs present the two branches travel next to the mix so each
    can be measured on its own. The mix is all the server needs, but only the
    branches can tell "the other side is quiet" from "the other side is not
    reaching us" -- in the mix, a hot microphone hides either.
    """
    return 3 if monitor and mic else 1


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
    alternates between the two. A limiter guards the resulting headroom, with
    its auto-level off: on by default, it scales the limited output back up to
    full scale, which undoes the headroom it was put there for.

    Channel 0 of the output is that mix; see :func:`output_channels` for the
    others. The wav gets the mix alone, which is what was transcribed.
    """
    if not monitor and not mic:
        raise ValueError("At least one of monitor/mic must be given.")

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
    ]

    inputs = []
    if monitor:
        # 20 ms fragments keep capture latency low instead of ffmpeg's default buffer.
        cmd += ["-f", "pulse", "-name", SYSTEM_STREAM_NAME, "-stream_name", "system-audio",
                "-fragment_size", "1920", "-i", monitor]
        inputs.append(("monitor", monitor_gain))
    if mic:
        cmd += ["-f", "pulse", "-name", MIC_STREAM_NAME, "-stream_name", "microphone",
                "-fragment_size", "1920", "-i", mic]
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

    limiter = "alimiter=limit=0.95:level=disabled"
    channels = output_channels(monitor, mic)
    if channels == 1:
        mixed = f"{labels[0]}{limiter}"
    else:
        # A filter output label feeds exactly one consumer, so each branch is
        # split before the mix takes its copy.
        for idx, label in enumerate(labels):
            chains.append(f"{label}asplit=2[m{idx}][b{idx}]")
        mixed = (
            f"[m0][m1]amix=inputs=2:duration=longest:"
            f"dropout_transition=0:normalize=0,{limiter}"
        )

    if wav_path is not None:
        chains.append(f"{mixed},asplit=2[mix][wav]")
    else:
        chains.append(f"{mixed}[mix]")
    if channels == 1:
        out = "[mix]"
    else:
        chains.append("[mix][b0][b1]join=inputs=3:channel_layout=3.0:"
                      "map=0.0-FL|1.0-FR|2.0-FC[out]")
        out = "[out]"

    cmd += ["-filter_complex", ";".join(chains)]
    cmd += ["-map", out]
    if channels == 1:
        cmd += ["-ac", "1"]
    # With three, no -ac: ffmpeg's default three-channel layout is 2.1, so
    # "-ac 3" would remix join's 3.0 -- folding the microphone into the other
    # two channels and leaving an empty LFE where it should be.
    cmd += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]

    if wav_path is not None:
        cmd += ["-map", "[wav]", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", "-y", str(wav_path)]

    return cmd


@dataclass
class Chunk:
    """One slice of captured audio: the mix, and each input when there are two."""

    pcm: bytes
    system: bytes | None = None
    mic: bytes | None = None


def split_channels(data: bytes, channels: int) -> Chunk:
    """Turn interleaved s16le into a :class:`Chunk`; ``data`` holds whole frames."""
    if channels == 1:
        return Chunk(data)
    samples = array.array("h")
    samples.frombytes(data)
    return Chunk(samples[0::channels].tobytes(), samples[1::channels].tobytes(),
                 samples[2::channels].tobytes())


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
        self.channels = output_channels(monitor, mic)
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

    async def chunks(self) -> AsyncIterator[Chunk]:
        """Yield chunks of whole frames until ffmpeg stops.

        A pipe read returns whatever is there, which can end mid-frame -- with
        three channels, mid-way between the mix and a branch -- so the
        remainder waits for the next read.
        """
        assert self.process is not None and self.process.stdout is not None
        frame = BYTES_PER_SAMPLE * self.channels
        pending = b""
        while True:
            data = await self.process.stdout.read(self.chunk_bytes * self.channels)
            if not data:
                break
            pending += data
            usable = len(pending) - len(pending) % frame
            if usable:
                yield split_channels(pending[:usable], self.channels)
                pending = pending[usable:]

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


def find_capture_stream(pid: int, application: str = SYSTEM_STREAM_NAME) -> CaptureStream | None:
    for stream in parse_capture_streams(_pactl("list", "source-outputs")):
        if stream.pid == pid and stream.application == application:
            return stream
    return None


def source_name(index: int) -> str | None:
    for raw in _pactl("list", "short", "sources").splitlines():
        fields = raw.split("\t")
        if len(fields) >= 2 and _int(fields[0]) == index:
            return fields[1].strip()
    return None


class MeetingAudioRouter:
    """Keep the system-audio capture on whatever sink the meeting plays to.

    Naming a monitor does not pin a capture to it. When the name matches the
    default sink, WirePlumber treats the stream as "follow the default"
    (``linking.follow-default-target``) and moves it with every default
    change -- including Zoom's share-sound switch to its combine sink, which
    takes the other participants out of the recording while the microphone
    carries on. A ``move-source-output`` onto the current default does not pin
    it either: that, too, reads as "follow the default". So the router does
    not try to pin; it looks where :func:`meeting_sink` says the meeting is
    and moves the capture there whenever the two differ. A move to anything
    but the default does stick. Measured with null sinks: after Zoom's stream
    returned to the real device while the default stayed on the combine
    sink, the router had the capture back within one poll, and without it
    the system audio went to digital silence for good.

    Polled rather than driven by ``pactl subscribe``: one look every couple of
    seconds is cheap, needs no event parsing, and cannot get stuck on a
    subscription that died.
    """

    def __init__(self, capture_pid: int,
                 on_change: Callable[[Sink, str], None] | None = None) -> None:
        self.capture_pid = capture_pid
        self.on_change = on_change
        self._warned = False

    async def run(self, interval: float = 2.0) -> None:
        while True:
            try:
                await asyncio.to_thread(self.step)
            except Exception as exc:  # routing is best effort; capture goes on
                if not self._warned:
                    self._warned = True
                    logger.warning("无法跟踪会议的输出设备: %s", exc)
            await asyncio.sleep(interval)

    def step(self) -> None:
        stream = find_capture_stream(self.capture_pid)
        if stream is None:
            return   # ffmpeg has not connected yet, or is going away
        current = source_name(stream.source)
        if current is None:
            return   # the source vanished between the two lookups
        sink, reason = meeting_sink()
        if current == sink.monitor:
            return
        if _run_pactl("move-source-output", str(stream.index), sink.monitor) is None:
            return
        logger.warning("系统声音改为录 %s（%s）", sink.monitor, reason)
        if self.on_change is not None:
            self.on_change(sink, reason)
