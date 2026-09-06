"""Shared desktop surfaces, typography and keyboard-friendly Tk controls.

Canvas-backed controls keep the same appearance across Linux themes. Embedded
row frames are inset because Tk always paints child windows above canvas items,
including the rounded corners of the surface underneath them.
"""

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    HEADERBAR: str
    WINDOW: str
    CARD: str
    SURFACE: str
    HOVER: str
    SEPARATOR: str
    TRACK: str
    ACCENT: str
    ACCENT_HOVER: str
    ACCENT_PRESSED: str
    ACCENT_TEXT: str
    TEXT: str
    TEXT_DIM: str
    TEXT_MUTED: str
    FIELD: str
    FIELD_FOCUS: str
    SUCCESS: str
    WARNING: str
    ERROR: str
    THUMB: str


LIGHT = Palette(
    HEADERBAR="#f5f5f4", WINDOW="#f5f5f4", CARD="#ffffff",
    SURFACE="#eaeae8", HOVER="#e3e5e7", SEPARATOR="#dcdedc", TRACK="#c6cccf",
    ACCENT="#345f84", ACCENT_HOVER="#284e6e", ACCENT_PRESSED="#20435f",
    ACCENT_TEXT="#ffffff", TEXT="#252a2c", TEXT_DIM="#5d6468",
    TEXT_MUTED="#697176", FIELD="#ffffff", FIELD_FOCUS="#345f84",
    SUCCESS="#35704a", WARNING="#886219", ERROR="#a73535", THUMB="#ffffff",
)
DARK = Palette(
    HEADERBAR="#232628", WINDOW="#232628", CARD="#2d3235",
    SURFACE="#353b3f", HOVER="#3e474d", SEPARATOR="#424a4f", TRACK="#606c73",
    ACCENT="#91b8d6", ACCENT_HOVER="#abcce4", ACCENT_PRESSED="#c1ddf0",
    ACCENT_TEXT="#1c2b36", TEXT="#f4f4f1", TEXT_DIM="#b5bdc2",
    TEXT_MUTED="#a7b1b7", FIELD="#2d3235", FIELD_FOCUS="#91b8d6",
    SUCCESS="#95c5a3", WARNING="#e1be73", ERROR="#f0a2a2", THUMB="#f4f4f1",
)


def get_palette(name: str) -> Palette:
    return DARK if name == "dark" else LIGHT


