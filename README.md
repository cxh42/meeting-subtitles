# Meeting Subtitles

**Live English-to-Chinese meeting subtitles, with local transcription and translation.**

[简体中文](README.zh-CN.md) · [Design notes (Chinese)](docs/design-notes.zh-CN.md)

Meeting Subtitles is a Linux desktop tool for following English-language meetings
and keeping a bilingual transcript. It captures system playback and, optionally,
your microphone, so it works with meeting apps, browser videos, and local media
without a separate integration for each app.

With the default local engine, audio and text are processed on your computer.
Models need to be downloaded before offline use; no cloud API account is required.
The interface is in Simplified Chinese.

![Live bilingual subtitles in dark mode](docs/images/dark/subtitle-bar.png)

## Features

- **Live captions with sentence refinement.** Read the English source and a Chinese
  draft as speech arrives. Once a sentence is refined, its completed translation
  stays at the top while the next sentence develops.
- **A desktop launcher.** Start from the application menu, choose meeting options,
  and let the launcher manage the transcription engine.
- **Light and dark modes.** Change appearance in the title bar. The choice is saved
  immediately and restored next time, including for captions and transcript history.
- **A live display preview.** Adjust font size and opacity against a sample caption
  at the bottom of your screen before recording.
- **Readable history and local files.** Scroll back without losing your place,
  select text to copy, and save a Markdown transcript alongside the meeting audio.
- **Domain terminology.** A built-in CS/AI preset and optional custom terms help
  the models handle technical vocabulary and names.

WhisperLiveKit runs Whisper large-v3 for recognition and NLLB-1.3B for streaming
translation. A local Qwen3-4B model then refines completed sentences.

## Requirements

| Component | Requirement |
| --- | --- |
| System | Linux with PulseAudio or PipeWire's PulseAudio compatibility service. Tested on Ubuntu 24.04 with GNOME and XWayland. |
| GPU | NVIDIA with CUDA support. The default models use roughly 22 GB of VRAM with sentence refinement, or 14 GB without it. These are reference estimates, not fixed requirements. |
| Storage | Roughly 18–20 GB for the default model cache, plus space for dependencies and meeting recordings. |
| Python | Python 3.11 or later, with Tk and a CJK font. |

Memory use and latency depend on the models, runtime, hardware, and audio. The
current sentence-refinement path uses CUDA; a CPU-only setup does not provide
the full default experience.

## Installation

```bash
git clone https://github.com/cxh42/meeting-subtitles.git
cd meeting-subtitles
./install.sh
```

The installer checks system dependencies, creates a virtual environment, installs
the project, adds a desktop entry, and runs diagnostics. If system packages are
missing, it prints an installation command for you to run before trying again.

The engine downloads its Whisper and NLLB models when first started. Sentence
refinement loads Qwen from the local cache, so download it once before your first
meeting, or restore an existing model backup:

```bash
HF_HUB_OFFLINE=0 .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-4B-Instruct-2507')"
```

If you do not need refinement, turn off **整句润色** in the launcher and skip the
Qwen download. Without that model cached, refinement cannot run; streaming
translation remains available once the engine is ready.

To check the environment after installation or diagnose a problem:

```bash
.venv/bin/meeting-subtitles-doctor
```

The report covers audio devices, CUDA libraries, free GPU memory, Chinese fonts,
the display, model caches, and other dependencies, with suggested fixes for
failures. The free-memory check accounts for whether the engine is already
loaded: when there is not enough left for the refiner as well, refinement is
skipped and says so on the caption bar, rather than leaving the engine to run
out of memory partway through a meeting.

## Using the desktop app

Search for **会议字幕 / Meeting Subtitles** in the application menu, or run:

```bash
.venv/bin/meeting-subtitles
```

| Light mode | Dark mode |
| --- | --- |
| ![Launcher in light mode](docs/images/launcher.png) | ![Launcher in dark mode](docs/images/dark/launcher.png) |

1. Enter a meeting name, or leave it blank to use the default name with a date.
2. Choose whether to record the microphone, refine completed sentences, and use
   the built-in CS/AI terminology preset.
3. Set the font size and opacity. Click **预览字幕** to see a sample at the screen
   bottom. Use **切换深色 / 切换浅色** in the title bar to change appearance.
4. Wait for the engine to report that it is ready, then click **开始会议**.
   The launcher hides and the caption bar appears. First-time downloads and model
   loading can take several minutes; the launcher shows the current stage.
5. Click **结束并保存** on the caption bar to finish. The launcher returns with
   the recording location; **打开会议记录** opens the folder.

![Caption preview in light mode](docs/images/launcher-preview.png)

Meeting options are remembered between sessions, including the theme, caption
size and opacity -- adjusting the last two from the caption bar during a meeting
updates the launcher's sliders too. The engine stays running after a meeting so
it can be reused; **关闭引擎** stops it when you want to release its GPU memory.

### Captions and transcript history

The top section shows the most recent refined Chinese sentence. Below it are the
live English source and Chinese draft. The draft can change as more speech arrives;
refinement considers the completed sentence. Processing time varies with the audio
and available hardware.

