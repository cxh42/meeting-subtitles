"""Make Tk able to render Chinese, whatever interpreter we were started with.

Linux only, and only for one specific breakage: some managed Python builds
ship Tk without Xft or fontconfig. Such a Tk can only use X core
bitmap fonts -- it never sees the installed Noto CJK families, so every Chinese
string in the launcher and the subtitle overlay comes out as empty boxes.
Ubuntu's own Tk is built with Xft and does see them.

Rather than demanding a particular interpreter, we detect the broken case at
startup and re-exec once with a compatible system Tk preloaded. Probe that
environment in a child first: mismatched libraries or Tcl scripts can prevent
Tk from starting at all.
"""

import json
import logging
import os
import subprocess
import sys
import threading
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


def _system_library(pattern: str) -> str | None:
    for directory in LIB_DIRS:
        matches = sorted(Path(directory).glob(pattern))
        if matches:
            return str(matches[0])
    return None


def _script_directory(name: str, filename: str) -> str | None:
    for directory in ("/usr/share/tcltk", "/usr/lib", "/usr/share"):
        candidate = Path(directory) / name
        if (candidate / filename).is_file():
            return str(candidate)
    return None


def system_tk_environment() -> dict | None:
    """A system Tk matching this interpreter, including its startup scripts."""
    try:
        import tkinter as tk
    except ImportError:
        return None
    try:
        version = str(tk.TkVersion)
        # The pinned interpreter uses Tk 8.6. Its newer managed builds use Tk 9
        # and lose worker callbacks even when a system Tk repairs the fonts.
        if version != "8.6":
            return None
        tk_library = _script_directory(f"tk{version}", "tk.tcl")
        libraries = [_system_library(f"libtcl{version}.so*"),
                     _system_library(f"libtk{version}.so*")]
        tcl_library = _script_directory(f"tcl{version}", "init.tcl")
        if not all(libraries) or not tk_library or not tcl_library:
            return None
    except (RuntimeError, tk.TclError):
        return None
    environment = dict(os.environ)
    preload = os.pathsep.join(libraries)
    existing = environment.get("LD_PRELOAD", "")
    environment["LD_PRELOAD"] = f"{existing}:{preload}" if existing else preload
    # Tcl scripts require their exact library patch version, not only the ABI.
    environment["TCL_LIBRARY"] = tcl_library
    environment["TK_LIBRARY"] = tk_library
    environment[GUARD_ENV] = "1"
    return environment


def system_cjk_families(environment: dict | None) -> list[str]:
    """Verify the proposed repair without risking the running GUI process."""
    if environment is None:
        return []
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import json; "
             "from meeting_subtitles.tkfix import cjk_families; "
             "print(json.dumps(cjk_families(check_callbacks=True)))"],
            env=environment, capture_output=True, text=True, timeout=5, check=True,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def cjk_families(*, check_callbacks: bool = False) -> list[str]:
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
        if check_callbacks:
            delivered = []

            def finish():
                delivered.append(True)
                root.quit()

            def worker():
                try:
                    root.after(0, finish)
                except (RuntimeError, tk.TclError):
                    pass

            # Some Tk builds render correctly but drop every worker callback.
            # That leaves the launcher checking forever and the overlay empty.
            root.after(10, lambda: threading.Thread(target=worker, daemon=True).start())
            root.after(1500, root.quit)
            root.mainloop()
            if not delivered:
                return []
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

    environment = system_tk_environment()
    if not system_cjk_families(environment):
        logger.warning(
            "Tk 无法显示中文，且系统 Tk 替换检查未通过；"
            "请运行 uv run --no-sync meeting-subtitles-doctor 查看修复方法。"
        )
        return

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
    environment.pop("TCL_LIBRARY", None)
    environment.pop("TK_LIBRARY", None)
    return environment
