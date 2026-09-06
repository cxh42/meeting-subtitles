"""Always-on-top bilingual subtitle window.

A frameless Tk window pinned above every other window, showing the English the
speaker just said and the Chinese translation underneath -- movie-subtitle
style, so it can sit over a Zoom window without switching focus away from it.

Tk must own the main thread, so the network side runs in a worker thread and
hands snapshots over through a queue drained by a periodic ``after`` callback.
"""

import logging
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from meeting_subtitles import gnome as ui
from meeting_subtitles.gnome import screen_scale
from meeting_subtitles.model import Snapshot
from meeting_subtitles.rounded import RoundedWindow, undecorate
from meeting_subtitles.settings import Settings

logger = logging.getLogger(__name__)

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
        theme: str | None = None,
    ) -> None:
        self.theme = theme if theme is not None else Settings()["theme"]
        self.palette = ui.get_palette(self.theme)
        self._draft_color = "#c9cfd2" if self.theme == "dark" else "#414b51"
        self.queue: queue.Queue[Snapshot] = queue.Queue()
        self.on_close = on_close
        self.font_size = max(11, min(46, font_size))
        self.show_source = True
        self.paused = False
        self.started_at = time.monotonic()
        self.opacity = max(0.35, min(1.0, opacity))
        self.status_text = "连接中"
        self._notices: dict[str, str] = {}
        self._notice_lock = threading.Lock()
        self._notice = ""
        self._notice_shown: str | None = None
        self._appearance_job: str | None = None
        self._closed = False
        self._close_requested = False
        self._drag_origin = (0, 0)
        self._final_text = ""
        self._last_snapshot: Snapshot | None = None
        self._paused_snapshot: Snapshot | None = None
        self._history_window: tk.Toplevel | None = None
        self._history_text: tk.Text | None = None
        self._history_rounded = None
        self._status_pill: tk.Label | None = None
        self._jump_button: tk.Button | None = None
        self._history_count: tk.Label | None = None
        self._history_empty: tk.Frame | None = None
        self._display_menu: tk.Menu | None = None
        self._rendered: list = []
        self._frozen = False
        self._pending = 0
        self.history: list = []

        # className sets the window's WM_CLASS, which is how the desktop
        # associates a running window with its .desktop entry -- for the icon
        # in the dash, and for grouping. Tk's default is the bare "Tk", which
        # every other Tk program on the system also claims.
        self.root = tk.Tk(className="meeting-subtitles")
        self.root.title("会议字幕")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._apply_opacity()
        self.root.configure(bg=self.palette.WINDOW)

        # Canvas/padding geometry is in pixels but fonts scale with DPI; grow
        # both together so the bar is not cramped on a HiDPI panel.
        self.k = screen_scale(self.root)
        family = _pick_font(self.root)
        self.source_font = tkfont.Font(family=family, size=max(11, self.font_size - 3))
        self.target_font = tkfont.Font(family=family, size=self.font_size)
        self.draft_font = tkfont.Font(family=family, size=self.font_size)
        self.small_font = tkfont.Font(family=family, size=10)
        self.caption_font = tkfont.Font(family=family, size=9)
        self.heading_font = tkfont.Font(family=family, size=13, weight="bold")
        self.history_font = tkfont.Font(family=family, size=12)
        self.history_target_font = tkfont.Font(family=family, size=14)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.width = min(screen_w - int(24 * self.k),
                         max(int(580 * self.k), int(screen_w * width_ratio)))
        self._pad_x = int(24 * self.k)
        self._text_inset = 0
        wrap = self.width - 2 * self._pad_x - self._text_inset

        self._recompute_budgets()
        self._build_toolbar()

        body = tk.Frame(self.root, bg=self.palette.WINDOW)
        body.pack(fill="both", expand=True, padx=self._pad_x,
                  pady=(int(12 * self.k), int(16 * self.k)))
        self.body = body

        self.final_frame = tk.Frame(body, bg=self.palette.WINDOW)
        self.final_label = tk.Label(
            self.final_frame, text="", font=self.target_font,
            fg=self.palette.TEXT, bg=self.palette.WINDOW,
            wraplength=wrap, justify="left", anchor="w", bd=0, padx=0,
        )
        self.final_label.pack(fill="x")

        self.divider = tk.Frame(body, bg=self.palette.SEPARATOR, height=1)
        self.live_frame = tk.Frame(body, bg=self.palette.WINDOW)
        self.live_frame.pack(fill="x")
        self.source_label = tk.Label(
            self.live_frame, text="", font=self.source_font,
            fg=self.palette.TEXT_DIM, bg=self.palette.WINDOW,
            wraplength=wrap, justify="left", anchor="w", bd=0, padx=0,
        )
        self.source_label.pack(fill="x")
        self.target_label = tk.Label(
            self.live_frame, text="等待语音…", font=self.draft_font,
            fg=self._draft_color, bg=self.palette.WINDOW, wraplength=wrap, justify="left", anchor="w",
            bd=0, padx=0, pady=int(3 * self.k),
        )
        self.target_label.pack(fill="x")
        self.waiting_hint = tk.Label(
            body, text="播放会议声音后，英文原文与中文翻译会显示在这里。",
            font=self.small_font, fg=self.palette.TEXT_MUTED, bg=self.palette.WINDOW, anchor="w", bd=0,
        )
        self.waiting_hint.pack(fill="x", pady=(int(5 * self.k), 0))
        # Anything that silently degrades the meeting says so here: a refiner
        # that never loaded, or recognition that has stopped coming back. Both
        # used to be a log line in a process whose output goes nowhere, so the
        # only symptom was subtitles that quietly stopped improving.
        self.notice_label = tk.Label(
            body, text="", font=self.small_font, fg=self.palette.WARNING,
            bg=self.palette.WINDOW, wraplength=wrap, justify="left", anchor="w", bd=0,
        )

        for widget in (body, self.final_frame, self.final_label, self.live_frame,
                       self.source_label, self.target_label, self.waiting_hint,
                       self.notice_label):
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
        self._rounded = RoundedWindow(self.root, radius=int(10 * self.k))
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
        bar = tk.Frame(self.root, bg=self.palette.HEADERBAR)
        bar.pack(fill="x", side="top", padx=int(12 * k), pady=int(3 * k))
        self._make_draggable(bar)
        identity = tk.Frame(bar, bg=self.palette.HEADERBAR)
        identity.pack(side="left", padx=(int(8 * k), int(8 * k)))
        self._make_draggable(identity)
        self.status_label = tk.Label(
            identity, text="连接中", font=self.small_font,
            fg=self.palette.WARNING, bg=self.palette.HEADERBAR, bd=0,
        )
        self.status_label.pack(side="left")
        self.timer_label = tk.Label(
            identity, text="00:00", font=self.small_font,
            fg=self.palette.TEXT_MUTED, bg=self.palette.HEADERBAR, bd=0,
        )
        self.timer_label.pack(side="left", padx=(int(12 * k), 0))
        for widget in (self.status_label, self.timer_label):
            self._make_draggable(widget)

        controls = tk.Frame(bar, bg=self.palette.HEADERBAR)
        controls.pack(side="right")

        def button(label: str, command) -> tk.Button:
            btn = tk.Button(
                controls, text=label, font=self.small_font, command=command,
                fg=self.palette.TEXT_DIM, bg=self.palette.HEADERBAR, activebackground=self.palette.HOVER,
                activeforeground=self.palette.TEXT, relief="flat", bd=0,
                padx=int(8 * k), pady=int(4 * k), cursor="hand2",
                highlightthickness=1, highlightbackground=self.palette.HEADERBAR,
                highlightcolor=self.palette.ACCENT,
            )
            btn.pack(side="left", padx=int(2 * k))
            btn.bind("<Enter>", lambda _e: btn.configure(bg=self.palette.HOVER))
            btn.bind("<Leave>", lambda _e: btn.configure(bg=self.palette.HEADERBAR))
            return btn

        button("转录记录", self.show_history)
        self.source_button = button("原文：开", self._toggle_source)
        self.pause_button = button("暂停显示", self._toggle_pause)
        self.display_button = button("显示设置", self._show_display_menu)
        divider = tk.Frame(controls, bg=self.palette.SEPARATOR, width=1, height=int(18 * k))
        divider.pack(side="left", padx=int(10 * k))
        button("结束并保存", self.close)

        # A second row preserves all actions on compact displays without
        # changing the user's requested subtitle width.
        self.root.update_idletasks()
        if bar.winfo_reqwidth() + int(24 * k) > self.width:
            identity.pack_forget()
            controls.pack_forget()
            identity.pack(anchor="w", pady=(0, int(6 * k)))
            controls.pack(anchor="e")
        tk.Frame(self.root, bg=self.palette.SEPARATOR, height=1).pack(fill="x")

    def _show_display_menu(self) -> None:
        if self._display_menu is not None:
            self._display_menu.destroy()
        menu = tk.Menu(
            self.root, tearoff=False, bg=self.palette.WINDOW, fg=self.palette.TEXT,
            activebackground=self.palette.HOVER, activeforeground=self.palette.TEXT,
            disabledforeground=self.palette.TEXT_MUTED, bd=1, relief="solid",
            font=self.small_font,
        )
        self._display_menu = menu
        menu.add_command(label=f"字号  {self.font_size}", state="disabled")
        menu.add_command(label="放大字号    ＋", command=lambda: self._resize_font(2))
        menu.add_command(label="缩小字号    −", command=lambda: self._resize_font(-2))
        menu.add_separator()
        menu.add_command(label=f"不透明度  {self.opacity:.0%}", state="disabled")
        menu.add_command(label="更不透明    ＋", command=lambda: self._adjust_opacity(0.06))
        menu.add_command(label="更透明        −", command=lambda: self._adjust_opacity(-0.06))
        try:
            menu.tk_popup(self.display_button.winfo_rootx(),
                          self.display_button.winfo_rooty()
                          + self.display_button.winfo_height())
        finally:
            menu.grab_release()

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
        self._remember_appearance()

    def _remember_appearance(self) -> None:
        """Persist font size and opacity, coalescing a run of clicks.

        Adjusting either is a burst of ``＋`` presses, and every press would
        otherwise rewrite the settings file. The delay also outlives the menu,
        which is torn down and rebuilt on each press.
        """
        if self._appearance_job is not None:
            try:
                self.root.after_cancel(self._appearance_job)
            except tk.TclError:
                pass
        self._appearance_job = self.root.after(600, self._write_appearance)

    def _write_appearance(self) -> None:
        """Merge the current appearance into whatever is on disk right now.

        Re-read rather than hold an instance from construction: the launcher is
        still alive behind this window with its own copy of the settings, and
        writing a snapshot taken minutes ago would undo whatever it saved in
        between.
        """
        self._appearance_job = None
        settings = Settings()
        settings["font_size"] = self.font_size
        settings["opacity"] = int(round(self.opacity * 100))
        settings.save()

    def _recompute_budgets(self) -> None:
        self._caption_lines = [MAX_LINES_FINAL, MAX_LINES_SOURCE, MAX_LINES_DRAFT]
        fonts = (self.target_font, self.source_font, self.draft_font)
        line_heights = [font.metrics("linespace") for font in fonts]
        available = self.root.winfo_screenheight() * 0.5 - int(140 * self.k)
        # At large font sizes, fewer complete rows are more useful than a
        # tall label whose bottom half is hidden by the screen-height guard.
        for index in (1, 2, 0):
            if sum(rows * height for rows, height in zip(self._caption_lines, line_heights, strict=True)) <= available:
                break
            self._caption_lines[index] = 1

    def _clip_caption(self, value: str, font: tkfont.Font, rows: int) -> str:
        width = max(40, self.width - 2 * self._pad_x - self._text_inset)

        def fits(candidate: str) -> bool:
            count, used = 1, 0
            for word in re.findall(r"\n|[^\S\n]+|[^\s]+", candidate):
                if word == "\n":
                    count, used = count + 1, 0
                else:
                    measured = font.measure(word)
                    if measured <= width:
                        if used and used + measured > width:
                            count, used = count + 1, 0
                        used += measured
                    else:
                        for char in word:
                            measured_char = font.measure(char)
                            if used and used + measured_char > width:
                                count, used = count + 1, 0
                            used += measured_char
                if count > rows:
                    return False
            return True

        value = value.strip()
        if fits(value):
            return value
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if fits("… " + value[-middle:]):
                low = middle
            else:
                high = middle - 1
        return _tail(value, max(1, low))

    def _resize_font(self, delta: int) -> None:
        self.font_size = max(11, min(46, self.font_size + delta))
        self.source_font.configure(size=max(11, self.font_size - 3))
        self.target_font.configure(size=self.font_size)
        self.draft_font.configure(size=self.font_size)
        self._recompute_budgets()
        self._final_text = ""
        if self._last_snapshot is not None:
            self._render(self._last_snapshot)
        else:
            self._fit_height()
        self._remember_appearance()

    def _toggle_source(self) -> None:
        self.show_source = not self.show_source
        if self.show_source:
            self.source_label.pack(fill="x", before=self.target_label)
            self.source_button.configure(text="原文：开", fg=self.palette.TEXT_DIM)
        else:
            self.source_label.pack_forget()
            self.source_button.configure(text="原文：关", fg=self.palette.TEXT_MUTED)
        self._fit_height()

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text="继续显示" if self.paused else "暂停显示",
                                    fg=self.palette.WARNING if self.paused else self.palette.TEXT_DIM)
        if not self.paused and self._paused_snapshot is not None:
            self._render(self._paused_snapshot)
            self._paused_snapshot = None
            self._fill_history()

    def set_status(self, text: str) -> None:
        """Called from the network thread; a plain attribute write is enough
        because ``_tick`` reads it on the Tk thread."""
        self.status_text = text

    def set_notice(self, text: str, key: str = "general") -> None:
        """Show (or with "" clear) a warning line under the subtitles.

        Keyed because the two things that warn are independent: a refiner that
        never loaded stays true for the whole meeting, while a stall comes and
        goes. Sharing one slot meant whichever spoke last erased the other.

        Called from the network thread, so the Tk side only reads a plain
        string that this assembles under the lock -- ``_tick`` does the widget
        work on the Tk thread.
        """
        with self._notice_lock:
            if text:
                self._notices[key] = text
            else:
                self._notices.pop(key, None)
            self._notice = "\n".join(self._notices.values())

    def _apply_notice(self) -> None:
        if self._notice == self._notice_shown:
            return
        self._notice_shown = self._notice
        if self._notice:
            self.notice_label.configure(text=self._notice)
            if not self.notice_label.winfo_manager():
                self.notice_label.pack(fill="x", pady=(int(6 * self.k), 0))
        else:
            self.notice_label.pack_forget()
        self._fit_height()

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
        if latest is not None and self.paused:
            self._paused_snapshot = latest
        elif latest is not None:
            self._render(latest)
            if self._history_window is not None and self._history_window.winfo_exists() \
                    and self._history_window.state() != "withdrawn":
                self._fill_history()
        self.root.after(100, self._drain)

    def _render(self, snapshot: Snapshot) -> None:
        self._last_snapshot = snapshot
        speech = snapshot.speech_lines
        self.history = speech
        if not speech:
            self._final_text = ""
            self.final_frame.pack_forget()
            self.divider.pack_forget()
            self.source_label.configure(text=self._clip_caption(
                snapshot.buffer_transcription, self.source_font, self._caption_lines[1]))
            self.target_label.configure(text=self._clip_caption(
                snapshot.buffer_translation, self.draft_font, self._caption_lines[2])
                or "等待语音…")
            if snapshot.buffer_transcription or snapshot.buffer_translation:
                self.waiting_hint.pack_forget()
            elif not self.waiting_hint.winfo_manager():
                self.waiting_hint.pack(fill="x", pady=(int(5 * self.k), 0))
            self._fit_height()
            return
        self.waiting_hint.pack_forget()

        # The pinned row holds the newest sentence that has been re-translated
        # in full. It is deliberately *not* cleared when the speaker moves on:
        # the polished Chinese has to stay put long enough to read.
        newest_final = None
        for line in reversed(speech):
            if line.refined and line.translation.strip():
                newest_final = line
                break
        if newest_final is not None:
            self._final_text = newest_final.text
            self.final_label.configure(
                text=self._clip_caption(newest_final.translation, self.target_font,
                                        self._caption_lines[0]))
            if not self.final_frame.winfo_manager():
                self.final_frame.pack(fill="x", before=self.live_frame)
                self.divider.pack(fill="x", pady=int(10 * self.k),
                                  before=self.live_frame)
        else:
            self._final_text = ""
            self.final_frame.pack_forget()
            self.divider.pack_forget()

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

        self.source_label.configure(
            text=self._clip_caption(source, self.source_font, self._caption_lines[1]))
        self.target_label.configure(
            text=self._clip_caption(draft, self.draft_font, self._caption_lines[2]) if draft else "",
            fg=self._draft_color,
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
        window.title("转录记录 · 会议字幕")
        window.configure(bg=self.palette.WINDOW)
        window.attributes("-topmost", True)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = int(min(max(700 * self.k, self.width * 0.65), screen_w * 0.78))
        height = int(screen_h * 0.68)
        window.geometry(f"{width}x{height}+{(screen_w - width) // 2}"
                        f"+{max(int(36 * self.k), (screen_h - height) // 2)}")
        window.minsize(min(width, int(540 * self.k)), int(300 * self.k))

        bar = tk.Frame(window, bg=self.palette.HEADERBAR, height=int(44 * self.k))
        bar.pack(fill="x", padx=int(24 * self.k))
        bar.pack_propagate(False)
        title = tk.Label(bar, text="转录记录", font=self.heading_font,
                         fg=self.palette.TEXT, bg=self.palette.HEADERBAR, bd=0, anchor="w")
        title.pack(side="left")

        def start_drag(event):
            self._hist_drag = (event.x_root, event.y_root,
                               window.winfo_x(), window.winfo_y())

        def do_drag(event):
            ox, oy, wx, wy = self._hist_drag
            window.geometry(f"+{wx + event.x_root - ox}+{wy + event.y_root - oy}")

        for widget in (bar, title):
            widget.bind("<Button-1>", start_drag)
            widget.bind("<B1-Motion>", do_drag)

        close = tk.Button(
            bar, text="关闭", command=window.withdraw, font=self.small_font,
            fg=self.palette.TEXT_DIM, bg=self.palette.HEADERBAR, activebackground=self.palette.HOVER,
            activeforeground=self.palette.TEXT, cursor="hand2", relief="flat", bd=0,
            highlightthickness=1, highlightbackground=self.palette.HEADERBAR,
            highlightcolor=self.palette.ACCENT, padx=int(10 * self.k), pady=int(3 * self.k),
        )
        close.pack(side="right")
        close.bind("<Enter>", lambda _e: close.configure(bg=self.palette.HOVER))
        close.bind("<Leave>", lambda _e: close.configure(bg=self.palette.HEADERBAR))
        tk.Frame(window, bg=self.palette.SEPARATOR, height=1).pack(fill="x")

        meta = tk.Frame(window, bg=self.palette.WINDOW)
        meta.pack(fill="x")
        self._history_count = tk.Label(
            meta, text="暂无转录 · 选中文字可复制", font=self.caption_font,
            fg=self.palette.TEXT_DIM, bg=self.palette.WINDOW, bd=0,
        )
        self._history_count.pack(side="left", padx=int(24 * self.k), pady=int(8 * self.k))
        self._status_pill = tk.Label(
            meta, text="跟随最新", font=self.caption_font,
            fg=self.palette.TEXT_DIM, bg=self.palette.WINDOW, bd=0,
        )
        self._status_pill.pack(side="right", padx=int(24 * self.k))

        frame = tk.Frame(window, bg=self.palette.WINDOW)
        frame.pack(fill="both", expand=True, pady=(0, int(14 * self.k)))
        scrollbar = tk.Scrollbar(
            frame, bg=self.palette.SURFACE, troughcolor=self.palette.WINDOW,
            activebackground=self.palette.HOVER,
            bd=0, highlightthickness=0, relief="flat", elementborderwidth=0,
            width=int(10 * self.k),
        )
        scrollbar.pack(side="right", fill="y", padx=(0, int(5 * self.k)),
                       pady=int(12 * self.k))
        text = tk.Text(
            frame, bg=self.palette.WINDOW, fg=self.palette.TEXT_DIM, font=self.history_font, wrap="word",
            bd=0, highlightthickness=0, padx=int(24 * self.k),
            pady=int(8 * self.k), selectbackground=self.palette.ACCENT,
            selectforeground=self.palette.ACCENT_TEXT, inactiveselectbackground=self.palette.HOVER,
            insertbackground=self.palette.ACCENT, state="disabled",
        )
        text.pack(side="left", fill="both", expand=True)

        self._history_empty = tk.Frame(frame, bg=self.palette.WINDOW)
        tk.Label(
            self._history_empty, text="还没有转录内容", font=self.heading_font,
            fg=self.palette.TEXT, bg=self.palette.WINDOW, bd=0,
        ).pack()
        tk.Label(
            self._history_empty, text="听到语音后，英文原文和中文翻译会自动出现在这里。",
            font=self.small_font, fg=self.palette.TEXT_DIM, bg=self.palette.WINDOW, bd=0,
            wraplength=width - int(100 * self.k),
        ).pack(pady=(int(10 * self.k), 0))
        self._history_empty.place(relx=0.5, rely=0.42, anchor="center")

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

        text.tag_configure("time", foreground=self.palette.TEXT_DIM, font=self.caption_font,
                           spacing1=int(18 * self.k), spacing3=int(4 * self.k))
        text.tag_configure("en", foreground=self.palette.TEXT_DIM, spacing3=int(4 * self.k))
        text.tag_configure("zh_final", foreground=self.palette.TEXT, font=self.history_target_font)
        text.tag_configure("zh_draft", foreground=self.palette.TEXT, font=self.history_target_font)

        self._jump_button = tk.Button(
            window, text="回到最新", font=self.small_font, command=self._jump_to_latest,
            fg=self.palette.ACCENT_TEXT, bg=self.palette.ACCENT,
            activebackground=self.palette.ACCENT_HOVER,
            activeforeground=self.palette.ACCENT_TEXT, relief="flat", bd=0,
            padx=int(16 * self.k), pady=int(7 * self.k),
            highlightthickness=1, highlightbackground=self.palette.ACCENT,
            highlightcolor=self.palette.TEXT, cursor="hand2")

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
            label = f"正在回看 · {self._pending} 段新内容" if self._pending else "正在回看"
            self._status_pill.configure(text=label, fg=self.palette.TEXT_DIM)
            self._jump_button.place(relx=0.5, rely=1.0, anchor="s",
                                    y=-int(18 * self.k))
            # A placed widget does not automatically sit above its packed
            # siblings; without this the button is mapped but never painted.
            self._jump_button.lift()
        else:
            self._status_pill.configure(text="跟随最新", fg=self.palette.TEXT_DIM)
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
        if self._history_count is not None:
            count = f"{len(signatures)} 段转录" if signatures else "暂无转录"
            self._history_count.configure(
                text=f"{count} · 选中文字可复制")
        if self._history_empty is not None:
            if signatures:
                self._history_empty.place_forget()
            else:
                self._history_empty.place(relx=0.5, rely=0.42, anchor="center")
        if signatures == self._rendered:
            return

        if self._frozen:
            self._pending = max(0, len(signatures) - len(self._rendered))
            self._set_history_state(True)
            return

        # Refinement can rewrite a line that is already on screen, so redraw
        # from the first block that actually differs rather than assuming only
        # the tail changed.
        # A snapshot can also retract its tail; keeping the old block count
        # would leave removed sentences visible in an otherwise current view.
        first_changed = min(len(self._rendered), len(signatures))
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
            state = "已润色" if line.refined else "实时转录"
            text.insert("end", f"{line.start}  ·  {state}\n", "time")
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
        wanted = min(wanted, self.root.winfo_screenheight() - int(24 * self.k))
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
        colour = {"connected": self.palette.SUCCESS,
                  "finished": self.palette.TEXT_MUTED}.get(self.status_text, self.palette.WARNING)
        label = {"connected": "录制中", "finished": "已结束"}.get(self.status_text, self.status_text)
        self.status_label.configure(text=label, fg=colour)
        self._apply_notice()
        self.root.after(1000, self._tick)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._appearance_job is not None:
            # Closing within the coalescing delay must not lose the adjustment.
            try:
                self.root.after_cancel(self._appearance_job)
            except tk.TclError:
                pass
            self._write_appearance()
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
