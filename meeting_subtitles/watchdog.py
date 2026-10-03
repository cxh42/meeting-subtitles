"""Notice when audio is going in and nothing is coming back.

The server catches a backend exception per chunk and keeps the session alive,
so a CUDA out-of-memory inside the ASR -- what happens when something else on
the card takes the memory the engine was counting on -- produces a meeting that
looks perfectly healthy. The WebSocket stays open, snapshots keep arriving, the
transcript file stays readable and openable. It simply never gains another
word, and half a meeting can go by before anyone notices.

The engine now reports those exceptions itself (``serve.py``), and
:class:`BackendWatch` turns its counters into a warning within seconds. The
stall watchdog stays as the fallback for an engine that fails without raising,
or one too old to report.

Elapsed time cannot be the trigger: a meeting is mostly pauses, and a pause is
indistinguishable from a dead backend at this layer -- both are an unchanged
snapshot. What separates them is the audio, so only the seconds that actually
carried speech count towards the deadline.

Nothing here reads a clock: the length of a PCM chunk *is* its duration, which
keeps the whole thing deterministic and testable without a soundcard.
"""

import array

#: 16 kHz mono s16le, matching :mod:`meeting_subtitles.audio`.
BYTES_PER_SECOND = 16000 * 2

#: Mean absolute sample value above which a chunk is treated as carrying
#: speech. Room tone through a sink monitor measures in the low tens and
#: ordinary meeting speech in the high hundreds, so this sits well clear of
#: both -- it is deliberately unselective, because a false "quiet" only delays
#: the warning while a false "loud" would raise one during a long pause.
SPEECH_LEVEL = 150.0

#: Seconds of speech that may pass with the transcript unchanged before this
#: is called a stall. A working engine revises the snapshot several times a
#: second while someone talks. Sixty used to be the figure, and in the meeting
#: that prompted :class:`BackendWatch` the user gave up after 53 seconds of
#: speech into a dead engine without ever seeing the warning.
STALL_SECONDS = 30.0

#: Seconds of speech on the microphone, with the computer playing nothing,
#: before :class:`SystemAudioWatch` says so.
UNHEARD_SECONDS = 45.0


def speech_level(chunk: bytes) -> float:
    """Mean absolute amplitude of a 16-bit PCM chunk, 0.0 when it is empty.

    Every eighth sample is enough for a level estimate and keeps the cost off
    the event loop that is also pumping the WebSocket.
    """
    samples = array.array("h")
    # An odd trailing byte cannot be part of a sample; frombytes would raise.
    samples.frombytes(chunk[:len(chunk) - len(chunk) % 2])
    if not samples:
        return 0.0
    sparse = samples[::8]
    return sum(abs(value) for value in sparse) / len(sparse)


def transcript_signature(snapshot) -> str:
    """Everything the recogniser can still revise, as one comparable string.

    The partial sentence belongs in it: a working engine keeps rewriting it
    before it commits a line, so it is the earliest sign that recognition is
    alive. Silence lines do not: the server appends them on its own clock --
    a long enough pause adds one even while the ASR is failing every chunk --
    and each one used to reset the count. Nor does the translation buffer,
    which trails recognition and can keep moving after it has stopped.
    """
    parts = [line.text for line in snapshot.speech_lines]
    parts.append(snapshot.buffer_transcription)
    return "\x1f".join(parts)


class StallWatchdog:
    """Tracks speech going in against transcript coming out."""

    def __init__(
        self,
        stall_seconds: float = STALL_SECONDS,
        speech_level_threshold: float = SPEECH_LEVEL,
    ) -> None:
        self.stall_seconds = stall_seconds
        self.speech_level_threshold = speech_level_threshold
        self.speech_seconds = 0.0
        self._signature: str | None = None
        #: Set once per stall so the caller can warn on the edge, not every
        #: chunk, and cleared as soon as text starts moving again.
        self.reported = False

    def note_audio(self, chunk: bytes) -> None:
        if speech_level(chunk) < self.speech_level_threshold:
            return
        self.speech_seconds += len(chunk) / BYTES_PER_SECOND

    def note_text(self, signature: str) -> None:
        """Feed the current transcript state; anything new clears the count."""
        if signature == self._signature:
            return
        self._signature = signature
        self.speech_seconds = 0.0
        self.reported = False

    @property
    def stalled(self) -> bool:
        return self.speech_seconds >= self.stall_seconds

    def take_report(self) -> bool:
        """True once per stall, for the caller that shows the warning."""
        if self.stalled and not self.reported:
            self.reported = True
            return True
        return False