# Existing callers retain a stable light palette until they opt into theming.
HEADERBAR = LIGHT.HEADERBAR
WINDOW = LIGHT.WINDOW
CARD = LIGHT.CARD
SURFACE = LIGHT.SURFACE
HOVER = LIGHT.HOVER
SEPARATOR = LIGHT.SEPARATOR
TRACK = LIGHT.TRACK
ACCENT = LIGHT.ACCENT
ACCENT_HOVER = LIGHT.ACCENT_HOVER
ACCENT_PRESSED = LIGHT.ACCENT_PRESSED
ACCENT_TEXT = LIGHT.ACCENT_TEXT
TEXT = LIGHT.TEXT
TEXT_DIM = LIGHT.TEXT_DIM
TEXT_MUTED = LIGHT.TEXT_MUTED
FIELD = LIGHT.FIELD
FIELD_FOCUS = LIGHT.FIELD_FOCUS
SUCCESS = LIGHT.SUCCESS
WARNING = LIGHT.WARNING
ERROR = LIGHT.ERROR

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
    """Capsule toggle with pointer and keyboard activation."""

    def __init__(
        self,
        master: tk.Misc,
        value: bool = True,
        *,
        scale: float = 1.0,
        parent_bg: str | None = None,
        palette: Palette = LIGHT,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.palette = palette
        parent_bg = palette.CARD if parent_bg is None else parent_bg
        self._cw = int(42 * scale)
        self._ch = int(24 * scale)
        self._margin = max(4, int(4 * scale))
        super().__init__(master, width=self._cw + 2 * self._margin,
                         height=self._ch + 2 * self._margin,
                         bg=parent_bg, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=True)
        self.value = bool(value)
        self.on_change = on_change
        self._parent_bg = parent_bg
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self._focused = False
        self._pad = max(2.0, 2.5 * scale)
        m = self._margin
        self._focus_ring = Pill(self, 1, 1, self._cw + 2 * m - 1,
                               self._ch + 2 * m - 1, parent_bg)
        self._focus_gap = Pill(self, 3, 3, self._cw + 2 * m - 3,
                              self._ch + 2 * m - 3, parent_bg)
        self._track = Pill(self, m, m, self._cw + m, self._ch + m, palette.TRACK)
        self._knob = self.create_oval(0, 0, 0, 0, fill=palette.THUMB, outline="")
        self._draw()
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<FocusIn>", lambda _e: self._focus(True))
        self.bind("<FocusOut>", lambda _e: self._focus(False))
        self.bind("<KeyPress-space>", self._press)
        self.bind("<KeyRelease-space>", self._release)
        self.bind("<Return>", self._clicked)

    def _draw(self) -> None:
        p = self.palette
        r = self._ch / 2 - self._pad
        cy = self._ch / 2 + self._margin
        cx = self._margin + (
            (self._cw - self._ch / 2) if self.value else (self._ch / 2))
        self.coords(self._knob, cx - r, cy - r, cx + r, cy + r)
        if not self._enabled:
            track, knob = p.SEPARATOR, p.THUMB
        elif self.value:
            track = (p.ACCENT_PRESSED if self._pressed else
                     p.ACCENT_HOVER if self._hovered else p.ACCENT)
            knob = p.ACCENT_TEXT
        else:
            track, knob = (p.TEXT_MUTED if self._hovered else p.TRACK), p.THUMB
        self._track.configure(fill=track)
        self.itemconfigure(self._knob, fill=knob)
        self._focus_ring.configure(
            fill=p.ACCENT if self._focused and self._enabled else self._parent_bg)

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _focus(self, focused: bool) -> None:
        self._focused = focused
        if not focused:
            self._pressed = False
        self._draw()

    def _press(self, _event=None) -> str:
        if self._enabled:
            self.focus_set()
            self._pressed = True
            self._draw()
        return "break"

    def _release(self, event=None) -> str:
        pressed, self._pressed = self._pressed, False
        inside = (event is None or event.keysym == "space" or
                  (0 <= event.x < self.winfo_width() and
                   0 <= event.y < self.winfo_height()))
        self._draw()
        if pressed and inside:
            self._clicked()
        return "break"

    def _clicked(self, _event=None) -> str:
        if not self._enabled:
            return "break"
        self.set(not self.value)
        if self.on_change:
            self.on_change(self.value)
        return "break"

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._pressed = False
        self.configure(cursor="hand2" if enabled else "", takefocus=enabled)
        self._draw()


class Entry(tk.Frame):
    """Inset text field with a placeholder and an accent focus ring."""

    def __init__(
        self,
        master: tk.Misc,
        placeholder: str = "",
        *,
        width: int = 230,
        scale: float = 1.0,
        font: tuple | None = None,
        parent_bg: str | None = None,
        palette: Palette = LIGHT,
    ) -> None:
        self.palette = palette
        parent_bg = palette.CARD if parent_bg is None else parent_bg
        super().__init__(master, bg=parent_bg)
        w, h = int(width * scale), int(38 * scale)
        radius = int(6 * scale)
        self._pad = int(10 * scale)
        self.canvas = tk.Canvas(self, width=w, height=h, bg=parent_bg,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._shape = self.canvas.create_polygon(
            rounded_points(1, 1, w - 1, h - 1, radius),
            smooth=True, fill=palette.FIELD, outline=palette.SEPARATOR, width=1,
        )
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self.canvas, textvariable=self.var, bd=0, relief="flat", bg=palette.FIELD,
            fg=palette.TEXT, insertbackground=palette.TEXT, highlightthickness=0,
            selectbackground=palette.ACCENT, selectforeground=palette.ACCENT_TEXT,
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
            self.entry.configure(fg=self.palette.TEXT_MUTED)

    def _focus_in(self, _e=None) -> None:
        self.canvas.itemconfigure(self._shape, outline=self.palette.FIELD_FOCUS, width=2)
        if self._showing:
            self._showing = False
            self.var.set("")
            self.entry.configure(fg=self.palette.TEXT)

    def _focus_out(self, _e=None) -> None:
        self.canvas.itemconfigure(self._shape, outline=self.palette.SEPARATOR, width=1)
        self._show_placeholder()

    def get(self) -> str:
        return "" if self._showing else self.var.get().strip()

    def set(self, value: str) -> None:
        if value:
            self._showing = False
            self.var.set(value)
            self.entry.configure(fg=self.palette.TEXT)
        else:
            self.var.set("")
            self._show_placeholder()


class Slider(tk.Canvas):
    """Value slider supporting arrows, Page Up/Down, Home and End."""

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
        parent_bg: str | None = None,
        palette: Palette = LIGHT,
        on_change: Callable[[int], None] | None = None,
        suffix: str = "",
    ) -> None:
        self.palette = palette
        parent_bg = palette.CARD if parent_bg is None else parent_bg
        w, h = int(width * scale), int(34 * scale)
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.minimum, self.maximum = minimum, maximum
        self.value = max(minimum, min(maximum, value))
        self.on_change = on_change
        self.suffix = suffix
        self._enabled = True
        self._focused = False
        self._hovered = False
        self._pressed = False
        self._r = int(7 * scale)
        self._ring_pad = max(3, int(3 * scale))
        # A "%" needs a wider readout than a bare number, and a readout that
        # overflows its box would sit on top of the track.
        self._readout_w = int((42 if suffix else 30) * scale)
        self._x0 = self._r + self._ring_pad + 1
        self._x1 = max(self._x0 + 1, w - self._readout_w - self._x0)
        self._cy = h / 2
        self._th = max(3, int(3 * scale))
        self._track = Pill(self, self._x0, self._cy - self._th / 2,
                           self._x1, self._cy + self._th / 2, palette.TRACK)
        self._fill = Pill(self, self._x0, self._cy - self._th / 2,
                          self._x0 + self._th, self._cy + self._th / 2, palette.ACCENT)
        self._focus_ring = self.create_oval(0, 0, 0, 0, outline="", width=1.5)
        self._knob = self.create_oval(0, 0, 0, 0, fill=palette.THUMB,
                                      outline=palette.TEXT_MUTED, width=1)
        self._readout = self.create_text(
            w - self._readout_w / 2, self._cy, text=f"{self.value}{suffix}",
            fill=palette.TEXT_DIM, font=font or ("TkDefaultFont", 10))
        self._draw()
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<FocusIn>", lambda _e: self._focus(True))
        self.bind("<FocusOut>", lambda _e: self._focus(False))
        for key in ("Left", "Down", "Right", "Up", "Prior", "Next", "Home", "End"):
            self.bind(f"<{key}>", self._key)

    def _draw(self) -> None:
        p = self.palette
        span = max(1, self.maximum - self.minimum)
        cx = self._x0 + (self._x1 - self._x0) * (self.value - self.minimum) / span
        self._fill.resize(self._x0, self._cy - self._th / 2,
                          max(cx, self._x0 + self._th), self._cy + self._th / 2)
        self.coords(self._knob, cx - self._r, self._cy - self._r,
                    cx + self._r, self._cy + self._r)
        ring_r = self._r + self._ring_pad
        self.coords(self._focus_ring, cx - ring_r, self._cy - ring_r,
                    cx + ring_r, self._cy + ring_r)
        self.itemconfigure(self._focus_ring,
                           outline=p.ACCENT if self._focused and self._enabled else "")
        self.itemconfigure(self._readout, text=f"{self.value}{self.suffix}")
        self.itemconfigure(self._readout, fill=p.TEXT_DIM)
        self._fill.configure(fill=p.ACCENT if self._enabled else p.TRACK)
        self.itemconfigure(self._knob,
                           fill=p.SURFACE if not self._enabled else p.THUMB,
                           outline=(p.TRACK if not self._enabled else
                                    p.ACCENT if self._pressed or self._hovered else p.TEXT_MUTED))

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _focus(self, focused: bool) -> None:
        self._focused = focused
        if not focused:
            self._pressed = False
        self._draw()

    def _press(self, event) -> None:
        if self._enabled:
            self.focus_set()
            self._pressed = True
            self._drag(event)
            self._draw()

    def _release(self, _event=None) -> None:
        self._pressed = False
        self._draw()

    def _key(self, event) -> str:
        if not self._enabled:
            return "break"
        step = max(1, round((self.maximum - self.minimum) / 10))
        changes = {"Left": -1, "Down": -1, "Right": 1, "Up": 1,
                   "Prior": step, "Next": -step}
        new = (self.minimum if event.keysym == "Home" else
               self.maximum if event.keysym == "End" else
               self.value + changes[event.keysym])
        self._change(new)
        return "break"

    def _change(self, value: int) -> None:
        new = max(self.minimum, min(self.maximum, int(value)))
        if new != self.value:
            self.value = new
            self._draw()
            if self.on_change:
                self.on_change(new)

    def _drag(self, event) -> None:
        if not self._enabled:
            return
        span = self._x1 - self._x0
        ratio = 0.0 if span <= 0 else max(0.0, min(1.0, (event.x - self._x0) / span))
        self._change(round(self.minimum + ratio * (self.maximum - self.minimum)))

    def get(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = max(self.minimum, min(self.maximum, int(value)))
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._pressed = False
        self.configure(cursor="hand2" if enabled else "", takefocus=enabled)
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
                 scale: float = 1.0, parent_bg: str | None = None,
                 palette: Palette = LIGHT) -> None:
        self.palette = palette
        parent_bg = palette.WINDOW if parent_bg is None else parent_bg
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
        self._track.configure(fill=self.palette.SEPARATOR)
        self._chip.configure(fill=self.palette.ACCENT)
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
    """Rounded action with a visible focus ring and release-to-activate behavior."""

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
        parent_bg: str | None = None,
        palette: Palette = LIGHT,
        font: tuple | None = None,
        icon: str | None = None,
        flat: bool = False,
    ) -> None:
        self.palette = palette
        parent_bg = palette.WINDOW if parent_bg is None else parent_bg
        if icon not in (None, "close", "minimize"):
            raise ValueError(f"不支持的按钮图标：{icon}")
        w, h = int(width * scale), int(height * scale)
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.command = command
        self._bg = palette.ACCENT if accent else parent_bg if flat else palette.SURFACE
        self._hover = palette.ACCENT_HOVER if accent else palette.HOVER
        self._pressed_bg = palette.ACCENT_PRESSED if accent else palette.TRACK
        self._fg = palette.ACCENT_TEXT if accent else palette.TEXT
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self._focused = False
        self._accent = accent
        self._flat = flat
        self._cy = h / 2
        self._focus_outline = self.create_polygon(
            rounded_points(0.5, 0.5, w - 0.5, h - 0.5, int(8 * scale)),
            smooth=True, fill="", outline="", width=1.5)
        self._shape = self.create_polygon(
            rounded_points(3, 3, w - 3, h - 3, int(6 * scale)),
            smooth=True, fill=self._bg, outline="", width=1)
        self._label = self.create_text(w / 2, h / 2, text=text, fill=self._fg,
                                       font=font or ("TkDefaultFont", 10))
        self._icon_lines: list[tuple[int, tuple[float, ...]]] = []
        if icon:
            self.itemconfigure(self._label, state="hidden")
            cx, cy, size = w / 2, h / 2, 4 * scale
            segments = ([(cx - size, cy - size, cx + size, cy + size),
                         (cx - size, cy + size, cx + size, cy - size)]
                        if icon == "close" else
                        [(cx - size, cy + 2 * scale, cx + size, cy + 2 * scale)])
            for coords in segments:
                item = self.create_line(*coords, fill=self._fg,
                                        width=max(1.5, 1.5 * scale), capstyle="round")
                self._icon_lines.append((item, coords))
        self.bind("<Enter>", lambda _e: self._hover_change(True))
        self.bind("<Leave>", lambda _e: self._hover_change(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda _e: self._focus(True))
        self.bind("<FocusOut>", lambda _e: self._focus(False))
        self.bind("<KeyPress-space>", self._press)
        self.bind("<KeyRelease-space>", self._release)
        self.bind("<Return>", self._invoke)
        self._draw()

    def _draw(self) -> None:
        p = self.palette
        fill = (self._bg if not self._enabled and self._flat else
                p.SURFACE if not self._enabled else
                self._pressed_bg if self._pressed else
                self._hover if self._hovered else self._bg)
        self.itemconfigure(self._focus_outline,
                           outline=p.ACCENT if self._focused and self._enabled else "")
        self.itemconfigure(self._shape, fill=fill,
                           outline="" if self._accent or self._flat else p.SEPARATOR)
        self.itemconfigure(self._label, fill=self._fg if self._enabled else p.TEXT_MUTED)
        offset = 1 if self._pressed else 0
        x, _y = self.coords(self._label)
        self.coords(self._label, x, self._cy + offset)
        for item, coords in self._icon_lines:
            self.itemconfigure(item, fill=self._fg if self._enabled else p.TEXT_MUTED)
            self.coords(item, *(v + offset if i % 2 else v for i, v in enumerate(coords)))

    def _hover_change(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _focus(self, focused: bool) -> None:
        self._focused = focused
        if not focused:
            self._pressed = False
        self._draw()

    def _press(self, _event=None) -> str:
        if self._enabled:
            self.focus_set()
            self._pressed = True
            self._draw()
        return "break"

    def _release(self, event=None) -> str:
        pressed, self._pressed = self._pressed, False
        inside = (event is None or event.keysym == "space" or
                  (0 <= event.x < self.winfo_width() and
                   0 <= event.y < self.winfo_height()))
        self._draw()
        if pressed and inside:
            self._invoke()
        return "break"

    def _invoke(self, _event=None) -> str:
        if self._enabled:
            self.command()
        return "break"

    def set_text(self, text: str) -> None:
        self.itemconfigure(self._label, text=text)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._pressed = False
        self.configure(cursor="hand2" if enabled else "", takefocus=enabled)
        self._draw()


class BoxedList(tk.Canvas):
    """Rounded card whose inset row windows leave the corners visible."""

    def __init__(self, master: tk.Misc, *, width: int, scale: float = 1.0,
                 parent_bg: str | None = None, palette: Palette = LIGHT) -> None:
        self.palette = palette
        parent_bg = palette.WINDOW if parent_bg is None else parent_bg
        self._cw = int(width * scale)
        self._scale = scale
        self._radius = int(16 * scale)
        self._inset = int(8 * scale)
        super().__init__(master, width=self._cw, height=1, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._card = self.create_polygon([0, 0, 0, 0], smooth=True,
                                         fill=palette.CARD, outline=palette.SEPARATOR, width=1)
        self._y = self._inset
        self._rows: list[tk.Frame] = []

    def add_row(self, height: int = 52) -> tk.Frame:
        h = int(height * self._scale)
        if self._rows:
            line = tk.Frame(self, bg=self.palette.SEPARATOR, height=1)
            self.create_window(int(18 * self._scale), self._y, window=line,
                               anchor="nw", width=self._cw - int(36 * self._scale),
                               height=1)
            self._y += 1
        row = tk.Frame(self, bg=self.palette.CARD)
        self.create_window(self._inset, self._y, window=row, anchor="nw",
                           width=self._cw - 2 * self._inset, height=h)
        self._y += h
        self._rows.append(row)
        return row

    def render(self) -> None:
        height = self._y + self._inset
        self.configure(height=height)
        self.coords(self._card,
                    *rounded_points(1, 1, self._cw - 1, height - 1, self._radius))
