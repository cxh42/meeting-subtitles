"""Always-on-top bilingual subtitle window.

A frameless Tk window pinned above every other window, showing the English the
speaker just said and the Chinese translation underneath -- movie-subtitle
style, so it can sit over a Zoom window without switching focus away from it.

Tk must own the main thread, so the network side runs in a worker thread and
hands snapshots over through a queue drained by a periodic ``after`` callback.
"""

import logging
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from meeting_subtitles.gnome import screen_scale
from meeting_subtitles.model import Snapshot
from meeting_subtitles.rounded import RoundedWindow, undecorate

logger = logging.getLogger(__name__)

BG = "#1c1c1c"
FG_SOURCE = "#f4f6fb"
FG_TARGET = "#ff9f6b"
FG_PENDING = "#9a9a9a"
FG_MUTED = "#787878"
TOOLBAR_BG = "#222222"
DIVIDER = "#3a3a3a"

CJK_FONTS = ("Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC",
             "WenQuanYi Micro Hei", "Droid Sans Fallback")

#: How many wrapped rows each of the three text rows may occupy. The actual
#: character budget is measured from the font at runtime, so it stays correct
#: when the user changes the size or moves to a different screen -- a fixed
#: character count silently overflowed the window at larger fonts and the
#: bottom of the sentence was cut off.
MAX_LINES_FINAL = 2
MAX_LINES_SOURCE = 2
MAX_LINES_DRAFT = 2

#: While a new sentence is still this short, keep showing the previous one so
#: its polished translation is actually on screen long enough to be read.
HOLD_UNTIL_CHARS = 14


def _pick_font(root: tk.Tk) -> str:
    """First installed CJK-capable family, so Chinese never renders as tofu."""
    available = set(tkfont.families(root))
    for family in CJK_FONTS:
        if family in available:
            return family
    return "TkDefaultFont"


def _tail(text: str, limit: int) -> str:
    """Keep the end of a growing line so the newest words stay on screen."""
    text = text.strip()
    if len(text) <= limit:
        return text
    clipped = text[-limit:]
    space = clipped.find(" ")
    if 0 < space < 40:
        clipped = clipped[space + 1:]
    return "… " + clipped


