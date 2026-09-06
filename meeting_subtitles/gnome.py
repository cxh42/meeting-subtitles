"""GNOME / Yaru-styled Tk widgets.

The palette is sampled from Ubuntu 24.04's own Settings window in dark mode, so
the launcher sits next to it without looking like a different toolkit. The
structural idea worth copying is the *boxed list*: a rounded card holding rows
that are a label on the left and one control on the right, separated by hairlines.
Almost every page of GNOME Settings is built from it, and it is what makes a
window read as designed rather than as widgets stacked in a column.

Tk cannot round a Frame, so each card is a Canvas that draws the rounded
rectangle and carries its rows as embedded windows. Embedded windows always
paint above canvas items, so separators are thin Frames rather than canvas
lines.
"""

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

# --- Yaru dark, sampled from gnome-control-center -------------------------
HEADERBAR = "#222222"
WINDOW = "#2c2c2c"
CARD = "#272727"
HOVER = "#3c3c3c"
SEPARATOR = "#353535"
ACCENT = "#e95420"
ACCENT_HOVER = "#f06d42"
ACCENT_TEXT = "#ffffff"
TEXT = "#ffffff"
TEXT_DIM = "#9a9a9a"
TEXT_MUTED = "#787878"
FIELD = "#1f1f1f"
FIELD_FOCUS = "#e95420"
SUCCESS = "#57e389"
WARNING = "#f8c76b"
ERROR = "#ff7b63"

#: Ubuntu's UI font has no CJK coverage; GNOME itself falls back to Noto CJK for
#: Chinese, so using it directly matches what Settings actually renders. The
#: list is long because the packaging of Noto CJK has changed across Ubuntu
#: releases -- the family is variously "Noto Sans CJK SC", "Noto Sans CJK JP"
#: (which also covers Chinese) or "Noto Sans SC" -- and a release that ships
#: none of them should still fall back to something rather than to tofu.
UI_FONTS = (
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
    "Noto Sans CJK JP", "WenQuanYi Zen Hei", "Ubuntu Sans", "Ubuntu",
    "DejaVu Sans",
)


