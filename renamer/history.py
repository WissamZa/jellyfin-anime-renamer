"""
history — RenameHistory (undo log).

The history maps the **absolute path of the new (renamed) file** to the
**absolute path of the original file**.  Using absolute paths on both sides
ensures that ``undo()`` can always find and revert files reliably, even
when the original was inside a season subfolder or the working directory
has changed since the rename session.

Legacy history files that used relative paths (``new_rel -> orig_name``)
are handled transparently: on load, relative keys are resolved against
*media_dir* if supplied, and relative values are also resolved.
"""

from __future__ import annotations

import json
from pathlib import Path

from renamer.config import get_logger

log = get_logger()


class RenameHistory:
    """
    JSON-backed undo log: maps ``new_absolute_path`` -> ``original_absolute_path``.

    Parameters
    ----------
    path:
        Filesystem path of the JSON history file.
    media_dir:
        Optional media directory used to resolve legacy relative-path entries.
    """

    def __init__(self, path: Path, media_dir: Path | None = None):
        self._path = path
        self._media_dir = media_dir

    def load(self) -> dict[str, str]:
        """
        Load history, upgrading legacy relative-path entries to absolute paths.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error("Could not read history file: %s", e)
            return {}

        # Upgrade legacy entries where keys/values are relative paths
        if self._media_dir is not None:
            upgraded: dict[str, str] = {}
            for new_key, orig_val in raw.items():
                # Key: if not absolute, resolve against media_dir
                new_path = Path(new_key)
                if not new_path.is_absolute():
                    new_key = str((self._media_dir / new_key).resolve())

                # Value: if not absolute, resolve against media_dir
                orig_path = Path(orig_val)
                if not orig_path.is_absolute():
                    orig_val = str((self._media_dir / orig_val).resolve())

                upgraded[new_key] = orig_val
            return upgraded

        return raw

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
