"""Notice when audio is going in and nothing is coming back.

The server catches a backend exception per chunk and keeps the session alive,
so a CUDA out-of-memory inside the ASR -- what happens when something else on
the card takes the memory the engine was counting on -- produces a meeting that
looks perfectly healthy. The WebSocket stays open, snapshots keep arriving, the
transcript file stays readable and openable. It simply never gains another
word, and half a meeting can go by before anyone notices.

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
#: is called a stall. Normal operation revises the snapshot every second or
#: two, so a minute of continuous speech producing nothing is not a slow
#: model -- it is a broken one.
STALL_SECONDS = 60.0


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
