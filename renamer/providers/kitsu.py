"""
kitsu.py — Kitsu.io JSON API provider.

Free API (no key needed) that provides:
  1. Anime search by name
  2. Multi-season discovery via sequel/prequel relationship chains
  3. Episode lists for each season with titles

Kitsu is especially good for anime with complex season structures
(e.g. Re:Zero which has 5+ separate entries) because each cour/season
gets its own Kitsu entry linked via sequel chains.
"""

import re
from typing import Optional

from renamer.config import log
from renamer.providers.base import EpisodeFetcher, EpisodeInfo, SeriesSearchResult
from renamer.romaniser import _romaniser, anime_title_case


# ── Module-level API cache (fixes the old _api_cache NameError) ──
_api_cache: dict[tuple, Optional[dict]] = {}


class KitsuFetcher(EpisodeFetcher):
    """
    Kitsu.io as a full metadata provider.
    """

    name = "Kitsu"

    API_BASE = "https://kitsu.io/api/edge"
    API_HEADERS = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }

    # Minimum similarity (0-1) to accept a name match
    MIN_SIMILARITY = 0.45

    def __init__(self, kitsu_id: Optional[int] = None):
        self._id = kitsu_id
        self._seasons: Optional[list[tuple[int, str, int]]] = None
        self._specials_cache: dict[int, EpisodeInfo] = {}

    # ── Kitsu API helpers ──────────────────────────────────

    def _kitsu_get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET request to Kitsu API with caching and error handling."""
        global _api_cache
        cache_key = ("kitsu", url, frozenset((params or {}).items()))
        if cache_key in _api_cache:
            log.debug("Kitsu cache hit: %s", url)
            return _api_cache[cache_key]

        result = self._get(
            url,
            params=params,
            headers=self.API_HEADERS,
        )

        _api_cache[cache_key] = result
        if result is None:
            log.warning("Kitsu API returned no data for %s", url)

        return result

    def _fetch_all_pages(self, url: str, params: Optional[dict] = None) -> list[dict]:
        """Fetch all pages of a paginated Kitsu API response."""
        all_data: list[dict] = []
        current_url: Optional[str] = url
        current_params = params
        page = 0

        while current_url and page < 20:  # Safety limit of 20 pages
            data = self._kitsu_get(current_url, current_params)
            if not data:
                break

            items = data.get("data", [])
            all_data.extend(items)

            links = data.get("links", {})
            next_url = links.get("next")
            if next_url and next_url != current_url:
                current_url = next_url
                current_params = None  # next URL already has params
                page += 1
            else:
                break

        return all_data

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Token-set similarity — order-insensitive, handles partial matches."""
        a_tokens = set(a.lower().split())
        b_tokens = set(b.lower().split())
        if not a_tokens or not b_tokens:
            return 0.0
        intersection = a_tokens & b_tokens
        union = a_tokens | b_tokens
        return len(intersection) / len(union)

    # ── Search by name ─────────────────────────────────────

    def find_series(self, name: str) -> Optional[tuple[int, str]]:
        """
        Search Kitsu for an anime by name.
        Returns (kitsu_id, romaji_title) for the best match, or None.
        """
        log.info("Kitsu — searching for '%s' …", name)

        data = self._kitsu_get(
            f"{self.API_BASE}/anime",
            params={
                "filter[text]": name,
                "page[limit]": 10,
            },
        )

        if not data or not data.get("data"):
            log.warning("Kitsu search returned no results for '%s'.", name)
            return None

        best_id: Optional[int] = None
        best_title: Optional[str] = None
        best_score: float = 0.0

        for anime in data["data"]:
            attrs = anime.get("attributes", {})
            titles = attrs.get("titles", {})

            romaji = titles.get("en_jp") or titles.get("en")
            english = titles.get("en")
            slug = attrs.get("slug", "")

            candidates = [t for t in [romaji, english, slug] if t]
            for candidate in candidates:
                score = self._similarity(name, candidate)
                if score > best_score:
                    best_score = score
                    best_id = int(anime["id"])
                    best_title = romaji or english or slug

        if best_id is None or best_score < self.MIN_SIMILARITY:
            log.info(
                "Kitsu found no match for '%s' above threshold (best %.0f%%)",
                name,
                best_score * 100,
            )
            return None

        chosen = anime_title_case(best_title)

        if not self._id:
            self._id = best_id

        log.info(
            "Kitsu find_series '%s' -> id=%d title='%s' (similarity %.0f%%)",
            name, best_id, chosen, best_score * 100,
        )
        return best_id, chosen

    def find_romaji(self, name: str) -> Optional[str]:
        """Search Kitsu by name and return the romaji title."""
        result = self.find_series(name)
        return result[1] if result else None

    # ── Multi-season discovery ─────────────────────────────

    def _discover_seasons(self, kitsu_id: int) -> list[tuple[int, str, int]]:
        """
        Discover all seasons of an anime by following the sequel chain.

        Returns a list of (season_number, title, kitsu_id) tuples ordered
        from Season 1 to the last sequel found.
        """
        seasons: list[tuple[int, str, int]] = []

        first_data = self._kitsu_get(f"{self.API_BASE}/anime/{kitsu_id}")
        if not first_data or not first_data.get("data"):
            return [(1, "Unknown", kitsu_id)]

        attrs = first_data["data"].get("attributes", {})
        titles = attrs.get("titles", {})
        first_title = titles.get("en_jp") or titles.get("en") or attrs.get("slug", "Season 1")
        seasons.append((1, first_title, kitsu_id))

        current_id = kitsu_id
        season_num = 1
        visited = {kitsu_id}

        for _ in range(20):  # Safety limit
            rel_data = self._kitsu_get(
                f"{self.API_BASE}/anime/{current_id}/media-relationships",
                params={"page[limit]": 20, "include": "destination"},
            )
            if not rel_data or not rel_data.get("data"):
                break

            included_map: dict[tuple[str, str], dict] = {}
            for inc in rel_data.get("included", []):
                included_map[(inc.get("type"), inc.get("id"))] = inc

            sequel_id: Optional[int] = None

            for rel in rel_data["data"]:
                rel_attrs = rel.get("attributes", {})
                role = rel_attrs.get("role", "")

                if role.lower() != "sequel":
                    continue

                dest_ref = (
                    rel.get("relationships", {})
                    .get("destination", {})
                    .get("data", {})
                )
                dest_type = dest_ref.get("type")
                dest_id_str = dest_ref.get("id")

                if dest_type != "anime" or not dest_id_str:
                    continue

                sid = int(dest_id_str)
                if sid in visited:
                    continue

                dest_anime = included_map.get(("anime", dest_id_str))
                if dest_anime:
                    d_attrs = dest_anime.get("attributes", {})
                    d_titles = d_attrs.get("titles", {})
                    d_title = (
                        d_titles.get("en_jp")
                        or d_titles.get("en")
                        or d_attrs.get("slug", f"Season {season_num + 1}")
                    )
                    season_num += 1
                    seasons.append((season_num, d_title, sid))
                    sequel_id = sid
                    break
                else:
                    season_num += 1
                    sequel_info = self._kitsu_get(f"{self.API_BASE}/anime/{sid}")
                    if sequel_info and sequel_info.get("data"):
                        s_attrs = sequel_info["data"].get("attributes", {})
                        s_titles = s_attrs.get("titles", {})
                        s_title = (
                            s_titles.get("en_jp")
                            or s_titles.get("en")
                            or s_attrs.get("slug", f"Season {season_num}")
                        )
                        seasons.append((season_num, s_title, sid))
                    else:
                        seasons.append((season_num, f"Season {season_num}", sid))
                    sequel_id = sid
                    break

            if sequel_id is None:
                break

            visited.add(sequel_id)
            current_id = sequel_id

        log.info(
            "Kitsu: discovered %d season(s) — %s",
            len(seasons),
            ", ".join(f"S{s[0]}: {s[1]} (id={s[2]})" for s in seasons),
        )
        return seasons

    # ── Episode fetching ───────────────────────────────────

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        if not self._id:
            return None

        log.info("Kitsu — fetching episode data for id=%d …", self._id)

        if self._seasons is None:
            self._seasons = self._discover_seasons(self._id)

        if not self._seasons:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        specials: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        special_counter = 1

        for season_num, season_title, season_kitsu_id in self._seasons:
            log.info(
                "Kitsu — fetching episodes for S%02d '%s' (id=%d) …",
                season_num, season_title, season_kitsu_id,
            )

            episodes = self._fetch_all_pages(
                f"{self.API_BASE}/anime/{season_kitsu_id}/episodes",
                params={"page[limit]": 20, "sort": "number"},
            )

            if not episodes:
                log.warning(
                    "Kitsu: no episodes returned for S%02d (id=%d) — skipping.",
                    season_num, season_kitsu_id,
                )
                continue

            valid_eps = []
            for ep in episodes:
                attrs = ep.get("attributes", {})
                ep_num = attrs.get("number")
                if ep_num is not None:
                    try:
                        valid_eps.append((int(ep_num), ep))
                    except (ValueError, TypeError):
                        continue

            valid_eps.sort(key=lambda x: x[0])

            for ep_num, ep in valid_eps:
                attrs = ep.get("attributes", {})
                title = attrs.get("titles", {})
                ep_title = title.get("en_jp") or title.get("en") or f"Episode {ep_num}"

                ep_title = _romaniser.to_romaji(ep_title)
                ep_title = anime_title_case(ep_title)

                is_special = attrs.get("category", "") == "special"

                air_date = ""
                aired = attrs.get("aired")
                if aired:
                    air_date = aired[:10] if isinstance(aired, str) else ""

                if is_special:
                    specials[special_counter] = EpisodeInfo(
                        absolute=special_counter,
                        season=0,
                        episode=special_counter,
                        title=ep_title,
                        air_date=air_date,
                        source=self.name,
                        is_special=True,
                    )
                    special_counter += 1
                else:
                    mapping[abs_counter] = EpisodeInfo(
                        absolute=abs_counter,
                        season=season_num,
                        episode=ep_num,
                        title=ep_title,
                        air_date=air_date,
                        source=self.name,
                        is_special=False,
                    )
                    abs_counter += 1

        self._specials_cache = specials

        log.info(
            "Kitsu: mapped %d regular episodes across %d season(s), %d specials.",
            len(mapping), len(self._seasons), len(specials),
        )
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Return cached specials from the last fetch() call."""
        return self._specials_cache
