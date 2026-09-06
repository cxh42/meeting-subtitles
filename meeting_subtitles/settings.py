"""Shared desktop preferences for the launcher and meeting windows."""

import json
import logging
import tempfile
from pathlib import Path

from meeting_subtitles import paths

logger = logging.getLogger(__name__)
CONFIG_PATH = paths.settings_path()


class Settings:
    """Last-used options, including the appearance used by child processes."""

    DEFAULTS = {
        "title": "",
        "record_mic": True,
        "refine": True,
        "domain": "cs-ai",
        "font_size": 20,
        "opacity": 94,
        "theme": "light",
        "output_dir": str(paths.default_output_dir()),
    }

    def __init__(self) -> None:
        self.data = dict(self.DEFAULTS)
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.data.update(saved)
        except (OSError, ValueError):
            pass
        if self.data["theme"] not in ("light", "dark"):
            self.data["theme"] = "light"

    def save(self) -> bool:
        temporary = None
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            kept = {key: self.data[key] for key in self.DEFAULTS}
            # A meeting process may read the theme while the launcher saves;
            # replacing a complete file prevents a partial JSON read.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=CONFIG_PATH.parent,
                prefix=".settings-", suffix=".json", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(kept, stream, ensure_ascii=False, indent=2)
            temporary.replace(CONFIG_PATH)
            return True
        except OSError as exc:
            logger.warning("无法保存设置，请检查配置目录的写入权限: %s", exc)
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