class SubtitleOverlay:
    """Tk overlay driven by :class:`meeting.client.Snapshot` updates."""

    def __init__(
        self,
        on_close: Callable[[], None] | None = None,
        font_size: int = 19,
        width_ratio: float = 0.78,
        opacity: float = 0.94,
    ) -> None:
        self.queue: queue.Queue[Snapshot] = queue.Queue()
        self.on_close = on_close
        self.font_size = font_size
        self.show_source = True
        self.paused = False
        self.started_at = time.monotonic()
        self.opacity = max(0.35, min(1.0, opacity))
        self.status_text = "连接中"
        self._closed = False
        self._close_requested = False
        self._drag_origin = (0, 0)
        self._final_text = ""
        self._history_window: tk.Toplevel | None = None
        self._history_text: tk.Text | None = None
        self._history_rounded = None
        self._status_pill: tk.Label | None = None
        self._jump_button: tk.Button | None = None
        self._rendered: list = []
        self._frozen = False
        self._pending = 0
        self.history: list = []

        self.root = tk.Tk()
        self.root.title("Meeting Subtitles")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._apply_opacity()
        self.root.configure(bg=BG)

        # Canvas/padding geometry is in pixels but fonts scale with DPI; grow
        # both together so the bar is not cramped on a HiDPI panel.
        self.k = screen_scale(self.root)
        family = _pick_font(self.root)
        self.source_font = tkfont.Font(family=family, size=self.font_size)
        self.target_font = tkfont.Font(family=family, size=self.font_size + 2, weight="bold")
        self.draft_font = tkfont.Font(family=family, size=self.font_size)
        self.small_font = tkfont.Font(family=family, size=10)
        self.history_font = tkfont.Font(family=family, size=max(11, self.font_size - 4))

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.width = int(screen_w * width_ratio)
        self._pad_x = int(16 * self.k)
        wrap = self.width - 2 * self._pad_x

        self._recompute_budgets()
        self._build_toolbar()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=self._pad_x,
                  pady=(int(6 * self.k), int(12 * self.k)))
        self.body = body

        # Row 1: the most recent fully re-translated sentence. It stays until
        # the next one is ready, so the polished Chinese is actually readable --
        # previously it appeared only while the next sentence was still short
        # and flashed past before it could be read.
        self.final_label = tk.Label(
            body, text="", font=self.target_font, fg=FG_TARGET, bg=BG,
            wraplength=wrap, justify="left", anchor="w",
        )
        self.final_label.pack(fill="x")

        self.divider = tk.Frame(body, bg=DIVIDER, height=max(1, int(self.k)))

        # Row 2/3: what is being said right now, and its rough draft.
        self.source_label = tk.Label(
            body, text="", font=self.source_font, fg=FG_SOURCE, bg=BG,
            wraplength=wrap, justify="left", anchor="w",
        )
        self.source_label.pack(fill="x")

        self.target_label = tk.Label(
            body, text="等待语音…", font=self.draft_font, fg=FG_PENDING, bg=BG,
            wraplength=wrap, justify="left", anchor="w", pady=int(2 * self.k),
        )
        self.target_label.pack(fill="x")

        for widget in (body, self.final_label, self.source_label, self.target_label):
            self._make_draggable(widget)

        # Bottom-centred, a little above the screen edge so it clears the dock.
        self.root.update_idletasks()
        x = (screen_w - self.width) // 2
        height = self.root.winfo_reqheight()
        y = screen_h - height - int(90 * self.k)
        self.root.geometry(f"{self.width}x{height}+{x}+{max(y, 0)}")
        # Without a layout pass here Tk keeps painting the children at their
        # *requested* width -- far narrower than the bar -- and the right-hand
        # part of the window stays unpainted until something else forces a
        # relayout, which used to be the first subtitle arriving.
        self.root.update()

        # GNOME rounds every window it decorates; a frameless bar with square
        # corners is the one thing on screen that looks unfinished.
        self._rounded = RoundedWindow(self.root, radius=int(14 * self.k))
        # Drive the mask from <Configure> rather than from the code that asks
        # for a new size: a shape set before X has actually resized the window
        # is clipped to the old geometry and never catches up, which left the
        # bottom rows of a long subtitle invisible.
        self.root.bind("<Configure>", self._on_configure, add="+")
        self._rounded.apply(self.width, height)

        # The window exists now, so the alpha set during construction can
        # finally stick; <Map> covers a compositor that maps it even later.
        self._apply_opacity()
        self.root.bind("<Map>", lambda _e: self._apply_opacity())

        self.root.after(100, self._drain)
        self.root.after(1000, self._tick)

    def _build_toolbar(self) -> None:
        k = self.k
        bar = tk.Frame(self.root, bg=TOOLBAR_BG, height=int(28 * k))
        bar.pack(fill="x", side="top")
        self._make_draggable(bar)

        self.status_label = tk.Label(
            bar, text="● 连接中", font=self.small_font, fg=FG_MUTED, bg=TOOLBAR_BG,
        )
        self.status_label.pack(side="left", padx=(int(12 * k), int(8 * k)),
                               pady=int(4 * k))
        self._make_draggable(self.status_label)

        self.timer_label = tk.Label(
            bar, text="00:00", font=self.small_font, fg=FG_MUTED, bg=TOOLBAR_BG,
        )
        self.timer_label.pack(side="left")
        self._make_draggable(self.timer_label)

        def button(text: str, command, tooltip: str = "") -> tk.Button:
            btn = tk.Button(
                bar, text=text, font=self.small_font, command=command,
                fg=FG_SOURCE, bg=TOOLBAR_BG, activebackground="#232a38",
                activeforeground=FG_SOURCE, relief="flat", bd=0,
                padx=int(8 * k), pady=1, highlightthickness=0, cursor="hand2",
            )
            btn.pack(side="right", padx=int(2 * k), pady=int(3 * k))
            return btn

        # "结束并保存" is the real action; a bare ✕ reads as "hide the bar" and
        # left no obvious way to finish the recording.
        end = button("结束并保存", self.close)
        end.configure(fg="#ffb4a8")
        button("A+", lambda: self._resize_font(+2))
        button("A−", lambda: self._resize_font(-2))
        button("◐+", lambda: self._adjust_opacity(+0.06))
        button("◐−", lambda: self._adjust_opacity(-0.06))
        self.source_button = button("原文", self._toggle_source)
        self.pause_button = button("暂停", self._toggle_pause)
        button("历史", self.show_history)

    def _make_draggable(self, widget: tk.Misc) -> None:
        widget.bind("<Button-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)
        self._window_origin = (self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        x = self._window_origin[0] + dx
        y = self._window_origin[1] + dy
        self.root.geometry(f"+{x}+{y}")

    def _apply_opacity(self) -> None:
        """X11 gives Tk only whole-window alpha, so the text fades with the
        background. 0.8 is about as transparent as it can get and stay
        comfortably readable over a bright video call.

        Setting -alpha before the window is mapped is silently dropped by the
        window manager, which left the bar fully opaque until the first click
        on the opacity button -- so it appeared to jump 1.0 -> 0.74 once and
        then behave. Called again from <Map> for that reason.
        """
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass

    def _adjust_opacity(self, delta: float) -> None:
        self.opacity = max(0.35, min(1.0, self.opacity + delta))
        self._apply_opacity()

    def _char_budget(self, font: tkfont.Font, lines: int) -> int:
        """How many characters of this font fit in ``lines`` wrapped rows.

        Measured against a mixed sample so it lands between the width of Latin
        text and the much wider CJK glyphs, rather than assuming either.
        """
        sample = "the model 模型 attention 注意力 "
        try:
            per_char = max(font.measure(sample) / len(sample), 1.0)
        except tk.TclError:
            per_char = float(self.font_size)
        wrap = max(self.width - 2 * self._pad_x, 100)
        # Wrapping never fills the last row completely; 0.92 keeps the estimate
        # from promising a row that does not exist.
        return max(20, int(wrap * lines * 0.92 / per_char))

    def _recompute_budgets(self) -> None:
        self._budget_final = self._char_budget(self.target_font, MAX_LINES_FINAL)
        self._budget_source = self._char_budget(self.source_font, MAX_LINES_SOURCE)
        self._budget_draft = self._char_budget(self.draft_font, MAX_LINES_DRAFT)

    def _resize_font(self, delta: int) -> None:
        self.font_size = max(11, min(46, self.font_size + delta))
        self.source_font.configure(size=self.font_size)
        self.target_font.configure(size=self.font_size + 2)
        self.draft_font.configure(size=self.font_size)
        self._recompute_budgets()
        self._final_text = ""          # force the pinned row to be re-clipped
        self._fit_height()

    def _toggle_source(self) -> None:
        self.show_source = not self.show_source
        if self.show_source:
            self.source_label.pack(fill="x", before=self.target_label)
            self.source_button.configure(fg=FG_SOURCE)
        else:
            self.source_label.pack_forget()
            self.source_button.configure(fg=FG_MUTED)
        self._fit_height()

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text="继续" if self.paused else "暂停")

    def set_status(self, text: str) -> None:
        """Called from the network thread; a plain attribute write is enough
        because ``_tick`` reads it on the Tk thread."""
        self.status_text = text

    def request_close(self) -> None:
        """Ask the window to close from another thread.

        Tk widgets may only be touched from the thread running ``mainloop``,
        including ``after``, so the worker only flips a flag and the periodic
        ``_drain`` callback does the actual teardown.
        """
        self._close_requested = True

    def push(self, snapshot: Snapshot) -> None:
        """Called from the network thread."""
        self.queue.put(snapshot)

    def _drain(self) -> None:
        if self._closed:
            return
        if self._close_requested:
            self.close()
            return
        latest: Snapshot | None = None
        try:
            while True:
                latest = self.queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None and not self.paused:
            self._render(latest)
            if self._history_window is not None and self._history_window.winfo_exists() \
                    and self._history_window.state() != "withdrawn":
                self._fill_history()
        self.root.after(100, self._drain)

    def _render(self, snapshot: Snapshot) -> None:
        speech = snapshot.speech_lines
        if not speech:
            return
        self.history = speech

        # The pinned row holds the newest sentence that has been re-translated
        # in full. It is deliberately *not* cleared when the speaker moves on:
        # the polished Chinese has to stay put long enough to read.
        newest_final = None
        for line in reversed(speech):
            if line.refined and line.translation.strip():
                newest_final = line
                break
        if newest_final is not None and newest_final.text != self._final_text:
            self._final_text = newest_final.text
            self.final_label.configure(
                text=_tail(newest_final.translation, self._budget_final))
            if not self.divider.winfo_ismapped():
                self.divider.pack(fill="x", pady=int(6 * self.k),
                                  before=self.source_label)

        # The live rows always track the sentence in progress, even when it is
        # the same one that is pinned above -- the reader follows the English
        # here and confirms the meaning above.
        current = speech[-1]
        source = current.text.strip()
        if snapshot.buffer_transcription:
            source = f"{source} {snapshot.buffer_transcription.strip()}".strip()

        if current.refined and current.text == self._final_text:
            draft = ""          # already shown, polished, in the pinned row
        else:
            draft = (current.translation or "").strip()
            if snapshot.buffer_translation:
                draft = f"{draft} {snapshot.buffer_translation.strip()}".strip()

        self.source_label.configure(text=_tail(source, self._budget_source))
        self.target_label.configure(
            text=_tail(draft, self._budget_draft) if draft else "",
            fg=FG_PENDING,
        )
        self._fit_height()

    def show_history(self) -> None:
        """Open (or raise) a scrollable window with everything said so far."""
        if self._history_window is not None and self._history_window.winfo_exists():
            self._history_window.deiconify()
            self._history_window.lift()
            self._jump_to_latest()
            return

        window = tk.Toplevel(self.root)
        window.title("本次转录")
        window.configure(bg=BG)
        window.attributes("-topmost", True)
        width = int(min(self.width, self.root.winfo_screenwidth() * 0.62))
        height = int(self.root.winfo_screenheight() * 0.62)
        window.geometry(f"{width}x{height}")

        bar = tk.Frame(window, bg=TOOLBAR_BG)
        bar.pack(fill="x")
        title = tk.Label(bar, text="本次转录", font=self.small_font, fg=FG_SOURCE,
                         bg=TOOLBAR_BG)
        title.pack(side="left", padx=(int(14 * self.k), 0), pady=int(7 * self.k))

        def start_drag(event):
            self._hist_drag = (event.x_root, event.y_root,
                               window.winfo_x(), window.winfo_y())

        def do_drag(event):
            ox, oy, wx, wy = self._hist_drag
            window.geometry(f"+{wx + event.x_root - ox}+{wy + event.y_root - oy}")

        for widget in (bar, title):
            widget.bind("<Button-1>", start_drag)
            widget.bind("<B1-Motion>", do_drag)

        close = tk.Label(bar, text="✕", font=self.small_font, fg=FG_MUTED,
                         bg=TOOLBAR_BG, cursor="hand2",
                         padx=int(12 * self.k), pady=int(4 * self.k))
        close.pack(side="right", padx=(0, int(8 * self.k)))
        close.bind("<Button-1>", lambda _e: window.withdraw())
        close.bind("<Enter>", lambda _e: close.configure(fg=FG_SOURCE))
        close.bind("<Leave>", lambda _e: close.configure(fg=FG_MUTED))

        self._status_pill = tk.Label(
            bar, text="", font=self.small_font, fg=FG_MUTED, bg=TOOLBAR_BG)
        self._status_pill.pack(side="right", padx=int(10 * self.k))

        frame = tk.Frame(window, bg=BG)
        frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(frame, bg=TOOLBAR_BG, troughcolor=BG, bd=0,
                                 highlightthickness=0)
        scrollbar.pack(side="right", fill="y")
        text = tk.Text(
            frame, bg=BG, fg=FG_SOURCE, font=self.history_font, wrap="word",
            bd=0, highlightthickness=0, padx=int(16 * self.k),
            pady=int(12 * self.k),
        )
        text.pack(side="left", fill="both", expand=True)

        # Every route that can move the view goes through _on_scroll, so the
        # frozen/following state is decided in one place.
        def on_yview(*args):
            scrollbar.set(*args)
            self._on_scroll()
        text.configure(yscrollcommand=on_yview)

        def scroll_command(*args):
            text.yview(*args)
            self._on_scroll()
        scrollbar.configure(command=scroll_command)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                         "<Prior>", "<Next>", "<Up>", "<Down>"):
            text.bind(sequence, lambda _e: self.root.after_idle(self._on_scroll),
                      add="+")

        text.tag_configure("time", foreground=FG_MUTED)
        text.tag_configure("en", foreground=FG_SOURCE, spacing1=int(6 * self.k))
        text.tag_configure("zh_final", foreground=FG_TARGET, spacing3=int(8 * self.k))
        text.tag_configure("zh_draft", foreground=FG_PENDING, spacing3=int(8 * self.k))

        # Floating "jump to latest", the way a chat app does it.
        self._jump_button = tk.Button(
            window, text="↓ 跳到最新", font=self.small_font, command=self._jump_to_latest,
            fg="#ffffff", bg="#e95420", activebackground="#f06d42",
            activeforeground="#ffffff", relief="flat", bd=0,
            padx=int(14 * self.k), pady=int(6 * self.k),
            highlightthickness=0, cursor="hand2")

        self._history_window = window
        self._history_text = text
        self._rendered: list = []
        self._frozen = False
        self._pending = 0

        # Same treatment as the launcher: drop the WM title bar so the shape
        # applies to the window we actually measure, and draw our own.
        undecorate(window)
        window.update_idletasks()
        self._history_rounded = RoundedWindow(window, radius=int(12 * self.k))
        window.bind("<Configure>", lambda e: self._history_rounded.apply_current()
                    if e.widget is window else None)
        window.after(120, self._history_rounded.apply_current)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)

        self._fill_history()
        window.after_idle(self._jump_to_latest)

    def _at_bottom(self) -> bool:
        text = self._history_text
        if text is None or not text.winfo_exists():
            return True
        try:
            return text.yview()[1] >= 0.999
        except tk.TclError:
            return True

    def _on_scroll(self) -> None:
        """Freeze while the reader is above the tail, resume at the bottom."""
        if self._history_text is None:
            return
        if self._at_bottom():
            if self._frozen:
                self._frozen = False
                self._fill_history()
            self._set_history_state(False)
        else:
            self._frozen = True
            self._set_history_state(True)

    def _set_history_state(self, frozen: bool) -> None:
        if self._status_pill is None or not self._status_pill.winfo_exists():
            return
        if frozen:
            label = f"已暂停 · {self._pending} 条新内容" if self._pending else "已暂停"
            self._status_pill.configure(text=label, fg=FG_TARGET)
            self._jump_button.place(relx=0.5, rely=1.0, anchor="s",
                                    y=-int(18 * self.k))
            # A placed widget does not automatically sit above its packed
            # siblings; without this the button is mapped but never painted.
            self._jump_button.lift()
        else:
            self._status_pill.configure(text="跟随最新", fg=FG_MUTED)
            self._jump_button.place_forget()

    def _jump_to_latest(self) -> None:
        self._frozen = False
        self._pending = 0
        self._fill_history()
        text = self._history_text
        if text is not None and text.winfo_exists():
            text.see("end")
        self._set_history_state(False)

    def _fill_history(self) -> None:
        """Append what is new; never touch what the reader is looking at.

        The previous version rebuilt the whole Text and restored the scroll
        *fraction*. As the meeting grew, the same fraction pointed at a
        different line, so the text crept under the reader on every update.
        Rendering is append-only now, and while the reader has scrolled up
        nothing is written at all -- the new lines are simply counted.
        """
        text = self._history_text
        if text is None or not text.winfo_exists():
            return

        signatures = [(line.text, line.translation, line.refined)
                      for line in self.history]
        if signatures == self._rendered:
            return

        if self._frozen:
            self._pending = max(0, len(signatures) - len(self._rendered))
            self._set_history_state(True)
            return

        # Refinement can rewrite a line that is already on screen, so redraw
        # from the first block that actually differs rather than assuming only
        # the tail changed.
        first_changed = len(self._rendered)
        for index, signature in enumerate(signatures):
            if index >= len(self._rendered) or self._rendered[index] != signature:
                first_changed = index
                break

        text.configure(state="normal")
        if first_changed < len(self._rendered):
            mark = f"block{first_changed}"
            if mark in text.mark_names():
                text.delete(mark, "end")
            else:
                text.delete("1.0", "end")
                first_changed = 0
        for index in range(first_changed, len(signatures)):
            line = self.history[index]
            text.mark_set(f"block{index}", "end-1c")
            text.mark_gravity(f"block{index}", "left")
            text.insert("end", f"{line.start}\n", "time")
            text.insert("end", f"{line.text.strip()}\n", "en")
            if line.translation.strip():
                text.insert("end", f"{line.translation.strip()}\n",
                            "zh_final" if line.refined else "zh_draft")
        text.configure(state="disabled")

        self._rendered = signatures
        self._pending = 0
        # Not frozen means the reader is at the tail (scrolling up is what sets
        # the flag), so stick to it. Testing _at_bottom() here would always say
        # no: the text just inserted is what pushed the view off the bottom.
        text.see("end")

    def _on_configure(self, event) -> None:
        if event.widget is self.root:
            self._rounded.apply_current()

    def _fit_height(self) -> None:
        """Resize to fit the current text, growing upward.

        An ``overrideredirect`` window keeps whatever size ``geometry`` gave it,
        so a line that wraps to two rows would otherwise be clipped. The bottom
        edge stays put: the bar sits near the bottom of the screen, so it has to
        grow towards the top.
        """
        self.root.update_idletasks()
        wanted = self.root.winfo_reqheight()
        # Last-ditch guard: whatever the text does, the bar must not grow past
        # what the screen can show, or its bottom rows are simply invisible.
        wanted = min(wanted, int(self.root.winfo_screenheight() * 0.5))
        current = self.root.winfo_height()
        if wanted == current:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        bottom = y + current
        self.root.geometry(f"{self.width}x{wanted}+{x}+{max(bottom - wanted, 0)}")

    def _tick(self) -> None:
        if self._closed:
            return
        elapsed = int(time.monotonic() - self.started_at)
        self.timer_label.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        colour = {"connected": "#5ad18f", "finished": FG_MUTED}.get(self.status_text, "#e0a458")
        label = {"connected": "录制中", "finished": "已结束"}.get(self.status_text, self.status_text)
        self.status_label.configure(text=f"● {label}", fg=colour)
        self.root.after(1000, self._tick)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.on_close:
            self.on_close()
        try:
            self.root.quit()
        except tk.TclError:
            pass

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._closed = True
            try:
                self.root.destroy()
            except tk.TclError:
                pass
