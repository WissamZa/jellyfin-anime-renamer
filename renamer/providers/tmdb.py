"""
providers.tmdb — TMDBFetcher, TMDBSearch, and Episode Groups support.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from renamer.config import Config, get_logger
from renamer.providers.base import (
    EpisodeFetcher,
    EpisodeGroupInfo,
    EpisodeInfo,
)
from renamer.romaniser import get_romaniser

log = get_logger()


class TMDBSearch:
    """
    Resolves a human-readable series name to a TMDB series ID.
    Used by qbit_hook.py which only knows the torrent name.
    """

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str):
        self._params = {"api_key": api_key}
        # Use a throwaway fetcher just for its _get helper
        self._fetcher = _TMDBGetHelper()

    def find(self, name: str) -> tuple[int, str] | None:
        """
        Returns (tmdb_id, romaji_name) for the top search result.

        Romaji resolution order:
          1. AniList native romaji title  (human-curated)
          2. pykakasi romanisation of TMDB Japanese title
          3. TMDB English title (last resort)
        """
        data = self._fetcher._get(
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
        romaniser = get_romaniser()
        tmdb_romaji = romaniser.to_romaji(ja_name)

        # Cross-reference with AniList and pick the best romaji
        from renamer.providers.romaji_resolver import RomajiResolver
        resolver = RomajiResolver()
        romaji = resolver.resolve(
            search_name=name,
            tmdb_romaji=tmdb_romaji,
            english_name=en_name,
        )

        log.info("Final series name: '%s' (TMDB id=%d)", romaji, tmdb_id)
        return tmdb_id, romaji

    def _fetch_japanese_name(self, series_id: int) -> str | None:
        data = self._fetcher._get(
            f"{self.BASE}/tv/{series_id}",
            {**self._params, "language": "ja"},
        )
        if data:
            return data.get("name") or data.get("original_name")
        return None


class _TMDBGetHelper(EpisodeFetcher):
    """Minimal fetcher subclass just to get _get() for TMDBSearch."""
    name = "TMDBSearchHelper"

    def fetch(self) -> dict[int, EpisodeInfo] | None:
        return None

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}


class TMDBFetcher(EpisodeFetcher):
    """Fetches episode data from TMDB, with Episode Groups support."""

    name = "TMDB"
    BASE = "https://api.themoviedb.org/3"

    def __init__(
        self,
        api_key: str,
        series_id: int,
        cfg: Config | None = None,
    ):
        super().__init__()
        self._key = api_key
        self._series_id = series_id
        self._params = {"api_key": api_key}
        self._cfg = cfg or Config()
        self._has_specials = True
        self._group_specials: dict[int, EpisodeInfo] = {}

    # ── Alternative Titles ─────────────────────────────────
    def fetch_alternative_titles(self) -> list[dict]:
        """
        Fetch alternative titles for the series from TMDB.

        Returns a list of dicts with keys: title, type, iso_3166_1.
        The TMDB API returns titles in two groups:
          - results[]  : titles with a type (3=original, 4=primary, etc.)
          - translations[] : titles from the /translations endpoint

        We merge both and deduplicate.
        """
        all_titles: list[dict] = []
        seen: set[str] = set()

        # 1. Fetch /tv/{id}/alternative_titles
        data = self._get(
            f"{self.BASE}/tv/{self._series_id}/alternative_titles",
            self._params,
            cfg=self._cfg,
        )
        if data:
            for t in data.get("results", []):
                title = (t.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                all_titles.append({
                    "title": title,
                    "type": t.get("type", ""),
                    "iso_3166_1": t.get("iso_3166_1", ""),
                })

        # 2. Fetch /tv/{id}/translations for more title variants
        trans_data = self._get(
            f"{self.BASE}/tv/{self._series_id}/translations",
            self._params,
            cfg=self._cfg,
        )
        if trans_data:
            for t in trans_data.get("translations", []):
                # Each translation has data.name (title in that language)
                td = t.get("data", {})
                title = (td.get("name") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                iso = t.get("iso_639_1", "") + "-" + t.get("iso_3166_1", "")
                all_titles.append({
                    "title": title,
                    "type": "translation",
                    "iso_3166_1": iso,
                })

        # 3. Fetch the main series info for original_name and name
        show = self._get(
            f"{self.BASE}/tv/{self._series_id}",
            self._params,
            cfg=self._cfg,
        )
        if show:
            for field in ("name", "original_name"):
                title = (show.get(field) or "").strip()
                if title and title not in seen:
                    seen.add(title)
                    all_titles.append({
                        "title": title,
                        "type": "primary" if field == "name" else "original",
                        "iso_3166_1": show.get("origin_country", [""])[0] if field == "original_name" else "",
                    })

            # Also add the Japanese name
            ja_data = self._get(
                f"{self.BASE}/tv/{self._series_id}",
                {**self._params, "language": "ja"},
                cfg=self._cfg,
            )
            if ja_data:
                ja_name = (ja_data.get("name") or "").strip()
                if ja_name and ja_name not in seen:
                    seen.add(ja_name)
                    all_titles.append({
                        "title": ja_name,
                        "type": "japanese",
                        "iso_3166_1": "JP",
                    })

        log.info(
            "TMDB: found %d alternative title(s) for id=%d",
            len(all_titles), self._series_id,
        )
        return all_titles

    # ── Episode Groups ──────────────────────────────────────
    def fetch_episode_groups(self) -> list[EpisodeGroupInfo]:
        """
        Fetch the list of available episode groups for this series.

        TMDB episode groups let users reorganise episodes into custom
        season structures (e.g. "Absolute Order" for long-running anime
        like One Piece).
        """
        data = self._get(
            f"{self.BASE}/tv/{self._series_id}/episode_groups",
            self._params,
            cfg=self._cfg,
        )
        if not data:
            log.info("No episode groups found for TMDB id=%d", self._series_id)
            return []

        groups: list[EpisodeGroupInfo] = []
        for g in data.get("results", []):
            groups.append(EpisodeGroupInfo(
                id=g.get("id", ""),
                name=g.get("name", "Unnamed"),
                description=g.get("description", ""),
                episode_count=g.get("episode_count", 0),
                group_count=g.get("group_count", 0),
                type=g.get("type", 0),
            ))

        log.info(
            "TMDB: found %d episode group(s) for id=%d",
            len(groups), self._series_id,
        )
        return groups

    def fetch_episode_group_details(
        self, group_id: str,
    ) -> dict[int, EpisodeInfo] | None:
        """
        Fetch the full episode group details and build an episode map.

        Each "group" within the episode group represents a season in the
        custom ordering.  Episodes within a group have an `order` field
        for their position within that group, plus the original TMDB
        `episode_number` and `season_number`.

        Specials groups (named "Specials" or containing only season 0
        episodes) are separated into ``self._group_specials`` so that
        ``fetch_specials()`` can return them instead of hitting the
        regular Season 0 API.
        """
        data = self._get(
            f"{self.BASE}/tv/episode_group/{group_id}",
            self._params,
            cfg=self._cfg,
        )
        if not data:
            log.error(
                "Could not fetch episode group '%s' — falling back.",
                group_id,
            )
            return None

        group_name = data.get("name", "Unknown Group")
        groups = data.get("groups", [])
        log.info(
            "Episode group '%s': %d sub-group(s), type=%d",
            group_name, len(groups), data.get("type", 0),
        )

        romaniser = get_romaniser()
        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1       # counts only regular episodes
        special_counter = 1   # counts only specials
        self._group_specials = {}

        # Fetch Japanese titles for each original season to merge
        # (episode groups only return English by default)
        ja_titles = self._prefetch_ja_titles_for_groups(groups)

        for group in groups:
            episodes = group.get("episodes", [])
            gname = group.get("name", "").strip()

            # ── Determine season number ──────────────────────
            # The group's "order" field is the 0-based position of
            # this sub-group within the episode group, NOT the season
            # number.  We must derive the season from the name or
            # from the episodes' original season_number values.
            is_specials_group = False

            # Check 1: group name clearly indicates specials
            if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
                is_specials_group = True
                season_num = 0
            else:
                # Check 2: all episodes in this group have season_number==0
                orig_seasons = {ep.get("season_number", -1) for ep in episodes}
                if orig_seasons == {0}:
                    is_specials_group = True
                    season_num = 0
                else:
                    # Check 3: extract season number from group name
                    sm = re.search(r"(\d+)", gname)
                    season_num = int(sm.group(1)) if sm else (group.get("order", 0) or 0) + 1

            log.info(
                "  Group '%s' (season %d%s): %d episodes",
                gname, season_num,
                ", specials" if is_specials_group else "",
                len(episodes),
            )

            for i, ep in enumerate(episodes):
                # Episode position within this group (1-based).
                # TMDB's `order` field is 0-based, so we use enumerate
                # instead of the buggy `order or episode_number` logic.
                ep_order = i + 1

                ep_title = ep.get("name", f"Episode {abs_counter}")

                # Try to find Japanese title from our prefetch
                orig_season = ep.get("season_number", 0)
                orig_ep = ep.get("episode_number", 0)
                ja_title = ja_titles.get((orig_season, orig_ep), "")
                if ja_title:
                    ep_title = romaniser.to_romaji(ja_title)

                if is_specials_group:
                    info = EpisodeInfo(
                        absolute=special_counter,
                        season=0,
                        episode=ep_order,
                        title=ep_title,
                        air_date=ep.get("air_date", ""),
                        overview=ep.get("overview", ""),
                        source=f"{self.name} (Group: {group_name})",
                        is_special=True,
                    )
                    self._group_specials[ep_order] = info
                    special_counter += 1
                else:
                    info = EpisodeInfo(
                        absolute=abs_counter,
                        season=season_num,
                        episode=ep_order,
                        title=ep_title,
                        air_date=ep.get("air_date", ""),
                        overview=ep.get("overview", ""),
                        source=f"{self.name} (Group: {group_name})",
                        is_special=False,
                    )
                    mapping[abs_counter] = info
                    abs_counter += 1

        if self._group_specials:
            self._has_specials = True

        log.info(
            "Episode group '%s': mapped %d episodes, %d specials.",
            group_name, len(mapping), len(self._group_specials),
        )
        return mapping

    def _prefetch_ja_titles_for_groups(
        self, groups: list[dict],
    ) -> dict[tuple[int, int], str]:
        """
        Pre-fetch Japanese episode titles for all original seasons
        referenced by the episode group, so we can romanise them.
        Returns {(orig_season, orig_ep): ja_title}.
        """
        # Collect all unique original seasons referenced (including 0/specials)
        orig_seasons: set[int] = set()
        for group in groups:
            for ep in group.get("episodes", []):
                sn = ep.get("season_number", -1)
                if sn >= 0:
                    orig_seasons.add(sn)

        if not orig_seasons:
            return {}

        ja_titles: dict[tuple[int, int], str] = {}

        def fetch_ja_season(sn: int) -> dict[tuple[int, int], str]:
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data = self._get(url, {**self._params, "language": "ja"}, cfg=self._cfg)
            result: dict[tuple[int, int], str] = {}
            if data:
                for ep in data.get("episodes", []):
                    en = ep.get("episode_number", 0)
                    name = ep.get("name", "")
                    if name:
                        result[(sn, en)] = name
            return result

        with ThreadPoolExecutor(max_workers=self._cfg.max_workers) as pool:
            futures = {
                pool.submit(fetch_ja_season, sn): sn
                for sn in orig_seasons
            }
            for future in as_completed(futures):
                try:
                    ja_titles.update(future.result())
                except Exception as exc:
                    log.warning("Failed to fetch JA titles: %s", exc)

        return ja_titles

    # ── Main fetch ──────────────────────────────────────────
    def fetch(self) -> dict[int, EpisodeInfo] | None:
        """
        Fetch episode data.  If an episode group ID is configured,
        use that instead of the default season structure.
        """
        # If an episode group is set, use it
        if self._cfg and self._cfg.episode_group_id:
            group_id = self._cfg.episode_group_id
            log.info(
                "Using episode group '%s' for TMDB id=%d",
                group_id, self._series_id,
            )
            result = self.fetch_episode_group_details(group_id)
            if result:
                return result
            log.warning(
                "Episode group '%s' failed — falling back to default.",
                group_id,
            )

        # Default: fetch by season
        return self._fetch_by_season()

    def _fetch_by_season(self) -> dict[int, EpisodeInfo] | None:
        """Fetch episodes using the default TMDB season structure."""
        log.info("TMDB — fetching series id=%s …", self._series_id)
        show = self._get(
            f"{self.BASE}/tv/{self._series_id}",
            self._params,
            cfg=self._cfg,
        )
        if not show:
            return None

        self._has_specials = any(
            s.get("season_number") == 0 for s in show.get("seasons", [])
        )
        seasons = [
            s for s in show.get("seasons", []) if s["season_number"] > 0
        ]
        log.info(
            "Found %d seasons — fetching episodes in parallel …", len(seasons)
        )

        season_data: dict[int, list] = {}
        romaniser = get_romaniser()

        def fetch_season(s: dict) -> tuple[int, list]:
            sn = s["season_number"]
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            ja_params = {**self._params, "language": "ja"}
            data_ja = self._get(url, ja_params, cfg=self._cfg)
            data_en = self._get(url, self._params, cfg=self._cfg)
            eps_ja = {
                e["episode_number"]: e
                for e in (data_ja or {}).get("episodes", [])
            }
            eps_en = (data_en or {}).get("episodes", [])
            merged = []
            for ep in eps_en:
                ep_num = ep["episode_number"]
                ja_ep = eps_ja.get(ep_num, {})
                ja_title = ja_ep.get("name", "")
                if ja_title:
                    ep["name"] = romaniser.to_romaji(ja_title)
                merged.append(ep)
            return sn, sorted(merged, key=lambda x: x["episode_number"])

        with ThreadPoolExecutor(max_workers=self._cfg.max_workers) as pool:
            futures = {pool.submit(fetch_season, s): s for s in seasons}
            for future in as_completed(futures):
                sn, eps = future.result()
                season_data[sn] = eps

        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        for sn in sorted(season_data):
            for ep in season_data[sn]:
                mapping[abs_counter] = EpisodeInfo(
                    absolute=abs_counter,
                    season=sn,
                    episode=ep["episode_number"],
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
        # If we already extracted specials from an episode group, use those
        if self._group_specials:
            log.info(
                "TMDB: using %d special(s) from episode group.",
                len(self._group_specials),
            )
            return self._group_specials

        if not self._has_specials:
            log.info(
                "No specials (Season 0) listed for this series on TMDB."
            )
            return {}

        log.info("TMDB — fetching specials (Season 0) …")
        url = f"{self.BASE}/tv/{self._series_id}/season/0"
        data = self._get(url, self._params, cfg=self._cfg)
        if not data:
            log.info("No specials found on TMDB.")
            return {}

        romaniser = get_romaniser()
        specials: dict[int, EpisodeInfo] = {}
        for ep in data.get("episodes", []):
            ep_num = ep["episode_number"]
            specials[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=0,
                episode=ep_num,
                title=romaniser.to_romaji(
                    ep.get("name", f"Special {ep_num}")
                ),
                air_date=ep.get("air_date", ""),
                overview=ep.get("overview", ""),
                source=self.name,
                is_special=True,
            )

        log.info("TMDB: found %d specials.", len(specials))
        return specials
