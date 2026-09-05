"""Make Tk able to render Chinese, whatever interpreter we were started with.

Linux only, and only for one specific breakage: Anaconda ships a
``libtk8.6.so`` built without Xft or fontconfig. Such a Tk can only use X core
bitmap fonts -- it never sees the installed Noto CJK families, so every Chinese
string in the launcher and the subtitle overlay comes out as empty boxes.
Ubuntu's own tk8.6 *is* built with Xft and does see them.

Rather than demanding a particular interpreter, we detect the broken case at
startup and re-exec once with the system Tcl/Tk preloaded. Anaconda and Ubuntu
both ship 8.6.x, so the ABI matches; if anything about the detection fails we
carry on with the fonts we have rather than refusing to start.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

GUARD_ENV = "MEETING_TK_PRELOADED"

LIB_DIRS = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib64",
    "/usr/lib",
    "/lib/x86_64-linux-gnu",
)

CJK_MARKERS = ("CJK", "Han Sans", "Han Serif", "WenQuanYi", "Heiti", "Hei")


def system_tcl_tk() -> tuple[str, str] | None:
    """Paths to the system libtcl/libtk pair, when both are present."""
    for directory in LIB_DIRS:
        base = Path(directory)
        if not base.is_dir():
            continue
        tcl = sorted(base.glob("libtcl8.6.so*"))
        tk = sorted(base.glob("libtk8.6.so*"))
        if tcl and tk:
            return str(tcl[0]), str(tk[0])
    return None


def cjk_families() -> list[str]:
    """CJK-capable font families the current Tk can actually use.

    Returns an empty list when Tk cannot start at all (no display, for example),
    which callers treat the same as "nothing to fix here".
    """
    try:
        import tkinter as tk
        import tkinter.font as tkfont
    except ImportError:
        return []
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        families = set(tkfont.families(root))
    except Exception:
        return []
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    return sorted(
        family for family in families
        if any(marker in family for marker in CJK_MARKERS)
    )


def ensure_cjk_tk(module: str | None = None) -> None:
    """Re-exec with the system Tcl/Tk preloaded if Tk has no CJK font.

    ``module`` is the ``-m`` target to restart with; pass it whenever the
    process was launched as ``python -m <module>``, since re-running the raw
    ``__main__.py`` path would break the package's relative imports.

    Does nothing when already re-executed once (guarded by an env var), when Tk
    already sees a CJK family, or when no system Tcl/Tk pair is installed.
    """
    if os.environ.get(GUARD_ENV):
        return
    if sys.platform != "linux":
        return
    if "DISPLAY" not in os.environ and "WAYLAND_DISPLAY" not in os.environ:
        return
    if cjk_families():
        return

    libs = system_tcl_tk()
    if libs is None:
        logger.warning(
            "Tk 看不到任何中文字体，且未找到系统 Tcl/Tk 可供替换；"
            "界面中的中文可能显示为方块。"
        )
        return

    preload = os.pathsep.join(libs)
    existing = os.environ.get("LD_PRELOAD", "")
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = f"{existing}:{preload}" if existing else preload
    environment[GUARD_ENV] = "1"

    if module:
        argv = [sys.executable, "-m", module, *sys.argv[1:]]
    elif sys.argv and Path(sys.argv[0]).is_file():
        argv = [sys.executable, *sys.argv]
    else:
        # Started as `python -c ...` or similar: sys.argv does not hold enough
        # to rebuild the command, and half a command line is worse than tofu.
        logger.warning(
            "无法重建启动命令（缺少 module 参数），跳过字体修复；"
            "界面中的中文可能显示为方块。"
        )
        return

    logger.info("使用系统 Tcl/Tk 重新启动以正确显示中文。")
    try:
        os.execve(sys.executable, argv, environment)
    except OSError as exc:
        logger.warning("重新启动失败 (%s)，继续使用当前字体。", exc)


def child_environment() -> dict:
    """Environment for subprocesses that must NOT inherit the Tk preload.

    The transcription server loads PyTorch and CUDA; there is no reason to drag
    Tcl/Tk into that address space, and any interposition there would be a
    surprise rather than a fix.
    """
    environment = dict(os.environ)
    environment.pop("LD_PRELOAD", None)
    environment.pop(GUARD_ENV, None)
    return environment
