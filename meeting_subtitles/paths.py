"""Where the app keeps its files.

Everything follows the XDG Base Directory spec rather than hardcoding
``~/.config``: distributions and desktop setups do move these, and reading the
variables costs nothing. Settings go in the config directory, the engine log in
the cache directory, and transcripts in ``~/Meetings`` where they are easy to
find.

Nothing here touches the disk except :func:`ensure`; importing this module is
free.
"""

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "meeting-subtitles"


def _xdg(variable: str, fallback: str) -> Path:
    """An XDG base directory, falling back to the spec's default."""
    value = os.environ.get(variable)
    # The spec says a relative path must be ignored, not resolved.
    if value and value.startswith("/"):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    """Settings that should survive a reinstall."""
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME


def cache_dir() -> Path:
    """Logs and other regenerable files."""
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME


def settings_path() -> Path:
    return config_dir() / "settings.json"


def env_path() -> Path:
    """Optional file of ``KEY=VALUE`` lines applied before starting the engine.

    A desktop launcher inherits none of the shell's environment, so anything
    set in ``.bashrc`` -- notably the proxy -- is invisible to the engine. This
    file is the one place both the shell scripts and the GUI can read.
    """
    return config_dir() / "env"


def server_log() -> Path:
    return cache_dir() / "server.log"


def default_output_dir() -> Path:
    return Path.home() / "Meetings"


def interpreter() -> str:
    """The Python to start our own child processes with.

    ``sys.executable`` rather than a guessed ``.venv/bin/python``: it is right
    both for a source checkout run from its virtualenv and for an installed
    package, and it guarantees the child sees exactly the dependencies the
    parent already imported successfully.
    """
    return sys.executable


def ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_env_file() -> dict:
    """Parse :func:`env_path` into a dict.

    Accepts the ``export KEY=VALUE`` form so the same file can also be sourced
    by a shell, and ignores anything it cannot parse rather than refusing to
    start.
    """
    result = {}
    try:
        # utf-8-sig tolerates a byte-order mark, which some editors add.
        text = env_path().read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


def open_in_file_manager(target: Path) -> None:
    """Show a folder in the desktop's file manager."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["xdg-open", str(target)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
