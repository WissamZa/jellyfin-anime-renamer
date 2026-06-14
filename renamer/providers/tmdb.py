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

log = get_logger(__name__)


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

    def search_multi(
        self,
        name: str,
        limit: int = 10,
    ) -> list[dict]:
        """
        Search TMDB for *name* and return up to *limit* results.

        Each result is a dict with keys:
          - ``id``: TMDB series ID
          - ``name``: English name on TMDB
          - ``original_name``: Original language name
          - ``ja_name``: Japanese name (fetched separately, may be empty)
          - ``romaji``: Best romaji name (AniList cross-ref > pykakasi > English)
          - ``overview``: Short description
          - ``first_air_date``: First air date
          - ``origin_country``: List of origin country codes

        This is used by the interactive picker when the auto-search
        needs user disambiguation.
        """
        data = self._fetcher._get(
            f"{self.BASE}/search/tv",
            {**self._params, "query": name, "language": "en-US"},
        )
        if not data:
            return []

        raw_results = data.get("results", [])
        if not raw_results:
            log.warning("TMDB multi-search for '%s' returned no results.", name)
            return []

        romaniser = get_romaniser()
        enriched: list[dict] = []

        for item in raw_results[:limit]:
            tmdb_id = item.get("id")
            if not tmdb_id:
                continue
            en_name = item.get("name", "")
            orig_name = item.get("original_name", "")

            # Fetch Japanese name (best-effort, don't fail on error)
            ja_name = self._fetch_japanese_name(tmdb_id) or orig_name or en_name
            tmdb_romaji = romaniser.to_romaji(ja_name) if ja_name else en_name

            # Cross-reference with AniList for best romaji
            best_romaji = tmdb_romaji
            try:
                from renamer.providers.romaji_resolver import RomajiResolver

                resolver = RomajiResolver()
                best_romaji = resolver.resolve(
                    search_name=en_name or orig_name,
                    tmdb_romaji=tmdb_romaji,
                    english_name=en_name,
                )
            except Exception as exc:
                log.debug("RomajiResolver failed for id=%d: %s", tmdb_id, exc)

            enriched.append(
                {
                    "id": tmdb_id,
                    "name": en_name,
                    "original_name": orig_name,
                    "ja_name": ja_name,
                    "romaji": best_romaji,
                    "overview": (item.get("overview") or "")[:200],
                    "first_air_date": item.get("first_air_date", ""),
                    "origin_country": item.get("origin_country", []),
                }
            )

        return enriched

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
        self._original_se_map: dict[tuple[int, int], EpisodeInfo] = {}
        self._group_arc_names: dict[int, str] = {}

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
                all_titles.append(
                    {
                        "title": title,
                        "type": t.get("type", ""),
                        "iso_3166_1": t.get("iso_3166_1", ""),
                    }
                )

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
                all_titles.append(
                    {
                        "title": title,
                        "type": "translation",
                        "iso_3166_1": iso,
                    }
                )

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
                    all_titles.append(
                        {
                            "title": title,
                            "type": "primary" if field == "name" else "original",
                            "iso_3166_1": show.get("origin_country", [""])[0]
                            if field == "original_name"
                            else "",
                        }
                    )

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
                    all_titles.append(
                        {
                            "title": ja_name,
                            "type": "japanese",
                            "iso_3166_1": "JP",
                        }
                    )

        log.info(
            "TMDB: found %d alternative title(s) for id=%d",
            len(all_titles),
            self._series_id,
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
            groups.append(
                EpisodeGroupInfo(
                    id=g.get("id", ""),
                    name=g.get("name", "Unnamed"),
                    description=g.get("description", ""),
                    episode_count=g.get("episode_count", 0),
                    group_count=g.get("group_count", 0),
                    type=g.get("type", 0),
                )
            )

        log.info(
            "TMDB: found %d episode group(s) for id=%d",
            len(groups),
            self._series_id,
        )
        return groups

    def fetch_episode_group_details(
        self,
        group_id: str,
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
            group_name,
            len(groups),
            data.get("type", 0),
        )

        romaniser = get_romaniser()
        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1  # counts only regular episodes (fallback)
        special_counter = 1  # counts only specials
        self._group_specials = {}

        # Determine title language preference
        use_english = getattr(self._cfg, "episode_title_lang", "romaji") == "english"

        # Fetch Japanese titles for each original season to merge
        # (episode groups only return English by default)
        ja_titles = self._prefetch_ja_titles_for_groups(groups)

        # Build a mapping from original (season_number, episode_number) to
        # EpisodeInfo — this allows file matching by original numbering when
        # the group's season/episode renumbering doesn't match filenames.
        self._original_se_map: dict[tuple[int, int], EpisodeInfo] = {}

        # Track the sequential season number for non-special groups
        # (1, 2, 3, ...) regardless of what the group name says.
        # Group names like "East Blue (1-61)" or "Alabasta (62-143)"
        # should NOT be used as season numbers — the number in parentheses
        # is the episode range, not the season number.
        regular_season_counter = 0

        # Also build arc names from group names for season folder naming
        self._group_arc_names: dict[int, str] = {}

        # ── First pass: parse group names to determine absolute episode start ──
        # Group names often contain the absolute episode range, e.g.:
        #   "East Blue (1-61)" → starts at absolute episode 1
        #   "Alabasta (62-143)" → starts at absolute episode 62
        #   "Egghead (1089-1155)" → starts at absolute episode 1089
        #   "Elbaph (1156-current)" → starts at absolute episode 1156
        # This is critical because TMDB episode groups may NOT be listed in
        # episode order, so a simple sequential counter would assign wrong
        # absolute numbers.  For example, One Piece's Crunchyroll group has
        # "Egghead (1089-1155)" listed before "Water Seven (229-263)".
        #
        # NOTE: The episode count in a group can differ significantly from
        # the nominal range.  For example, One Piece's Crunchyroll group has
        # "Egghead (1089-1155)" with 79 episodes (range suggests 67) because
        # Crunchyroll includes recaps/specials within the arc that fall
        # outside the numbered range.  We therefore always trust the range
        # start number and never reject it based on count mismatch.
        _EP_RANGE_RE = re.compile(
            r"\(\s*(\d+)\s*[-\u2013\u2014]\s*(\d+|current|ongoing)\s*\)",
            re.IGNORECASE,
        )

        def _parse_group_start(name: str, episode_count: int) -> int | None:
            """Extract the starting absolute episode number from a group name.

            Returns the start of the range (e.g. 1089 from "Egghead (1089-1155)"),
            or None if the group name doesn't contain a range.

            The episode_count parameter is accepted for logging only — we no
            longer reject a valid range based on count mismatch, because
            Crunchyroll/other groups routinely include extra episodes (recaps,
            specials) beyond the nominal range.
            """
            m = _EP_RANGE_RE.search(name)
            if m:
                start = int(m.group(1))
                # Basic sanity: start must be a positive number
                if start > 0:
                    return start
            return None

        # Check if the episode group uses absolute numbering in its episode_number fields.
        # We do this by checking if any non-special group (season_num > 0) has its first episode
        # starting at an episode number other than 1.
        has_absolute_group_nums = False
        non_special_groups = []
        for g in groups:
            gname = g.get("name", "")
            if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
                continue
            eps = g.get("episodes", [])
            if {ep.get("season_number", -1) for ep in eps} == {0}:
                continue
            non_special_groups.append(g)

        if len(non_special_groups) > 1:
            for g in non_special_groups[1:]:
                eps = g.get("episodes", [])
                if eps and eps[0].get("episode_number", 0) > 1:
                    has_absolute_group_nums = True
                    break

        for group in groups:
            episodes = group.get("episodes", [])
            gname = group.get("name", "").strip()

            # ── Determine season number ──────────────────────
            # Always use sequential numbering for non-special groups.
            # The group's "order" field is the 0-based position of
            # this sub-group within the episode group, but it may not
            # be reliable (some groups have order=None).  We use our
            # own counter instead.
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
                    # Use sequential season number — do NOT extract
                    # numbers from the group name (e.g. "Alabasta (62-143)"
                    # does NOT mean season 62).
                    regular_season_counter += 1
                    season_num = regular_season_counter

            # Store the group name as the arc name for this season
            if not is_specials_group and gname:
                self._group_arc_names[season_num] = gname

            # Determine absolute episode start for this group
            group_abs_start = _parse_group_start(gname, len(episodes))

            log.info(
                "  Group '%s' (season %d%s): %d episodes%s",
                gname,
                season_num,
                ", specials" if is_specials_group else "",
                len(episodes),
                f", abs range {group_abs_start}-{group_abs_start + len(episodes) - 1}"
                if group_abs_start
                else "",
            )

            for i, ep in enumerate(episodes):
                # Episode position within this group (1-based).
                # TMDB's `order` field is 0-based, so we use enumerate
                # instead of the buggy `order or episode_number` logic.
                ep_order = i + 1

                # Extract original TMDB season/episode BEFORE computing
                # actual_abs so we can use orig_ep for absolute numbering.
                orig_season = ep.get("season_number", 0)
                orig_ep = ep.get("episode_number", 0)

                # Compute the actual absolute episode number.
                # Strategy:
                #   1. If the group uses absolute numbering generally, use orig_ep directly.
                #   2. If the group has a range AND orig_ep looks like an
                #      absolute number (orig_ep >= group_abs_start), use
                #      orig_ep directly.  This handles shows like One Piece
                #      where TMDB stores absolute episode numbers.
                #   3. If the group has a range but orig_ep is per-season
                #      (small number), use group_abs_start + position.
                #   4. If no range, fall back to the sequential counter.
                if has_absolute_group_nums:
                    actual_abs = orig_ep
                elif group_abs_start is not None:
                    actual_abs = orig_ep if orig_ep >= group_abs_start else group_abs_start + i
                else:
                    actual_abs = abs_counter

                # Warn if actual_abs collides with an existing mapping
                # (can happen when groups overlap or abs_counter goes
                # wrong due to out-of-order groups without ranges).
                if actual_abs in mapping and not is_specials_group:
                    log.warning(
                        "Absolute number %d already mapped (group '%s', "
                        "position %d) — overwriting.  This usually means "
                        "the group_abs_start for a group without a range "
                        "name is incorrect.",
                        actual_abs,
                        gname,
                        i,
                    )

                ep_title = ep.get("name", f"Episode {actual_abs}")

                # Try to find Japanese title from our prefetch
                ja_title = ja_titles.get((orig_season, orig_ep), "")
                if ja_title and not use_english:
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
                        absolute=actual_abs,
                        season=season_num,
                        episode=ep_order,
                        title=ep_title,
                        air_date=ep.get("air_date", ""),
                        overview=ep.get("overview", ""),
                        source=f"{self.name} (Group: {group_name})",
                        is_special=False,
                    )
                    mapping[actual_abs] = info
                    # Also index by original (season, episode) so we can
                    # match files named with the original TMDB numbering.
                    self._original_se_map[(orig_season, orig_ep)] = info
                    abs_counter += 1

        if self._group_specials:
            self._has_specials = True

        log.info(
            "Episode group '%s': mapped %d episodes, %d specials.",
            group_name,
            len(mapping),
            len(self._group_specials),
        )
        return mapping

    def _prefetch_ja_titles_for_groups(
        self,
        groups: list[dict],
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
            futures = {pool.submit(fetch_ja_season, sn): sn for sn in orig_seasons}
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
                group_id,
                self._series_id,
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

        self._has_specials = any(s.get("season_number") == 0 for s in show.get("seasons", []))
        seasons = [s for s in show.get("seasons", []) if s["season_number"] > 0]
        log.info("Found %d seasons — fetching episodes in parallel …", len(seasons))

        # Determine title language preference
        use_english = getattr(self._cfg, "episode_title_lang", "romaji") == "english"

        season_data: dict[int, list] = {}
        romaniser = get_romaniser()

        def fetch_season(s: dict) -> tuple[int, list]:
            sn = s["season_number"]
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data_en = self._get(url, self._params, cfg=self._cfg)
            eps_en = (data_en or {}).get("episodes", [])
            merged = []

            if use_english:
                # English titles — just use the English API response as-is
                for ep in eps_en:
                    merged.append(ep)
            else:
                # Romaji titles — fetch Japanese and romanise
                ja_params = {**self._params, "language": "ja"}
                data_ja = self._get(url, ja_params, cfg=self._cfg)
                eps_ja = {e["episode_number"]: e for e in (data_ja or {}).get("episodes", [])}
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
            log.info("No specials (Season 0) listed for this series on TMDB.")
            return {}

        log.info("TMDB — fetching specials (Season 0) …")
        url = f"{self.BASE}/tv/{self._series_id}/season/0"
        data = self._get(url, self._params, cfg=self._cfg)
        if not data:
            log.info("No specials found on TMDB.")
            return {}

        # Determine title language preference
        use_english = getattr(self._cfg, "episode_title_lang", "romaji") == "english"

        romaniser = get_romaniser()
        specials: dict[int, EpisodeInfo] = {}
        for ep in data.get("episodes", []):
            ep_num = ep["episode_number"]
            ep_title = ep.get("name", f"Special {ep_num}")
            if not use_english:
                ep_title = romaniser.to_romaji(ep_title)
            specials[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=0,
                episode=ep_num,
                title=ep_title,
                air_date=ep.get("air_date", ""),
                overview=ep.get("overview", ""),
                source=self.name,
                is_special=True,
            )

        log.info("TMDB: found %d specials.", len(specials))
        return specials
