"""
anilist.py — AniList GraphQL API provider.

Free, unauthenticated API that provides:
  1. Series search with human-curated romaji titles
  2. Episode titles via streaming episodes list
  3. Airing schedule as a fallback for episode counts

No API key required.
"""

import datetime
import re
from typing import Optional

from renamer.config import log
from renamer.providers.base import EpisodeFetcher, EpisodeInfo, SeriesSearchResult
from renamer.romaniser import anime_title_case


class AniListFetcher(EpisodeFetcher):
    """
    Fetches series and episode data from AniList's free GraphQL API.
    Returns the official romaji title directly — no mechanical conversion.
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
        status
        startDate { year month day }
        endDate { year month day }
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

    AIRING_QUERY = """
    query ($id: Int, $page: Int) {
      Media(id: $id, type: ANIME) {
        id
        airingSchedule(page: $page, perPage: 50) {
          pageInfo { hasNextPage }
          nodes {
            episode
            airingAt
          }
        }
      }
    }
    """

    def __init__(self, anime_id: Optional[int] = None):
        self._id = anime_id

    def _gql(self, query: str, variables: dict) -> Optional[dict]:
        try:
            r = self._post(
                self.URL,
                json_data={"query": query, "variables": variables},
            )
            if r:
                return r.get("data", {}).get("Media")
            return None
        except Exception as e:
            log.error("AniList request failed: %s", e)
            return None

    def find_id(self, name: str) -> Optional[int]:
        """Search AniList by name and return the AniList series ID."""
        media = self._gql(self.SERIES_QUERY, {"search": name})
        return media.get("id") if media else None

    def find_romaji(self, name: str) -> Optional[str]:
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

    def find_series(self, name: str) -> Optional[tuple[int, str]]:
        """
        Search AniList by name and return (anilist_id, romaji_name).
        """
        media = self._gql(self.SERIES_QUERY, {"search": name})
        if not media:
            log.warning("AniList search for '%s' returned no results.", name)
            return None

        anilist_id = media.get("id")
        if not anilist_id:
            return None

        if not self._id:
            self._id = anilist_id

        titles = media.get("title", {})
        romaji = titles.get("romaji")
        english = titles.get("english")
        native = titles.get("native")

        chosen = romaji or english
        if not chosen:
            return None

        chosen = anime_title_case(chosen)

        log.info(
            "AniList find_series '%s' -> id=%d romaji='%s' english='%s' native='%s' -> '%s'",
            name, anilist_id, romaji, english, native, chosen,
        )
        return anilist_id, chosen

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        if not self._id:
            return None
        log.info("AniList — fetching episode titles for id=%d …", self._id)
        media = self._gql(self.EPISODES_QUERY, {"id": self._id})
        if not media:
            return None

        streaming = media.get("streamingEpisodes", [])
        if not streaming:
            log.info(
                "AniList: no streaming episodes found for id=%d — using airing schedule",
                self._id,
            )
            return self._fetch_from_airing_schedule()

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

    def _fetch_from_airing_schedule(self) -> Optional[dict[int, EpisodeInfo]]:
        """
        Fall back to the airing schedule when streamingEpisodes is empty.
        This provides episode numbers and air dates but no titles.
        """
        page = 1
        all_nodes: list[dict] = []

        while True:
            media = self._gql(self.AIRING_QUERY, {"id": self._id, "page": page})
            if not media:
                break
            schedule = media.get("airingSchedule", {})
            nodes = schedule.get("nodes", [])
            all_nodes.extend(nodes)

            has_next = schedule.get("pageInfo", {}).get("hasNextPage", False)
            if not has_next:
                break
            page += 1

        if not all_nodes:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        for node in all_nodes:
            ep_num = node.get("episode", 0)
            if ep_num <= 0:
                continue
            airing_at = node.get("airingAt", 0)
            air_date = ""
            if airing_at:
                air_date = datetime.datetime.fromtimestamp(airing_at).strftime("%Y-%m-%d")
            mapping[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=1,
                episode=ep_num,
                title=f"Episode {ep_num}",
                air_date=air_date,
                source=self.name,
            )

        log.info("AniList: mapped %d episodes from airing schedule.", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}
