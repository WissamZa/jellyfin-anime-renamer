"""
providers.anilist — AniListFetcher (GraphQL API).
"""

import re

import requests

from renamer.config import get_logger
from renamer.providers.base import EpisodeFetcher, EpisodeInfo

log = get_logger()


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
      }
    }
    """

    EPISODES_QUERY = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        streamingEpisodes {
          title
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

        try:
            r = requests.post(
                self.URL,
                json={"query": query, "variables": variables},
                timeout=15,
            )
            if r.status_code == 200:
                result = r.json().get("data", {}).get("Media")
                self._cache[cache_key] = result
                return result
            log.warning("AniList HTTP %s", r.status_code)
        except requests.exceptions.RequestException as e:
            log.error("AniList request failed: %s", e)
        self._cache[cache_key] = None
        return None

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
            name, self._id, romaji, english, native,
        )
        return romaji or english

    def fetch(self) -> dict[int, EpisodeInfo] | None:
        if not self._id:
            return None
        log.info(
            "AniList — fetching episode titles for id=%d …", self._id
        )
        media = self._gql(self.EPISODES_QUERY, {"id": self._id})
        if not media:
            return None

        streaming = media.get("streamingEpisodes", [])
        if not streaming:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        for idx, ep_data in enumerate(streaming, start=1):
            raw = ep_data.get("title", f"Episode {idx}")
            clean = re.sub(r"^Episode\s+\d+\s*[-–]\s*", "", raw).strip()
            mapping[idx] = EpisodeInfo(
                absolute=idx,
                season=1,
                episode=idx,
                title=clean or raw,
                source=self.name,
            )

        log.info("AniList: mapped %d episode titles.", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}
