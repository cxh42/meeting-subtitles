"""Regenerate the README screenshots, reproducibly and without a real desktop.

Run under a virtual X server so the result does not depend on whose screen it
was taken on, what else was on it, or the DPI of the machine::

    xvfb-run -s "-screen 0 1920x1080x24 -nocursor" \
        python tools/screenshots.py --out docs/images

A real GNOME Wayland session cannot be used for this: the app runs on
XWayland, whose windows are redirected offscreen, so ``x11grab`` captures
black, and GNOME 43+ refuses the D-Bus screenshot call outright. Xvfb has no
compositor, so the root window really does contain the window's pixels.

The Chinese in the screenshots needs a Tk that can see CJK fonts. If the
running interpreter's Tk cannot (Anaconda ships one built without Xft), this
re-executes itself once with the system Tcl/Tk preloaded, the same trick
``meeting_subtitles.tkfix`` uses at startup.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meeting_subtitles.tkfix import ensure_cjk_tk  # noqa: E402

#: A plausible stretch of a research meeting. The subtitle bar shows only the
#: tail of this; the history window shows all of it, which is the point of
#: having two screenshots.
LINES = [
    dict(text="Okay, let's start with the results from last week.",
         start="0:03:02", end="0:03:05",
         translation="好，我们先从上周的结果开始。", refined=True),
    dict(text="The fine-tuned model beats the zero-shot baseline by about "
              "four points on the held-out set.",
         start="0:03:06", end="0:03:12",
         translation="微调后的模型在留出集上比零样本基线高出大约四个点。",
         refined=True),
    dict(text="Is that with the same tokenizer, or did you change it?",
         start="0:03:13", end="0:03:16",
         translation="那是用的同一个分词器，还是你换过了？", refined=True),
    dict(text="Same one. We only changed the learning rate schedule and the "
              "batch size.",
         start="0:03:17", end="0:03:21",
         translation="同一个。我们只改了学习率调度和批大小。", refined=True),
    dict(text="Right, so the ablation shows the retrieval component is doing "
              "most of the work here.",
         start="0:04:11", end="0:04:16",
         translation="对，所以消融实验说明检索模块承担了这里的大部分工作。",
         refined=True),
    dict(text="We should probably rerun it with a longer context window "
              "before the rebuttal deadline",
         start="0:04:16", end="0:04:21",
         translation="我们大概应该在反驳截止之前用更长的上下文窗口重跑一次",
         refined=False),
]
BUFFER_TEXT = "and check whether the scaling law still"
BUFFER_TRANSLATION = "并检查缩放定律是否仍然"


def capture(window, path: Path, settle: float = 1.5) -> None:
    """Grab one window's rectangle off the root window with ffmpeg.

    The pointer is warped off-screen first: Xvfb's ``-nocursor`` suppresses the
    root cursor but not the one a widget asks for, so a Text widget under the
    default pointer position leaves an I-beam in the middle of the shot.
    """
    try:
        window.event_generate("<Motion>", warp=True, x=-100, y=-100)
        window.update_idletasks()
    except Exception:
        pass
    pump(window, settle)
    x, y = window.winfo_rootx(), window.winfo_rooty()
    width, height = window.winfo_width(), window.winfo_height()
    path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "x11grab", "-video_size", f"{width}x{height}",
        "-i", f"{os.environ.get('DISPLAY', ':0')}+{x},{y}",
        "-frames:v", "1", str(path),
    ])
    pump(window, 2.0)
    process.wait(timeout=15)
    print(f"  {path}  {width}x{height}")


def pump(window, seconds: float) -> None:
    """Run the same event loop that delivers worker callbacks in the app."""
    window.after(int(seconds * 1000), window.quit)
    window.mainloop()


def shoot_overlay(out: Path, theme: str = "light") -> None:
    from meeting_subtitles.model import Line, Snapshot
    from meeting_subtitles.overlay import SubtitleOverlay

    overlay = SubtitleOverlay(on_close=lambda: None, font_size=17,
                              width_ratio=0.92, opacity=1.0, theme=theme)
    snapshot = Snapshot(
        lines=[Line(speaker=0, **line) for line in LINES],
        buffer_transcription=BUFFER_TEXT,
        buffer_translation=BUFFER_TRANSLATION,
        status="active_transcription",
    )
    overlay.push(snapshot)
    overlay.set_status("connected")
    capture(overlay.root, out / "subtitle-bar.png")

    overlay.show_history()
    history = overlay._history_window
    if history is not None:
        capture(history, out / "history.png")
    overlay.request_close()
    try:
        overlay.root.destroy()
    except Exception:
        pass


def shoot_launcher(out: Path, theme: str = "light") -> None:
    from meeting_subtitles.launcher import LauncherApp

    app = LauncherApp(preview=True, theme=theme)
    app.title_entry.set("每周项目同步")
    app.font_slider.set(20)
    app.opacity_slider.set(94)
    app.mic_toggle.set(True)
    app.refine_toggle.set(True)
    app.domain_toggle.set(True)
    app._update_preview()
    app._apply_state(True, True)
    capture(app.root, out / "launcher.png")
    app._show_preview()
    capture(app._preview_window, out / "launcher-preview.png")
    try:
        app._on_window_close()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="docs/images", type=Path)
    parser.add_argument("--theme", choices=("light", "dark", "both"), default="both",
                        help="截图外观（默认生成浅色和深色两套）")
    args = parser.parse_args(argv)

    if not os.environ.get("DISPLAY"):
        print("需要一个 X 显示。请用 xvfb-run 运行，见本文件开头。",
              file=sys.stderr)
        return 2
    ensure_cjk_tk(module=None)

    print("生成截图:")
    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    for theme in themes:
        out = args.out if theme == "light" else args.out / "dark"
        shoot_launcher(out, theme)
        shoot_overlay(out, theme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
