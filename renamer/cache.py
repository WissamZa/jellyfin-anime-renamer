"""
cache.py — Per-folder series metadata cache.

After the first successful search for a folder, the resolved series
name, provider, and IDs are saved to a JSON file inside the media
directory.  Subsequent runs read from this file instead of hitting
the API again.
"""

import datetime
import json
from pathlib import Path
from typing import Optional

from renamer.config import Provider, log


class SeriesCache:
    """
    Per-folder cache for resolved series metadata.

    The cache file is .series_cache.json, stored in the media directory.
    It can be deleted manually to force a re-search.
    """

    FILENAME = ".series_cache.json"

    def __init__(self, media_dir: Path):
        self._path = media_dir / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[dict]:
        """
        Load cached series info from the media directory.
        Returns None if the cache doesn't exist or is invalid.
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read series cache: %s", exc)
            return None

        if not isinstance(data, dict):
            return None
        if "series_name" not in data:
            log.warning("Series cache is missing required fields — ignoring.")
            return None

        # Must have at least one provider ID
        has_id = any(
            data.get(k) is not None
            for k in ("tmdb_series_id", "anilist_id", "kitsu_id")
        )
        if not has_id:
            log.warning("Series cache has no provider ID — ignoring.")
            return None

        provider_str = data.get("provider", "tmdb")
        data["provider"] = Provider.from_str(provider_str)

        log.info(
            "Loaded series cache: '%s' (provider=%s, TMDB id=%s, AniList id=%s, Kitsu id=%s)",
            data.get("series_name"),
            data.get("provider").value,
            data.get("tmdb_series_id"),
            data.get("anilist_id"),
            data.get("kitsu_id"),
        )
        return data

    def save(
        self,
        series_name: str,
        provider: Provider = Provider.TMDB,
        tmdb_series_id: Optional[int] = None,
        anilist_id: Optional[int] = None,
        kitsu_id: Optional[int] = None,
    ) -> None:
        """Persist resolved series metadata to the media directory."""
        data = {
            "series_name": series_name,
            "provider": provider.value,
            "tmdb_series_id": tmdb_series_id,
            "anilist_id": anilist_id,
            "kitsu_id": kitsu_id,
            "resolved_at": datetime.datetime.now().isoformat(),
        }
        try:
            self._path.write_text(
                json.dumps(data, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Saved series cache -> %s", self._path)
        except OSError as exc:
            log.warning("Could not save series cache: %s", exc)

    def clear(self) -> None:
        """Delete the cache file so the next run re-searches."""
        if self._path.exists():
            try:
                self._path.unlink()
                log.info("Deleted series cache: %s", self._path)
            except OSError as exc:
                log.warning("Could not delete series cache: %s", exc)
