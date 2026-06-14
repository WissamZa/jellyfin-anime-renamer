"""
renamer.library_index
=====================
Persistent index mapping series folder paths → provider IDs.

File: ``<project_root>/library_index.json``

Structure::

    {
        "/mnt/D/Torrent/One Piece": {
            "name":       "One Piece",
            "tmdb_id":    37854,
            "anilist_id": 21,
            "anidb_aid":  69,
            "kitsu_id":   null,
            "updated_at": "2026-06-08T02:30:00"
        }
    }

Lookup priority used by qbit_hook
----------------------------------
1. **ID match**  (tmdb_id / anilist_id / kitsu_id / anidb_aid)
   → exact, works even when folder name differs completely from torrent name
2. **Fuzzy name match**  — existing ``find_matching_folder()`` behaviour
3. **Create new folder** — last resort

The index is updated automatically after every successful hook run, and can
be rebuilt manually via the CLI "Configure Hook" → "Rebuild Library Index"
option (reads each folder's ``.series_cache.json``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from renamer.config import get_logger

log = get_logger(__name__)

_INDEX_FILENAME = "library_index.json"


class LibraryIndex:
    """
    Persistent index of series folders → provider IDs.

    Parameters
    ----------
    index_path:
        Path to the JSON index file.  Defaults to
        ``<project_root>/library_index.json``.
    """

    def __init__(self, index_path: Path | None = None) -> None:
        if index_path is None:
            index_path = Path(__file__).resolve().parent.parent / _INDEX_FILENAME
        self._path = index_path
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    # ── I/O ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load the index from disk.  No-op if the file does not exist."""
        if not self._path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
            else:
                log.warning("library_index.json has unexpected format — resetting.")
                self._data = {}
        except Exception as exc:
            log.warning("Could not read library_index.json: %s", exc)
            self._data = {}

    def save(self) -> None:
        """Write the index to disk atomically (write to .tmp then rename)."""
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            log.debug("Library index saved (%d entries) → %s", len(self._data), self._path)
        except Exception as exc:
            log.warning("Could not save library_index.json: %s", exc)

    # ── Lookup ───────────────────────────────────────────────────────────

    def find_by_ids(
        self,
        tmdb_id: int | None = None,
        anilist_id: int | None = None,
        kitsu_id: int | None = None,
        anidb_aid: int | None = None,
    ) -> Path | None:
        """
        Return the folder ``Path`` whose entry matches any of the given
        provider IDs.

        Only returns a path if the directory still exists on disk.
        Stale entries (path deleted) are removed and the index is saved.

        Returns ``None`` if no match is found or all IDs are ``None``.
        """
        if not any([tmdb_id, anilist_id, kitsu_id, anidb_aid]):
            return None

        stale: list[str] = []

        for path_str, entry in self._data.items():
            matched = False
            if tmdb_id is not None and entry.get("tmdb_id") == tmdb_id or anilist_id is not None and entry.get("anilist_id") == anilist_id or kitsu_id is not None and entry.get("kitsu_id") == kitsu_id or anidb_aid is not None and entry.get("anidb_aid") == anidb_aid:
                matched = True

            if not matched:
                continue

            folder = Path(path_str)
            if folder.is_dir():
                log.info(
                    "Library index match: '%s' at %s",
                    entry.get("name", folder.name),
                    path_str,
                )
                return folder

            # Path was deleted — mark for removal
            log.warning(
                "Library index: path no longer exists on disk — removing: %s",
                path_str,
            )
            stale.append(path_str)

        if stale:
            for p in stale:
                self._data.pop(p, None)
            self.save()

        return None

    # ── Update ───────────────────────────────────────────────────────────

    def update(
        self,
        folder: Path,
        name: str,
        *,
        tmdb_id: int | None = None,
        anilist_id: int | None = None,
        kitsu_id: int | None = None,
        anidb_aid: int | None = None,
    ) -> None:
        """
        Upsert an entry for *folder*.

        Non-``None`` IDs overwrite existing values; ``None`` values never
        clobber previously stored IDs, so partial updates are always safe.
        """
        path_str = str(folder.resolve())
        existing = self._data.get(path_str, {})

        entry: dict[str, Any] = {
            "name": name,
            "tmdb_id": tmdb_id if tmdb_id is not None else existing.get("tmdb_id"),
            "anilist_id": anilist_id if anilist_id is not None else existing.get("anilist_id"),
            "kitsu_id": kitsu_id if kitsu_id is not None else existing.get("kitsu_id"),
            "anidb_aid": anidb_aid if anidb_aid is not None else existing.get("anidb_aid"),
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._data[path_str] = entry
        log.debug(
            "Library index: upserted '%s' (TMDB=%s AL=%s Kitsu=%s AniDB=%s)",
            folder.name,
            entry["tmdb_id"],
            entry["anilist_id"],
            entry["kitsu_id"],
            entry["anidb_aid"],
        )

    def remove(self, folder: Path) -> bool:
        """
        Remove an entry for *folder* from the index.

        Returns ``True`` if the entry existed and was removed.
        """
        path_str = str(folder.resolve())
        if path_str in self._data:
            del self._data[path_str]
            log.info("Library index: removed entry for %s", folder.name)
            return True
        return False

    # ── Rebuild ──────────────────────────────────────────────────────────

    def rebuild(self, base_path: Path) -> int:
        """
        Scan all immediate subdirectories of *base_path*, read their
        ``.series_cache.json`` (written by the renamer after a successful run),
        and rebuild the index from those cached series IDs.

        Folders without a ``.series_cache.json`` are skipped (they will be
        added to the index on the next qbit_hook run that processes them).

        Returns the number of entries added or updated.
        """
        if not base_path.is_dir():
            log.warning("Library index rebuild: base_path does not exist: %s", base_path)
            return 0

        count = 0
        for sub in sorted(base_path.iterdir()):
            if not sub.is_dir():
                continue
            cache_file = sub / ".series_cache.json"
            if not cache_file.exists():
                log.debug("Library index rebuild: no cache for %s — skipping", sub.name)
                continue

            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Could not read %s: %s", cache_file, exc)
                continue

            self.update(
                sub,
                name=cache.get("series_name") or sub.name,
                tmdb_id=cache.get("tmdb_series_id"),
                anilist_id=cache.get("anilist_id"),
                kitsu_id=cache.get("kitsu_id"),
            )
            count += 1
            log.debug(
                "Library index rebuild: %s → TMDB=%s AL=%s Kitsu=%s",
                sub.name,
                cache.get("tmdb_series_id"),
                cache.get("anilist_id"),
                cache.get("kitsu_id"),
            )

        self.save()
        log.info(
            "Library index rebuilt: %d entries from %s (total: %d)",
            count,
            base_path,
            len(self._data),
        )
        return count

    # ── Introspection ────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"LibraryIndex(path={self._path!r}, entries={len(self._data)})"

    def entries(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of the raw index data (keyed by absolute path)."""
        return dict(self._data)


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, one per process)
# ---------------------------------------------------------------------------

_global_index: LibraryIndex | None = None


def get_library_index(index_path: Path | None = None) -> LibraryIndex:
    """
    Return the global ``LibraryIndex`` singleton, creating it on first call.

    Parameters
    ----------
    index_path:
        Override the default path (useful in tests).  Ignored after the
        singleton is already created.
    """
    global _global_index
    if _global_index is None:
        _global_index = LibraryIndex(index_path)
    return _global_index


def reset_library_index() -> None:
    """Reset the global singleton (mainly for tests)."""
    global _global_index
    _global_index = None
