# AGENTS.md

Live English→Chinese meeting subtitles. A Tk overlay and launcher drive a
local WhisperLiveKit server over its WebSocket API. Linux only.

Read this file, not the README — the README sells the project, this one says
how not to break it. Long-form rationale lives in
`docs/design-notes.zh-CN.md` (Chinese).

## Commands

```bash
.venv/bin/meeting-subtitles-doctor       # run FIRST when anything misbehaves
.venv/bin/python -m pytest tests -q      # no GPU, no audio, no network
.venv/bin/ruff check meeting_subtitles tools tests
.venv/bin/meeting-subtitles-engine       # the server, foreground
./install.sh                             # venv + deps + desktop entry

.venv/bin/python tools/replay.py <audio.wav>   # re-run a meeting through the engine
.venv/bin/python tools/models.py prune         # list dead model blobs (--yes deletes)
```

Screenshots in `docs/images/` are generated, never hand-edited:
`xvfb-run -a -s "-screen 0 1920x1080x24 -nocursor" .venv/bin/python tools/screenshots.py --out docs/images`

## Shape

- `serve.py` starts the engine **in-process** so the PID the launcher watches
  is the server. It must finish setting the environment before importing
  `whisperlivekit`.
- `audio.py` → ffmpeg (PulseAudio monitor + mic, mixed) → 16 kHz mono PCM →
  `client.py` → server → `Snapshot` of `Line`s.
- Each snapshot is **complete state, not a delta**. Consumers replace, never
  merge. `recorder.py` rewrites its files from the newest snapshot.
- `segment.py` splits server lines into sentences; `refine.py` retranslates a
  finished sentence with a local LLM and pins it in the overlay.
- `watchdog.py` weighs speech going in against text coming out, because the
  server's failure mode is to stay up and stop producing. It reads no clock --
  a PCM chunk's length is its duration -- so it is testable without a soundcard.
- `model.py` holds the data types and imports nothing outside the stdlib.
  Keep it that way — `segment`, `recorder` and the tests depend on it.

## Conventions

- Code, comments and commit messages in English. Anything a user sees —
  labels, log messages, errors — in Chinese.
- Comments say **why**, never what. If a line needs "what", rename something.
  A comment that records a trap belongs next to the code, not only here.
- User-facing failures name the fix, not just the fault. `doctor.py` is the
  model for this.
- No mock-based tests. `tests/` covers the text pipeline with real strings;
  everything else is verified by running it (see below).

## Verifying things that need hardware

- **Audio.** Never capture the user's real devices for a test — you will
  record whatever they are doing. Make a null sink, play into it, capture its
  monitor, unload it, restore the default sink:
  `pactl load-module module-null-sink sink_name=test` … `pactl set-default-sink`.
- **The pipeline.** The decisive tool is `tools/replay.py`: it pushes a
  recorded `audio.wav` back through the engine and prints the snapshots. It
  separates "the server is wrong" from "our processing is wrong" in one run, is
  repeatable in a way that a live meeting is not, and reports whether anything
  reached the network. Keep the default real-time pacing unless only the text
  pipeline is under test -- the server segments on pauses, so audio pushed
  through at full speed arrives as one block and segments nothing like the
  meeting it came from.
- **The GUI.** Screenshots need Xvfb: GNOME refuses the D-Bus screenshot call,
  and an XWayland window comes out black under `x11grab` because the
  compositor redirects it offscreen.
- **Tk from a test.** `root.after` from a worker thread needs a running
  `mainloop()`. Driving a window with `update()` in a loop silently drops
  every threaded callback, which looks exactly like a broken state machine.

## Traps

Each of these failed silently or blamed the wrong thing. All are current.