| Control | Action |
| --- | --- |
| **转录记录** | Open the transcript for the current meeting. Select text to copy it. |
| **原文** | Show or hide the English source. |
| **暂停显示 / 继续显示** | Pause or resume the caption display. Transcription and recording continue. |
| **显示设置** | Adjust caption size and opacity during the meeting. Both are remembered, as is the light/dark theme. |
| **结束并保存** | End the meeting and finish saving its files. |

When you scroll up in the history window, the view stops following new text.
Use **回到最新** or scroll to the bottom to resume following it.

![Transcript history in light mode](docs/images/history.png)

By default, each meeting creates a folder under `~/Meetings/<date>_<name>/` with
`transcript.md` and `audio.wav`. The transcript is saved periodically during the
meeting and finalized when the session ends. Command-line options also support
SRT and JSON output.

## Command-line use

Start the engine in one terminal:

```bash
.venv/bin/meeting-subtitles-engine
```

Start a meeting in another terminal; this still opens the caption bar:

```bash
.venv/bin/meeting-subtitles-run --title "Weekly sync" --theme dark
```

Use `--help` to list all options. For example:

```bash
# Add terminology and names for this meeting
.venv/bin/meeting-subtitles-run --context "Kubernetes, Anirudh, SLO"

# Capture system playback only, without sentence refinement
.venv/bin/meeting-subtitles-run --no-mic --no-refine

# Record without a subtitle window; press Ctrl+C to finish
.venv/bin/meeting-subtitles-run --no-overlay --formats md,srt,json
```

Other display options include `--font-size 24 --opacity 0.8`. Use
`--theme light` or `--theme dark` for that session; omitting it uses the saved
appearance. Command-line theme overrides do not change the saved preference.

### Domain terminology

The default **CS/AI** preset provides 62 terms, such as LoRA, RLHF, vLLM, and
NeurIPS, along with translation guidance. In the launcher, turn off
**计算机与 AI 术语** for a general meeting; from the command line, use
`--domain general`. Add names or other meeting-specific terms with `--context`.
To define another preset, edit [domain.py](meeting_subtitles/domain.py).

## Backing up models

Check the cache and back it up before reinstalling or moving to another disk:

```bash
.venv/bin/python tools/models.py status
.venv/bin/python tools/models.py backup --to /mnt/backup/meeting-models
```

Restore it with:

```bash
.venv/bin/python tools/models.py restore --from /mnt/backup/meeting-models
```

A cache also accumulates revisions nothing points at and leftovers from
interrupted downloads -- 7.1 GB of them on the development machine. List them
first, then delete:

```bash
.venv/bin/python tools/models.py prune          # list only
.venv/bin/python tools/models.py prune --yes    # delete
```

To check recognition or refinement without waiting for another meeting, replay
a saved recording through the engine:

```bash
.venv/bin/python tools/replay.py ~/Meetings/<session>/audio.wav --seconds 60
```

It prints the recognised text, the refined text, and whether anything reached
the network -- with the models cached, nothing should.

## Limitations

- **Linux only.** Audio capture backends for macOS and Windows are not implemented.
- **Designed for English-to-Chinese meetings.** Other language pairs are not part
  of the default tested workflow.
- **Mixed audio.** System playback and the microphone are combined into one stream;
  the default setup does not distinguish individual speakers.
- **Recognition is imperfect.** Accents, overlapping speech, noise, and long silences
  can cause mistakes or spurious text. Review saved transcripts before reusing them.
- **Whole-window opacity.** Tk fades both the caption background and its text.
  Increase opacity when captions are hard to read over the meeting window.
- **Cold starts are slow.** Measured on the development machine, a first load
  reads 12.9 GB from disk for the engine and 7.5 GB for the refiner, so it
  usually takes one to three minutes and is much faster once the page cache is
  warm. Nothing goes over the network once the models are cached, so no proxy
  or VPN is needed.

## Licenses

The project code is [Apache-2.0](LICENSE). Model weights have separate licenses:

| Model | License |
| --- | --- |
| [Whisper large-v3 (.pt checkpoint)](https://github.com/openai/whisper#license) | MIT |
| [faster-whisper large-v3](https://huggingface.co/Systran/faster-whisper-large-v3) | MIT |
| [NLLB-200-distilled-1.3B](https://huggingface.co/facebook/nllb-200-distilled-1.3B) | CC-BY-NC-4.0 |
| [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) | Apache-2.0 |

The default streaming translator, NLLB, is licensed for non-commercial use. The
project's code license does not override those model terms. See the linked model
licenses before using a configuration commercially.

## Development and credits

Read [AGENTS.md](AGENTS.md) for development commands, architecture, and known
implementation pitfalls. [Design notes](docs/design-notes.zh-CN.md) explain the
text pipeline and operating details in Chinese; [DESIGN.md](DESIGN.md) records
the interface conventions.

The transcription server is [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit)
by Quentin Fuxa. This project installs and runs it as a dependency; it does not
copy the engine's source. Upstream dependencies and acknowledgments are listed
in [NOTICE](NOTICE).