def pick_font(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for family in UI_FONTS:
        if family in available:
            return family
    return "TkDefaultFont"


def screen_scale(root: tk.Misc) -> float:
    """Pixels per logical unit relative to a nominal 96 DPI display."""
    try:
        return max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except tk.TclError:
        return 1.0


class Pill:
    """A true capsule drawn as two circles plus a rectangle.

    ``create_polygon(smooth=True)`` approximates corners with Beziers, which is
    fine for a 12 px card radius but visibly wrong when the radius is half the
    height: the ends of a switch come out as flattened lozenges rather than
    semicircles. Composing real ovals is the only way to get the shape right.
    """

    def __init__(self, canvas: tk.Canvas, x0: float, y0: float,
                 x1: float, y1: float, fill: str) -> None:
        self.canvas = canvas
        r = (y1 - y0) / 2
        self.left = canvas.create_oval(x0, y0, x0 + 2 * r, y1, fill=fill, outline="")
        self.right = canvas.create_oval(x1 - 2 * r, y0, x1, y1, fill=fill, outline="")
        self.middle = canvas.create_rectangle(x0 + r, y0, x1 - r, y1,
                                              fill=fill, outline="")

    @property
    def items(self):
        return (self.left, self.right, self.middle)

    def configure(self, **kwargs) -> None:
        for item in self.items:
            self.canvas.itemconfigure(item, **kwargs)

    def resize(self, x0: float, y0: float, x1: float, y1: float) -> None:
        r = (y1 - y0) / 2
        x1 = max(x1, x0 + 2 * r)
        self.canvas.coords(self.left, x0, y0, x0 + 2 * r, y1)
        self.canvas.coords(self.right, x1 - 2 * r, y0, x1, y1)
        self.canvas.coords(self.middle, x0 + r, y0, x1 - r, y1)


def rounded_points(x0: float, y0: float, x1: float, y1: float, r: float):
    """Polygon points that ``create_polygon(smooth=True)`` renders as a
    rounded rectangle -- cheaper and cleaner than compositing arcs."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1,
        x0 + r, y1, x0, y1, x0, y1 - r,
        x0, y0 + r, x0, y0,
    ]


class Switch(tk.Canvas):
    """Yaru toggle: an accent-filled track with a white knob."""

    def __init__(
        self,
        master: tk.Misc,
        value: bool = True,
        *,
        scale: float = 1.0,
        parent_bg: str = CARD,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._cw = int(46 * scale)
        self._ch = int(26 * scale)
        # One extra pixel each way: a canvas exactly as wide as the pill clips
        # the last column of the right-hand circle, which reads as a flat edge.
        super().__init__(master, width=self._cw + 2, height=self._ch + 2,
                         bg=parent_bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.value = value
        self.on_change = on_change
        self._pad = max(2.0, 2.5 * scale)
        self._track = Pill(self, 1, 1, self._cw + 1, self._ch + 1,
                           ACCENT if value else "#4a4a4a")
        self._knob = self.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
        self._draw()
        self.bind("<Button-1>", self._clicked)

    def _draw(self) -> None:
        r = self._ch / 2 - self._pad
        cy = self._ch / 2 + 1
        cx = 1 + ((self._cw - self._ch / 2) if self.value else (self._ch / 2))
        self.coords(self._knob, cx - r, cy - r, cx + r, cy + r)
        self._track.configure(fill=ACCENT if self.value else "#4a4a4a")

    def _clicked(self, _event=None) -> None:
        self.set(not self.value)
        if self.on_change:
            self.on_change(self.value)

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self._draw()


class Entry(tk.Frame):
    """Flat GNOME text field with a placeholder and an accent focus ring."""

    def __init__(
        self,
        master: tk.Misc,
        placeholder: str = "",
        *,
        width: int = 230,
        scale: float = 1.0,
        font: tuple | None = None,
        parent_bg: str = CARD,
    ) -> None:
        super().__init__(master, bg=parent_bg)
        w, h = int(width * scale), int(34 * scale)
        radius = int(8 * scale)
        self._pad = int(10 * scale)
        self.canvas = tk.Canvas(self, width=w, height=h, bg=parent_bg,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._shape = self.canvas.create_polygon(
            rounded_points(1, 1, w - 1, h - 1, radius),
            smooth=True, fill=FIELD, outline="#3a3a3a", width=1,
        )
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self.canvas, textvariable=self.var, bd=0, relief="flat", bg=FIELD,
            fg=TEXT, insertbackground=TEXT, highlightthickness=0,
            font=font or ("TkDefaultFont", 10),
        )
        self.canvas.create_window(self._pad, h / 2, window=self.entry,
                                  anchor="w", width=w - 2 * self._pad)
        self._placeholder = placeholder
        self._showing = False
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not self.var.get() and self._placeholder:
            self._showing = True
            self.var.set(self._placeholder)
            self.entry.configure(fg=TEXT_MUTED)

    def _focus_in(self, _e=None) -> None:
        self.canvas.itemconfigure(self._shape, outline=FIELD_FOCUS, width=2)
        if self._showing:
            self._showing = False
            self.var.set("")
            self.entry.configure(fg=TEXT)

    def _focus_out(self, _e=None) -> None:
        self.canvas.itemconfigure(self._shape, outline="#3a3a3a", width=1)
        self._show_placeholder()

    def get(self) -> str:
        return "" if self._showing else self.var.get().strip()

    def set(self, value: str) -> None:
        if value:
            self._showing = False
            self.var.set(value)
            self.entry.configure(fg=TEXT)
        else:
            self.var.set("")
            self._show_placeholder()


class Slider(tk.Canvas):
    """Accent-filled track with a white knob and a value readout."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        minimum: int = 14,
        maximum: int = 34,
        value: int = 20,
        width: int = 200,
        scale: float = 1.0,
        font: tuple | None = None,
        parent_bg: str = CARD,
        on_change: Callable[[int], None] | None = None,
        suffix: str = "",
    ) -> None:
        w, h = int(width * scale), int(30 * scale)
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.minimum, self.maximum = minimum, maximum
        self.value = max(minimum, min(maximum, value))
        self.on_change = on_change
        self.suffix = suffix
        self._r = int(9 * scale)
        # A "%" needs a wider readout than a bare number, and a readout that
        # overflows its box would sit on top of the track.
        self._readout_w = int((42 if suffix else 30) * scale)
        self._x0 = self._r
        self._x1 = w - self._readout_w - self._r
        self._cy = h / 2
        self._th = max(4, int(5 * scale))
        self._track = Pill(self, self._x0, self._cy - self._th / 2,
                           self._x1, self._cy + self._th / 2, "#4a4a4a")
        self._fill = Pill(self, self._x0, self._cy - self._th / 2,
                          self._x0 + self._th, self._cy + self._th / 2, ACCENT)
        self._knob = self.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
        self._readout = self.create_text(
            w - self._readout_w / 2, self._cy, text=f"{self.value}{suffix}",
            fill=TEXT_DIM, font=font or ("TkDefaultFont", 10))
        self._draw()
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)

    def _draw(self) -> None:
        span = max(1, self.maximum - self.minimum)
        cx = self._x0 + (self._x1 - self._x0) * (self.value - self.minimum) / span
        self._fill.resize(self._x0, self._cy - self._th / 2,
                          max(cx, self._x0 + self._th), self._cy + self._th / 2)
        self.coords(self._knob, cx - self._r, self._cy - self._r,
                    cx + self._r, self._cy + self._r)
        self.itemconfigure(self._readout, text=f"{self.value}{self.suffix}")

    def _drag(self, event) -> None:
        span = self._x1 - self._x0
        ratio = 0.0 if span <= 0 else max(0.0, min(1.0, (event.x - self._x0) / span))
        new = round(self.minimum + ratio * (self.maximum - self.minimum))
        if new != self.value:
            self.value = new
            self._draw()
            if self.on_change:
                self.on_change(new)

    def get(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = max(self.minimum, min(self.maximum, int(value)))
        self._draw()


class IndeterminateBar(tk.Canvas):
    """A sliding accent segment, for work whose duration cannot be known.

    Loading the models is dominated by reading several gigabytes off disk, and
    nothing upstream reports how far along that is. A determinate bar would
    have to invent a number; this says only "still working", which is the
    honest claim and the one the user actually needs.

    Sized to be invisible when stopped: it keeps its space in the layout so
    starting and stopping it never reflows the window.
    """

    def __init__(self, master: tk.Misc, *, width: int = 340,
                 scale: float = 1.0, parent_bg: str = WINDOW) -> None:
        # Not _w / _h: tk.Misc already uses those for the Tcl widget path,
        # and shadowing them makes every later geometry call fail with a
        # "bad screen distance" that names no cause.
        self._bw = int(width * scale)
        self._bh = max(3, int(4 * scale))
        super().__init__(master, width=self._bw, height=self._bh, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._parent_bg = parent_bg
        self._seg = max(int(self._bw * 0.28), 1)
        self._track = Pill(self, 0, 0, self._bw, self._bh, parent_bg)
        self._chip = Pill(self, 0, 0, self._seg, self._bh, parent_bg)
        self._pos = 0.0
        self._dir = 1.0
        self._job: str | None = None

    def start(self) -> None:
        if self._job is not None:
            return
        self._track.configure(fill=SEPARATOR)
        self._chip.configure(fill=ACCENT)
        self._step()

    def stop(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        # Repainting in the parent's colour leaves the widget occupying its
        # space without drawing anything the user can see.
        self._track.configure(fill=self._parent_bg)
        self._chip.configure(fill=self._parent_bg)

    def _step(self) -> None:
        travel = max(self._bw - self._seg, 1)
        self._pos += self._dir * travel / 34.0
        if self._pos <= 0:
            self._pos, self._dir = 0.0, 1.0
        elif self._pos >= travel:
            self._pos, self._dir = float(travel), -1.0
        self._chip.resize(self._pos, 0, self._pos + self._seg, self._bh)
        self._job = self.after(40, self._step)


class Button(tk.Canvas):
    """Rounded button. ``accent=True`` gives GNOME's suggested-action style."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int = 160,
        height: int = 38,
        scale: float = 1.0,
        accent: bool = False,
        parent_bg: str = WINDOW,
        font: tuple | None = None,
    ) -> None:
        w, h = int(width * scale), int(height * scale)
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._bg = ACCENT if accent else "#383838"
        self._hover = ACCENT_HOVER if accent else "#454545"
        self._fg = ACCENT_TEXT if accent else TEXT
        self._enabled = True
        self._shape = self.create_polygon(
            rounded_points(0, 0, w, h, int(9 * scale)),
            smooth=True, fill=self._bg, outline="")
        self._label = self.create_text(w / 2, h / 2, text=text, fill=self._fg,
                                       font=font or ("TkDefaultFont", 10, "bold"))
        self.bind("<Enter>", lambda e: self._enabled and
                  self.itemconfigure(self._shape, fill=self._hover))
        self.bind("<Leave>", lambda e: self._enabled and
                  self.itemconfigure(self._shape, fill=self._bg))
        self.bind("<Button-1>", lambda e: self._enabled and self.command())

    def set_text(self, text: str) -> None:
        self.itemconfigure(self._label, text=text)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.itemconfigure(self._shape, fill=self._bg if enabled else "#303030")
        self.itemconfigure(self._label, fill=self._fg if enabled else TEXT_MUTED)
        self.configure(cursor="hand2" if enabled else "")


class BoxedList(tk.Canvas):
    """GNOME's rounded card of label-plus-control rows.

    Rows are embedded Frames, which Tk always paints above canvas items, so the
    hairlines between them are thin Frames too rather than canvas lines.
    """

    def __init__(self, master: tk.Misc, *, width: int, scale: float = 1.0,
                 parent_bg: str = WINDOW) -> None:
        self._cw = int(width * scale)
        self._scale = scale
        self._radius = int(12 * scale)
        super().__init__(master, width=self._cw, height=1, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._card = self.create_polygon([0, 0, 0, 0], smooth=True,
                                         fill=CARD, outline="")
        self._y = 0
        self._rows: list[tk.Frame] = []

    def add_row(self, height: int = 52) -> tk.Frame:
        h = int(height * self._scale)
        if self._rows:
            line = tk.Frame(self, bg=SEPARATOR, height=1)
            self.create_window(int(14 * self._scale), self._y, window=line,
                               anchor="nw", width=self._cw - int(28 * self._scale),
                               height=1)
            self._y += 1
        row = tk.Frame(self, bg=CARD)
        self.create_window(0, self._y, window=row, anchor="nw",
                           width=self._cw, height=h)
        self._y += h
        self._rows.append(row)
        return row

    def render(self) -> None:
        self.configure(height=self._y)
        self.coords(self._card,
                    *rounded_points(0, 0, self._cw, self._y, self._radius))