- **A CUDA OOM in the ASR looks exactly like a healthy meeting.** The server
  catches the backend exception per chunk and carries on, so the WebSocket
  stays open, snapshots keep arriving and the transcript file stays readable --
  it just never gains another word. The cause is usually the refiner: it is a
  *separate process* on the same card, so its 9 GB does not fail in the process
  that asked for it, it fails in whichever allocates next. `refine.py` checks
  `torch.cuda.mem_get_info()` before loading, and `watchdog.py` warns when
  speech keeps going in with no text coming back. Elapsed time cannot be the
  trigger -- a pause is indistinguishable from a dead backend -- so only
  seconds of audio above a speech threshold count.
- **Two processes, one settings file, last writer wins.** The launcher stays
  alive behind the overlay with its own copy of the settings, so an adjustment
  made in the overlay was overwritten by the launcher's stale sliders the
  moment the meeting ended. Both sides now re-read the file rather than saving
  a snapshot they took minutes ago.
- **CUDA 12 vs 13.** PyPI's `torch` now carries CUDA 13; CTranslate2 needs
  `libcublas.so.12`. The engine starts, answers `/health`, accepts audio, then
  fails every chunk and writes an empty transcript.
  `ctranslate2.get_cuda_device_count()` still returns 1, so probe with
  `ctypes.CDLL("libcublas.so.12")`. Install torch from the cu12x index, with
  `--force-reinstall` — pip will not replace an already-satisfied torch just
  because the index changed.
- **The released whisperlivekit silently ignores the glossary.** `?context=`
  arrived after 0.2.26, and an older server drops an unknown query parameter
  without complaining, so terminology conditioning just stops happening.
  `pyproject.toml` allows the PyPI release; `install.sh` pins the commit that
  has it. `doctor.py` probes for `session_asr_proxy.session_context_capability`
  rather than trusting the version number.
- **`HF_HUB_OFFLINE` is read at import time.** `huggingface_hub` freezes it
  into a module constant. Setting it after the import does nothing and the
  load then hangs on a hostname that does not resolve here.
- **Whisper continues the prompt's style.** The term list is sent as a
  finished sentence, not a bare comma list. As a list it hallucinates more
  list before the speech starts and emits `*sad music continues*` cues.
  Measured by replaying the same audio three ways.
- **`--pause-segmentation-seconds` defaults to 5 s**, which never happens in a
  meeting, so the whole call becomes one transcript line. `serve.py` sets 1.2.
- **`tk.Misc._w` is the Tcl widget path.** A widget that stores its width in
  `self._w` breaks every later geometry call with `bad screen distance`, which
  names nothing. Has bitten this codebase twice; use `_bw`/`_cw`.
- **Tk `-alpha` before the window is mapped is dropped**, so the first opacity
  change jumps. Re-apply after `<Map>`.
- **X11 shape masks take the *frame* size.** For a decorated window the shaped
  window is the WM frame, larger than the client area, and a client-sized mask
  cuts the bottom off. `RoundedWindow.apply_current()` exists for this.
- **`grep -q` in a `set -o pipefail` pipeline returns 141.** grep exits at the
  first match, the producer dies of SIGPIPE, and a successful match reads as
  failure. Match against a variable instead.
- **`pkill -f <pattern>` matches the shell running it.** Never put the pattern
  literally in the same command.
- **Anaconda's Tk has no Xft**, so it sees no CJK font and renders Chinese as
  boxes. `tkfix.py` re-execs once with the system Tcl/Tk preloaded; a test
  that calls `ensure_cjk_tk` will re-exec into whatever `module=` says.
- **`pactl` translates its field labels.** Parsers must force `LC_ALL=C`.
- **Desktop launchers inherit no shell environment**, so a proxy in `.bashrc`
  is invisible. It goes in the config directory's `env` file instead.

## Updating this file

Add an entry only if **all three** hold: it cost more than half an hour; it
failed silently or with an error naming the wrong cause; and it is not visible
from reading the code it affects. Everything else belongs in a comment where
the problem lives.

Entries are one or two sentences — symptom first, then cause. Delete an entry
when the trap stops existing. This file is a cheat sheet: if it ever reads
like a changelog, it has failed.
