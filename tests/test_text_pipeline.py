"""Tests for the text handling between the recogniser and the screen.

These are the parts with no GPU, no audio and no network in them, which makes
them the parts CI can actually run. Each test below corresponds to a mistake
that was made at some point and is easy to make again:

* splitting on every period turns "v1.5" into two sentences;
* stripping non-speech by *word* deletes the word "cough" from a sentence
  about coughing, when only "(cough)" should go;
* short interjections each get their own subtitle line and the bar flickers;
* a glossary that grows past the recogniser's context limit is truncated
  mid-term, and a half term is worse than no term.
"""

import array

import pytest

from meeting_subtitles import domain, watchdog
from meeting_subtitles.cleanup import strip_non_speech
from meeting_subtitles.segment import (
    MAX_SENTENCE_CHARS,
    format_time,
    parse_time,
    split_sentences,
)

# ------------------------------------------------------------------ segment

@pytest.mark.parametrize("text, expected", [
    ("Hello there. How are you?", 2),
    ("One sentence with no terminator", 1),
    ("", 0),
])
def test_split_sentences_counts(text, expected):
    assert len(split_sentences(text)) == expected


def test_split_sentences_keeps_terminators():
    assert split_sentences("The first sentence. The second one!") == [
        "The first sentence.", "The second one!"]


def test_short_fragments_are_not_split_off():
    """A two-word fragment on its own line reads worse than a run-on.

    ``MIN_SENTENCE_CHARS`` is why: recognised speech is full of short
    interjections ("Right. Yeah.") and giving each its own subtitle line makes
    the bar flicker without making anything clearer.
    """
    assert split_sentences("Right. Yeah.") == ["Right. Yeah."]


def test_abbreviations_do_not_split():
    """A period is not a sentence end just because it is a period."""
    for text in ("We used PyTorch v2.5 for this.",
                 "It costs about 1.5 GB of memory.",
                 "Compare e.g. the baseline."):
        assert len(split_sentences(text)) == 1, text


def test_very_long_run_is_broken_up():
    """Speech with no punctuation must not become one unreadable block."""
    text = " ".join(["and then we tried another configuration"] * 12)
    pieces = split_sentences(text)
    assert len(pieces) > 1
    assert all(len(piece) <= MAX_SENTENCE_CHARS * 1.5 for piece in pieces)


def test_time_round_trip():
    for seconds in (0.0, 1.5, 61.25, 3661.0):
        assert parse_time(format_time(seconds)) == pytest.approx(seconds, abs=0.01)


# ------------------------------------------------------------------ cleanup

def test_bracketed_non_speech_is_removed():
    assert strip_non_speech("(cough) So as I was saying") == "So as I was saying"
    assert strip_non_speech("[BLANK_AUDIO]") == ""


def test_cue_words_survive_outside_brackets():
    """Keying on brackets, not on words: the topic may *be* coughing."""
    text = "The model detects a cough in the audio"
    assert strip_non_speech(text) == text


def test_strip_handles_none():
    assert strip_non_speech(None) == ""


# ------------------------------------------------------------------- domain

def test_default_domain_has_terms():
    assert len(domain.get(domain.DEFAULT_DOMAIN).terms) > 0


def test_user_terms_come_first():
    """The recogniser weights early context most, and names matter most."""
    context = domain.build_context("cs-ai", "Anirudh, Project Helios")
    assert context.index("Anirudh") < context.index("LoRA")


def test_context_stays_within_the_limit():
    context = domain.build_context("cs-ai", "x" * 2000)
    assert len(context) <= domain.MAX_CONTEXT_CHARS


def test_context_is_a_finished_sentence():
    """Whisper continues the prompt's style, so the prompt must read as prose.

    A bare comma-separated list makes it emit comma-separated fragments and,
    measured on real audio, hallucinate more list before the speech starts.
    """
    context = domain.build_context("cs-ai", "")
    assert context.endswith(".")
    assert context[0].isupper()


def test_empty_domain_gives_no_prompt():
    """No terms must mean no prompt, not a sentence promising terms."""
    assert domain.build_context("general", "") == ""


def test_context_truncates_on_a_term_boundary():
    """Half a term is worse than no term: it conditions on a non-word."""
    context = domain.build_context("cs-ai", ", ".join(f"term{i}" for i in range(400)))
    assert not context.rstrip(".").endswith(",")
    assert context.endswith(".")


# ----------------------------------------------------------------- watchdog

def _pcm(amplitude: int, seconds: float) -> bytes:
    """`seconds` of 16 kHz mono s16le at a constant absolute amplitude."""
    samples = array.array("h", [amplitude, -amplitude] * int(8000 * seconds))
    return samples.tobytes()


def test_silence_never_stalls():
    """A meeting is mostly pauses; a pause must not read as a broken engine."""
    dog = watchdog.StallWatchdog(stall_seconds=10)
    dog.note_text("nothing said yet")
    for _ in range(600):
        dog.note_audio(_pcm(5, 0.1))
    assert not dog.stalled


def test_speech_with_no_new_text_stalls():
    dog = watchdog.StallWatchdog(stall_seconds=10)
    dog.note_text("the last thing the server said")
    for _ in range(120):
        dog.note_audio(_pcm(3000, 0.1))
    assert dog.stalled


def test_new_text_clears_the_count():
    dog = watchdog.StallWatchdog(stall_seconds=10)
    dog.note_text("first")
    for _ in range(90):
        dog.note_audio(_pcm(3000, 0.1))
    dog.note_text("second")
    assert not dog.stalled
    assert dog.speech_seconds == 0


def test_an_unchanged_snapshot_does_not_clear_the_count():
    """The server keeps resending the same state; only *new* text counts."""
    dog = watchdog.StallWatchdog(stall_seconds=10)
    for _ in range(120):
        dog.note_text("same state every time")
        dog.note_audio(_pcm(3000, 0.1))
    assert dog.stalled


def test_the_warning_is_raised_once_per_stall():
    """The check runs per 100 ms chunk; the user must be told once."""
    dog = watchdog.StallWatchdog(stall_seconds=1)
    dog.note_text("start")
    reports = 0
    for _ in range(60):
        dog.note_audio(_pcm(3000, 0.1))
        reports += dog.take_report()
    assert reports == 1
    dog.note_text("recovered")
    for _ in range(60):
        dog.note_audio(_pcm(3000, 0.1))
        reports += dog.take_report()
    assert reports == 2


def test_speech_level_tolerates_an_odd_trailing_byte():
    """A short read from ffmpeg can split a sample; frombytes would raise."""
    assert watchdog.speech_level(_pcm(3000, 0.1) + b"\x01") > 0
    assert watchdog.speech_level(b"") == 0.0
    assert watchdog.speech_level(b"\x01") == 0.0