class BackendWatch:
    """Turns the engine's running failure counters into edges.

    Fed one reading per poll. The first reading is only a baseline: the
    counters live as long as the engine, so they include failures from
    earlier sessions. A failure streak is several new failures between two
    polls -- a failing backend raises on every chunk, many times a second,
    while one stray exception (CTranslate2 reports one stale error on the
    first call after an earlier out-of-memory) must not raise an alarm.
    Recovery is two quiet polls in a row.
    """

    def __init__(self, burst: int = 3, quiet_polls: int = 2) -> None:
        self.burst = burst
        self.quiet_polls = quiet_polls
        self.failing = False
        self.out_of_memory = False
        self.last_error = ""
        self._failures: int | None = None
        self._out_of_memory: int | None = None
        self._quiet = 0

    def update(self, failures: int, out_of_memory: int, last_error: str = "") -> str | None:
        """Return "failing" or "recovered" on an edge, otherwise None."""
        if self._failures is None or failures < self._failures:
            # First reading, or an engine that restarted and began counting anew.
            self._failures, self._out_of_memory = failures, out_of_memory
            return None
        new = failures - self._failures
        new_oom = out_of_memory - (self._out_of_memory or 0)
        self._failures, self._out_of_memory = failures, out_of_memory
        if new >= self.burst:
            self._quiet = 0
            self.last_error = last_error
            self.out_of_memory = self.out_of_memory or new_oom > 0
            if not self.failing:
                self.failing = True
                return "failing"
            return None
        if self.failing and new == 0:
            self._quiet += 1
            if self._quiet >= self.quiet_polls:
                self.failing = False
                self.out_of_memory = False
                self._quiet = 0
                return "recovered"
        return None


class SystemAudioWatch:
    """Notices a meeting the microphone hears but the computer does not play.

    The system-audio branch is the only digital copy of the other
    participants. When it carries nothing while the microphone carries
    speech, the call is being played somewhere this machine cannot record: on
    another device in the room, by a Zoom that never joined computer audio or
    has its speaker muted, or so quietly that nothing reaches the engine. The
    subtitles then hear the others only as room sound leaking into the
    microphone, or not at all, and in the mix that looks like a meeting where
    only the user talks.

    It is a check rather than a fault -- remote participants who stay silent
    look the same -- so it asks for a long stretch of microphone speech, and
    one second of real sound from the computer clears it.
    """

    def __init__(
        self,
        unheard_seconds: float = UNHEARD_SECONDS,
        speech_level_threshold: float = SPEECH_LEVEL,
        heard_seconds: float = 1.0,
    ) -> None:
        self.unheard_seconds = unheard_seconds
        self.speech_level_threshold = speech_level_threshold
        self.heard_seconds = heard_seconds
        #: Microphone speech since the computer last played something.
        self.mic_seconds = 0.0
        self._system_seconds = 0.0
        self.reported = False
        #: Totals for the end-of-meeting summary.
        self.total_system_seconds = 0.0
        self.total_mic_seconds = 0.0

    def note(self, system: bytes, mic: bytes) -> str | None:
        """Feed one chunk of each branch; "unheard"/"heard" on an edge."""
        seconds = len(system) / BYTES_PER_SECOND
        if speech_level(system) >= self.speech_level_threshold:
            self.total_system_seconds += seconds
            self._system_seconds += seconds
            if self._system_seconds >= self.heard_seconds:
                self._system_seconds = 0.0
                self.mic_seconds = 0.0
                if self.reported:
                    self.reported = False
                    return "heard"
        if speech_level(mic) >= self.speech_level_threshold:
            self.total_mic_seconds += len(mic) / BYTES_PER_SECOND
            self.mic_seconds += len(mic) / BYTES_PER_SECOND
            if self.mic_seconds >= self.unheard_seconds and not self.reported:
                self.reported = True
                return "unheard"
        return None
