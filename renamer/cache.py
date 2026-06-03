"""
cache — Per-folder .series_cache.json for resolved series metadata.
"""

import json
from pathlib import Path
from typing import Any

from renamer.config import Provider, get_logger

log = get_logger(__name__)


class SeriesCache:
    """
    Per-folder cache that stores resolved series metadata so that
    subsequent runs don't need to re-search.

    File: <media_dir>/.series_cache.json
    """

    def __init__(self, media_dir: Path):
        self._path = media_dir / ".series_cache.json"

    def load(self) -> dict[str, Any] | None:
        """
        Load cached series info.  Returns None if the cache doesn't exist
        or is invalid.
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            # Convert provider string back to enum
            if "provider" in data and isinstance(data["provider"], str):
                data["provider"] = Provider.from_str(data["provider"])
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read series cache: %s", exc)
            return None

    def save(
        self,
        series_name: str,
        provider: Provider,
        tmdb_series_id: int | None = None,
        anilist_id: int | None = None,
        kitsu_id: int | None = None,
        episode_group_id: str | None = None,
        episode_start_mode: str | None = None,
        episode_title_lang: str | None = None,
        season_arc_names: dict[int, str] | None = None,
    ) -> None:
        """
        Save resolved series info to the cache file.
        """
        data: dict[str, Any] = {
            "series_name":        series_name,
            "provider":           provider.value,
            "tmdb_series_id":     tmdb_series_id,
            "anilist_id":         anilist_id,
            "kitsu_id":           kitsu_id,
            "episode_group_id":   episode_group_id,
            "episode_start_mode": episode_start_mode,
            "episode_title_lang": episode_title_lang,
            "season_arc_names":   season_arc_names,
        }
        try:
            self._path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.debug("Series cache saved to %s", self._path)
        except OSError as exc:
            log.warning("Could not write series cache: %s", exc)

    def clear(self) -> None:
        """Delete the cache file."""
        try:
            if self._path.exists():
                self._path.unlink()
                log.info("Series cache cleared: %s", self._path)
        except OSError as exc:
            log.warning("Could not delete series cache: %s", exc)
