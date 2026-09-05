"""Give a Tk window rounded corners on X11.

GNOME rounds every window it decorates, so a frameless overlay with square
corners is the one thing on screen that looks unfinished. Tk exposes no way to
shape a window, and neither ``-alpha`` nor a background colour can carve a
corner out -- the pixels have to leave the window's region entirely, which is
what the X11 SHAPE extension is for.

The mask is built once per size from filled rectangles: two inset rectangles
plus one quarter-disc per corner, approximated by horizontal spans. Rebuilding
it costs a few hundred rectangles and only happens when the window resizes.

Under a Wayland session this still works, because Tk is an X11 client running
on XWayland and mutter honours the shape of the XWayland surface.

Everything here degrades to a no-op rather than failing: no X11, no SHAPE
extension, or a window id Tk will not hand over just means square corners. Set
``MEETING_NATIVE_FRAME=1`` to keep the system title bars entirely, which is the
fallback worth trying first if a window misbehaves on an unusual desktop.
"""

import logging
import math
import os

logger = logging.getLogger(__name__)

#: Escape hatch: keep native decorations and skip all of the below.
NATIVE_FRAME = os.environ.get("MEETING_NATIVE_FRAME") == "1"

try:
    from Xlib import display as xdisplay
    from Xlib.ext import shape as xshape
    _XLIB_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _XLIB_AVAILABLE = False


def _corner_spans(width: int, height: int, radius: int) -> list[tuple[int, int, int, int]]:
    """Rectangles covering a rounded rectangle of this size.

    The straight middle is two big rectangles; each corner row contributes one
    span whose inset follows the circle, so the curve is exact to the pixel
    rather than a polygon approximation.
    """
    radius = max(0, min(radius, width // 2, height // 2))
    if radius == 0:
        return [(0, 0, width, height)]

    rects: list[tuple[int, int, int, int]] = [
        (radius, 0, width - 2 * radius, height),          # vertical band
        (0, radius, width, height - 2 * radius),          # horizontal band
    ]
    for row in range(radius):
        # Distance from the corner circle's centre to this row.
        dy = radius - row - 0.5
        dx = math.sqrt(max(radius * radius - dy * dy, 0.0))
        inset = int(round(radius - dx))
        span = width - 2 * inset
        if span <= 0:
            continue
        rects.append((inset, row, span, 1))                       # top row
        rects.append((inset, height - row - 1, span, 1))          # bottom row
    return rects


# ---------------------------------------------------------------------- X11

def undecorate(tk_window) -> bool:
    """Ask the window manager to drop its title bar, keeping the window managed.

    ``overrideredirect`` would also remove the decoration, but it takes the
    window out of the WM's hands entirely: no alt-tab, no taskbar entry, no
    minimise. ``_MOTIF_WM_HINTS`` is the old but universally honoured way to say
    "no decorations, still a normal window", and mutter only lets a shape mask
    through once it is no longer drawing a frame of its own.
    """
    if NATIVE_FRAME or not _XLIB_AVAILABLE:
        return False
    try:
        display = xdisplay.Display()
        atom = display.intern_atom("_MOTIF_WM_HINTS")
        window = display.create_resource_object("window", tk_window.winfo_id())
        tree = window.query_tree()
        target = tree.parent if tree.parent.id != tree.root.id else window
        # flags = MWM_HINTS_DECORATIONS, decorations = none
        target.change_property(atom, atom, 32, [2, 0, 0, 0, 0])
        display.sync()
        display.close()
        return True
    except Exception as exc:                           # pragma: no cover
        logger.debug("无法去除窗口装饰: %s", exc)
        return False


class RoundedWindow:
    """Applies and maintains a rounded-corner shape mask for a Tk window."""

    def __init__(self, tk_window, radius: int = 12) -> None:
        self.radius = radius
        self._size: tuple[int, int] | None = None
        self._display = None
        self._window = None
        if NATIVE_FRAME:
            return
        if not _XLIB_AVAILABLE:
            logger.debug("python-xlib 不可用，窗口保持直角。")
            return
        try:
            self._display = xdisplay.Display()
            if not self._display.has_extension("SHAPE"):
                logger.debug("X server 无 SHAPE 扩展，窗口保持直角。")
                self._display = None
                return
            self._window = self._toplevel_of(tk_window.winfo_id())
        except Exception as exc:                       # pragma: no cover
            logger.debug("无法准备圆角遮罩: %s", exc)
            self._display = None

    def _toplevel_of(self, window_id: int):
        """The X window the compositor actually shows.

        Tk wraps every toplevel: ``winfo_id()`` returns the *content* window,
        whose parent is the window the WM manages. Shaping the content window
        succeeds without error and changes nothing on screen, because the
        wrapper around it is still rectangular. Walk up to the child of the
        root instead.
        """
        window = self._display.create_resource_object("window", window_id)
        for _ in range(8):
            tree = window.query_tree()
            if tree.parent is None or tree.parent.id == tree.root.id:
                return window
            window = tree.parent
        return window

    @property
    def available(self) -> bool:
        return (not NATIVE_FRAME and self._display is not None
                and self._window is not None)

    def frame_size(self) -> tuple[int, int] | None:
        """Current size of the shaped window, decorations included."""
        if self._display is None or self._window is None:
            return None
        try:
            geometry = self._window.get_geometry()
            return geometry.width, geometry.height
        except Exception:
            return None

    def apply_current(self) -> None:
        """Shape to whatever size the shaped window actually is.

        Safer than passing Tk's numbers: for a decorated window the shaped
        window is the WM frame, which is larger than the client area, so a
        client-sized mask silently cuts the bottom of the window off.
        """
        size = self.frame_size()
        if size:
            self.apply(*size)

    def apply(self, width: int, height: int) -> None:
        """Shape the window to ``width`` x ``height`` with rounded corners."""
        if not self.available or width <= 0 or height <= 0:
            return
        if self._size == (width, height):
            return
        try:
            mask = self._window.create_pixmap(width, height, 1)
            gc = mask.create_gc(foreground=0, background=0)
            mask.fill_rectangle(gc, 0, 0, width, height)
            gc.change(foreground=1)
            for x, y, w, h in _corner_spans(width, height, self.radius):
                mask.fill_rectangle(gc, x, y, w, h)
            self._window.shape_mask(
                xshape.SO.Set, xshape.SK.Bounding, 0, 0, mask)
            gc.free()
            mask.free()
            self._display.flush()
            self._size = (width, height)
        except Exception as exc:                       # pragma: no cover
            logger.debug("应用圆角遮罩失败: %s", exc)
            self._display = None

    def close(self) -> None:
        if self._display is not None:
            try:
                self._display.close()
            except Exception:
                pass
            self._display = None
