"""Split the server's pause-delimited lines into real sentences.

The server starts a new transcript line after a VAD pause. In a real meeting
people rarely pause for long, so a single line can run for a minute and read as
one endless sentence -- bad to read, and a bad unit to re-translate.

Whisper already punctuates its output, so the sentence boundaries are right
there in the text. Re-segmenting on punctuation gives short, readable units,
and it means a sentence is finalised (and re-translated) as soon as the speaker
finishes it rather than whenever they happen to pause.

Timestamps for the pieces are interpolated across the parent line by character
position. That is approximate, but the alternative -- giving every sentence the
whole line's span -- makes the transcript useless for seeking.
"""

import re

from meeting_subtitles.cleanup import strip_non_speech
from meeting_subtitles.client import Line, Snapshot

#: Split after . ! ? … and their full-width forms, when followed by a space or
#: end of text. The lookbehind keeps the punctuation with the sentence it ends.
_SPLIT = re.compile(r"(?<=[.!?…。！？])(?=\s|$)")

#: Abbreviations and initials that end in a period without ending a sentence.
_NON_TERMINAL = re.compile(
    r"(?:\b(?:[A-Za-z]|Dr|Mr|Mrs|Ms|Prof|etc|vs|e\.g|i\.e|approx|Fig|Eq|Ref|al)\.)\s*$",
    re.IGNORECASE,
)

#: Below this many characters a fragment is glued onto the neighbouring
#: sentence instead of standing alone -- "Yeah." on its own line is noise.
MIN_SENTENCE_CHARS = 12

#: Whisper punctuates most of the time, but on fast continuous speech it can
#: run for a paragraph without a full stop. Anything longer than this is cut at
#: the best clause boundary available so the subtitle stays readable.
MAX_SENTENCE_CHARS = 140

#: Preferred cut points inside an over-long run, best first: a comma is a real
#: clause boundary, a discourse marker is where a speaker restarts a thought.
_CLAUSE_BREAK = re.compile(r",\s+|;\s+|:\s+")
_DISCOURSE = re.compile(
    r"\s+(?=(?:and then|but then|so then|and so|but|and|so|because|however|"
    r"actually|basically|you know|I mean|right|okay|well)\s)",
    re.IGNORECASE,
)


def parse_time(value: str) -> float:
    """Parse the server's ``H:MM:SS.cc`` stamp into seconds."""
    parts = [p for p in str(value or "").split(":") if p != ""]
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def format_time(seconds: float) -> str:
    """Inverse of :func:`parse_time`, matching the server's formatting."""
    total_cs = int(round(max(seconds, 0.0) * 100))
    cs, total_s = total_cs % 100, total_cs // 100
    s, total_m = total_s % 60, total_s // 60
    return f"{total_m // 60}:{total_m % 60:02d}:{s:02d}.{cs:02d}"


def _split_long(piece: str) -> list[str]:
    """Cut an unpunctuated run into readable chunks.

    Prefers a comma or semicolon near the middle of the allowance, then a
    discourse marker, and only falls back to a word boundary when the speaker
    gave no structure at all.
    """
    if len(piece) <= MAX_SENTENCE_CHARS:
        return [piece]

    out: list[str] = []
    rest = piece
    while len(rest) > MAX_SENTENCE_CHARS:
        window = rest[:MAX_SENTENCE_CHARS]
        cut = None
        for pattern in (_CLAUSE_BREAK, _DISCOURSE):
            matches = [m for m in pattern.finditer(window)
                       if m.end() > MAX_SENTENCE_CHARS // 3]
            if matches:
                cut = matches[-1].end()
                break
        if cut is None:
            space = window.rfind(" ")
            cut = space + 1 if space > MAX_SENTENCE_CHARS // 3 else MAX_SENTENCE_CHARS
        out.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return [p for p in out if p]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping punctuation and merging fragments."""
    text = strip_non_speech(text)
    if not text:
        return []

    pieces: list[str] = []
    for piece in _SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        # "the model, i.e. GPT-4, was..." must not break after "i.e."
        if pieces and _NON_TERMINAL.search(pieces[-1]):
            pieces[-1] = f"{pieces[-1]} {piece}"
        else:
            pieces.append(piece)

    merged: list[str] = []
    for piece in pieces:
        if merged and (len(piece) < MIN_SENTENCE_CHARS
                       or len(merged[-1]) < MIN_SENTENCE_CHARS):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)

    # Merging can produce an over-long run, and so can a speaker who never
    # pauses for Whisper to punctuate; both are cut down here.
    result: list[str] = []
    for piece in merged:
        result.extend(_split_long(piece))
    return result


def split_line(line: Line) -> list[Line]:
    """Split one server line into sentence-level lines with interpolated times."""
    sentences = split_sentences(line.text)
    if not sentences:
        return []
    if len(sentences) == 1 and sentences[0] == (line.text or "").strip():
        return [line]

    start, end = parse_time(line.start), parse_time(line.end)
    span = max(end - start, 0.0)
    total = sum(len(s) for s in sentences) or 1

    # The parent's streaming translation covers all the sentences at once. It
    # is only the grey draft, but blanking it would leave the live row empty
    # until refinement lands, so hand each piece its proportional share.
    draft = (line.translation or "").strip()

    result: list[Line] = []
    consumed = 0
    for index, sentence in enumerate(sentences):
        ratio_start = consumed / total
        consumed += len(sentence)
        ratio_end = consumed / total
        last = index == len(sentences) - 1
        result.append(Line(
            speaker=line.speaker,
            text=sentence,
            start=format_time(start + span * ratio_start),
            # The last piece keeps the parent's exact end so the transcript's
            # final timestamp is not an interpolation artefact.
            end=line.end if last else format_time(start + span * ratio_end),
            translation=draft[int(len(draft) * ratio_start):
                              len(draft) if last else int(len(draft) * ratio_end)],
        ))
    return result


def resegment(snapshot: Snapshot) -> Snapshot:
    """Return the snapshot with its lines split into sentences."""
    lines: list[Line] = []
    for line in snapshot.lines:
        if line.is_silence or not (line.text or "").strip():
            lines.append(line)
            continue
        pieces = split_line(line)
        if not pieces:
            continue                    # was nothing but caption cues
        if len(pieces) == 1 and pieces[0].text == line.text:
            lines.append(line)          # unchanged: keep the server's translation
        else:
            lines.extend(pieces)
    snapshot.lines = lines
    return snapshot
