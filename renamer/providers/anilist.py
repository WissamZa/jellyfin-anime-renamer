"""
providers.anilist — AniListFetcher (GraphQL API).

Each AniList ``Media`` entry corresponds to exactly one season/cour of a
show, so mapping a resolved entry to ``season=1`` is correct by
construction — cross-season shows are separate Media entries (resolve a
different AniList ID per season).
"""

import re
import time

import requests

from renamer.config import get_logger
from renamer.providers.base import EpisodeFetcher, EpisodeInfo, _get_shared_session

log = get_logger(__name__)


class AniListFetcher(EpisodeFetcher):
    """
    Fetches series and episode data from AniList's free GraphQL API.
    Returns the official romaji title directly — no mechanical conversion.
    Used both as a fallback fetcher and as a romaji cross-reference.
    """

    name = "AniList"
    URL = "https://graphql.anilist.co"

    SERIES_QUERY = """
    query ($id: Int, $search: String) {
      Media(id: $id, search: $search, type: ANIME) {
        id
        title {
          romaji
          english
          native
        }
        episodes
        format
        season
        seasonYear
        countryOfOrigin
      }
    }
    """

    EPISODES_QUERY = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        episodes
        streamingEpisodes {
          title
        }
      }
    }
    """

    SEARCH_QUERY = """
    query ($search: String, $perPage: Int) {
      Page(page: 1, perPage: $perPage) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
          id
          title {
            romaji
            english
            native
          }
          episodes
          format
          season
          seasonYear
          description(asHtml: false)
          countryOfOrigin
        }
      }
    }
    """

    def __init__(self, anime_id: int | None = None):
        super().__init__()
        self._id = anime_id

    def _gql(self, query: str, variables: dict) -> dict | None:
        # AniList uses POST, so cache by query + variables
        cache_key = ("anilist_gql", query, frozenset(variables.items()))
        if cache_key in self._cache:
            log.debug("AniList cache hit for variables: %s", variables)
            return self._cache[cache_key]

        result: dict | None = None
        session = _get_shared_session()
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                r = session.post(
                    self.URL,
                    json={"query": query, "variables": variables},
                    timeout=15,
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 200:
                    payload = r.json()
                    for err in payload.get("errors") or []:
                        log.warning("AniList GraphQL error: %s", err.get("message"))
                    data = payload.get("data")
                    result = data.get("Media") if isinstance(data, dict) else None
                    break
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 2 * attempt))
                    log.warning("AniList rate-limited — retrying in %ss …", wait)
                    time.sleep(wait)
                    continue
                log.warning("AniList HTTP %s", r.status_code)
                break  # non-transient client errors won't recover on retry
            except requests.exceptions.RequestException as e:
                log.error("AniList request failed: %s", e)
                break

        self._cache[cache_key] = result
        return result

    def find_id(self, name: str) -> int | None:
        """Search AniList by name and return the AniList series ID."""
        media = self._gql(self.SERIES_QUERY, {"search": name})
        return media.get("id") if media else None

    def find_romaji(self, name: str) -> str | None:
        """
        Search AniList by name and return the official romaji title.
        Also caches the resolved AniList ID on self._id for later use.
        """
        media = self._gql(self.SERIES_QUERY, {"search": name})
        if not media:
            return None
        if not self._id:
            self._id = media.get("id")
        titles = media.get("title", {})
        romaji = titles.get("romaji")
        english = titles.get("english")
        native = titles.get("native")
        log.info(
            "AniList title lookup '%s' -> id=%s romaji='%s' english='%s' native='%s'",
            name,
            self._id,
            romaji,
            english,
            native,
        )
        return romaji or english

    def search(self, name: str, limit: int = 10) -> list[dict]:
        """Search AniList and return raw Page media results."""
        resp = self._gql(self.SEARCH_QUERY, {"search": name, "perPage": limit})
        if not resp:
            return []
        return resp.get("media") or []

    @staticmethod
    def to_search_dicts(results: list[dict]) -> list[dict]:
        """
        Map raw AniList media entries to the TMDB-shaped dicts consumed by
        AnimeRenamer._pick_series_from_results / registry search factories.
        """
        mapped: list[dict] = []
        for m in results:
            titles = m.get("title") or {}
            romaji = titles.get("romaji") or ""
            english = titles.get("english") or ""
            native = titles.get("native") or ""
            year = m.get("seasonYear")
            description = re.sub(r"<[^>]+>", "", m.get("description") or "").strip()

            mapped.append(
                {
                    "id": m.get("id"),
                    "name": english or romaji,
                    "original_name": native or romaji,
                    "romaji": romaji,
                    "overview": description[:200],
                    "first_air_date": f"{year}-01-01" if year else "",
                    "origin_country": ["JP"] if m.get("countryOfOrigin") == "JP" else [],
                    "episodes": m.get("episodes"),
                    "format": m.get("format"),
                    "anilist_id": m.get("id"),
                }
            )
        return mapped

    def fetch(self) -> dict[int, EpisodeInfo] | None:
        if not self._id:
            return None
        log.info("AniList — fetching episode data for id=%d …", self._id)
        media = self._gql(self.EPISODES_QUERY, {"id": self._id})
        if not media:
            return None

        streaming = media.get("streamingEpisodes") or []
        count = media.get("episodes") or 0
        total = max(len(streaming), count)
        if total == 0:
            log.warning(
                "AniList: id=%d has no episode count and no streaming episodes", self._id
            )
            return None

        mapping: dict[int, EpisodeInfo] = {}
        for idx in range(1, total + 1):
            raw = streaming[idx - 1].get("title", "") if idx <= len(streaming) else ""
            clean = re.sub(r"^Episode\s+\d+\s*[-–]\s*", "", raw).strip() if raw else ""
            mapping[idx] = EpisodeInfo(
                absolute=idx,
                season=1,
                episode=idx,
                title=clean or raw or f"Episode {idx}",
                source=self.name,
            )

        log.info("AniList: mapped %d episode(s).", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}
