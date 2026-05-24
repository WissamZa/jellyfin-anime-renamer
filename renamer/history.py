"""
history.py — Rename undo log.

Tracks old -> new filename mappings so renames can be reverted.
"""

import json
from pathlib import Path

from renamer.config import log


class RenameHistory:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error("Could not read history file: %s", e)
            return {}

    def save(self, data: dict[str, str]) -> None:
        existing = self.load()
        existing.update(data)
        self._path.write_text(
            json.dumps(existing, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

    def clear_entries(self, keys: list[str]) -> None:
        data = self.load()
        for k in keys:
            data.pop(k, None)
        self._path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
