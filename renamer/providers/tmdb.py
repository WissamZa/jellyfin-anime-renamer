"""
tmdb.py — TMDB (The Movie Database) provider.

Uses the TMDB v3 API to:
  1. Search for TV series by name
  2. Fetch episode lists per season with titles
  3. Enrich with Japanese titles via romanisation

Requires TMDB_API_KEY (free at themoviedb.org).
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from renamer.config import Config, log
from renamer.providers.base import EpisodeFetcher, EpisodeInfo, SeriesSearchResult
from renamer.romaniser import _romaniser


class TMDBSearch:
    """
    Resolves a human-readable series name to a TMDB series ID.
    Used by qbit_hook.py which only knows the torrent name.
    """

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str):
        self._params = {"api_key": api_key}

    def find(self, name: str) -> Optional[tuple[int, str]]:
        """
        Returns (tmdb_id, romaji_name) for the top search result.

        Romaji resolution order:
          1. AniList native romaji title  (human-curated)
          2. pykakasi romanisation of TMDB Japanese title
          3. TMDB English title (last resort)
        """
        data = EpisodeFetcher._get(
            f"{self.BASE}/search/tv",
            {**self._params, "query": name, "language": "en-US"},
        )
        if not data:
            return None
        results = data.get("results", [])
        if not results:
            log.warning("TMDB search for '%s' returned no results.", name)
            return None
        top = results[0]
        tmdb_id = top["id"]
        en_name = top["name"]

        ja_name = self._fetch_japanese_name(tmdb_id) or en_name
        tmdb_romaji = _romaniser.to_romaji(ja_name)

        # Cross-reference with AniList/Kitsu for the best romaji
        from renamer.providers.romaji_resolver import RomajiResolver
        resolver = RomajiResolver()
        romaji = resolver.resolve(
            search_name=name,
            tmdb_romaji=tmdb_romaji,
            english_name=en_name,
        )

        log.info("Final series name: '%s' (TMDB id=%d)", romaji, tmdb_id)
        return tmdb_id, romaji

    def _fetch_japanese_name(self, series_id: int) -> Optional[str]:
        data = EpisodeFetcher._get(
            f"{self.BASE}/tv/{series_id}",
            {**self._params, "language": "ja"},
        )
        if data:
            return data.get("name") or data.get("original_name")
        return None


class TMDBFetcher(EpisodeFetcher):
    """TMDB episode metadata fetcher."""

    name = "TMDB"
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, series_id: int, cfg: Optional[Config] = None):
        self._key = api_key
        self._series_id = series_id
        self._params = {"api_key": api_key}
        self._cfg = cfg or Config()
        self._has_specials = True

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        log.info("TMDB — fetching series id=%s …", self._series_id)
        show = self._get(
            f"{self.BASE}/tv/{self._series_id}", self._params, cfg=self._cfg
        )
        if not show:
            return None

        self._has_specials = any(
            s.get("season_number") == 0 for s in show.get("seasons", [])
        )
        seasons = [s for s in show.get("seasons", []) if s["season_number"] > 0]
        log.info("Found %d seasons — fetching episodes in parallel …", len(seasons))

        season_data: dict[int, list] = {}

        def fetch_season_en(s: dict) -> tuple[int, list]:
            sn = s["season_number"]
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data = self._get(url, self._params, cfg=self._cfg)
            if not data:
                log.warning(
                    "TMDB: failed to fetch season %d (English) — will retry once", sn
                )
                data = self._get(url, self._params, cfg=self._cfg)
            eps = data.get("episodes", []) if data else []
            if not eps:
                log.warning("TMDB: season %d returned 0 episodes", sn)
            return sn, sorted(eps, key=lambda x: x["episode_number"])

        with ThreadPoolExecutor(max_workers=self._cfg.MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_season_en, s): s for s in seasons}
            for future in as_completed(futures):
                sn, eps = future.result()
                season_data[sn] = eps

        def enrich_with_ja_titles(sn: int, eps: list) -> list:
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data_ja = self._get(url, {**self._params, "language": "ja"}, cfg=self._cfg)
            if not data_ja:
                return eps
            ja_map = {
                e["episode_number"]: e.get("name", "")
                for e in data_ja.get("episodes", [])
            }
            for ep in eps:
                ja_title = ja_map.get(ep["episode_number"], "")
                if ja_title:
                    ep["name"] = _romaniser.to_romaji(ja_title)
            return eps

        for sn in sorted(season_data):
            season_data[sn] = enrich_with_ja_titles(sn, season_data[sn])

        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        for sn in sorted(season_data):
            for pos, ep in enumerate(season_data[sn], start=1):
                mapping[abs_counter] = EpisodeInfo(
                    absolute=abs_counter,
                    season=sn,
                    episode=pos,
                    title=ep.get("name", ""),
                    air_date=ep.get("air_date", ""),
                    overview=ep.get("overview", ""),
                    source=self.name,
                    is_special=False,
                )
                abs_counter += 1

        log.info("TMDB: mapped %d episodes.", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        if not self._has_specials:
            log.info("No specials (Season 0) listed for this series on TMDB.")
            return {}

        log.info("TMDB — fetching specials (Season 0) …")
        url = f"{self.BASE}/tv/{self._series_id}/season/0"
        data = self._get(url, self._params, cfg=self._cfg)
        if not data:
            log.info("No specials found on TMDB.")
            return {}

        specials: dict[int, EpisodeInfo] = {}
        for ep in data.get("episodes", []):
            ep_num = ep["episode_number"]
            specials[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=0,
                episode=ep_num,
                title=_romaniser.to_romaji(ep.get("name", f"Special {ep_num}")),
                air_date=ep.get("air_date", ""),
                overview=ep.get("overview", ""),
                source=self.name,
                is_special=True,
            )

        log.info("TMDB: found %d specials.", len(specials))
        return specials
