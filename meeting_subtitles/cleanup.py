"""Strip Whisper's non-speech annotations out of the transcript.

Whisper was trained on subtitle data, so it happily emits the bracketed cues
that captioners write -- "(cough)", "[Music]", "*clears throat*" -- and on a
noisy meeting mic it emits a lot of them. They are not speech, they carry no
meaning for the reader, and each one becomes a subtitle line and a sentence
sent for re-translation.

The filter deliberately keys on the *bracket*, not the word: a parenthesised
"cough" is a captioner's cue, while the word cough inside a sentence is
somebody talking about coughing. The only unbracketed case removed is a
segment that consists of nothing but such a word.
"""

import re

#: Words that mark a captioner's non-speech cue rather than speech.
NON_SPEECH_WORDS = (
    "cough", "coughs", "coughing", "clears throat", "clearing throat", "throat",
    "laugh", "laughs", "laughing", "laughter", "chuckle", "chuckles",
    "sigh", "sighs", "sighing", "breath", "breathes", "breathing", "inhales",
    "exhales", "sniff", "sniffs", "sneeze", "sneezes", "yawn", "yawns",
    "music", "musical", "applause", "clapping", "beep", "beeping", "click",
    "clicks", "typing", "keyboard", "footsteps", "door", "phone", "ringing",
    "static", "noise", "background noise", "silence", "pause",
    "inaudible", "unintelligible", "indistinct", "crosstalk", "overlapping",
    "blank_audio", "blank audio", "no speech", "speaking in foreign language",
    "foreign", "whispers", "whispering", "mumbles", "mumbling", "grunts",
)

_WORDS_RE = "|".join(sorted((re.escape(w) for w in NON_SPEECH_WORDS), key=len, reverse=True))

#: A cue is the word (optionally pluralised/qualified) inside brackets.
_BRACKETED = re.compile(
    rf"[\(\[\*\{{]\s*(?:{_WORDS_RE})[a-z]*\s*[\)\]\*\}}]",
    re.IGNORECASE,
)

#: Whole segment is nothing but a cue word and punctuation.
_BARE = re.compile(rf"^\W*(?:{_WORDS_RE})[a-z]*\W*$", re.IGNORECASE)

#: Musical-note runs Whisper uses for background music.
_NOTES = re.compile(r"[♪♫♬�]+")

_SPACES = re.compile(r"\s{2,}")
#: Removing a cue that sat between commas leaves ", ," and " ,".
_DOUBLED_PUNCT = re.compile(r"([,;:])\s*(?=[,.;:!?])")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_LEADING_PUNCT = re.compile(r"^\s*[,.;:]+\s*")


def strip_non_speech(text: str | None) -> str:
    """Remove caption cues; return "" when nothing but cues remain."""
    if not text:
        return ""
    cleaned = _NOTES.sub(" ", text)
    cleaned = _BRACKETED.sub(" ", cleaned)
    cleaned = _SPACES.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLED_PUNCT.sub("", cleaned)
    cleaned = _LEADING_PUNCT.sub("", cleaned)
    cleaned = _SPACES.sub(" ", cleaned).strip()
    if not cleaned or _BARE.match(cleaned):
        return ""
    return cleaned
