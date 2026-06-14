"""
providers.kitsu — KitsuFetcher (replaces AniDB).

Uses the Kitsu JSON API to search anime and walk sequel chains
to build multi-season episode mappings.
"""

from typing import Any

from renamer.config import Config, get_logger
from renamer.providers.base import EpisodeFetcher, EpisodeInfo
from renamer.romaniser import anime_title_case, get_romaniser

log = get_logger(__name__)


class KitsuFetcher(EpisodeFetcher):
    """
    Fetches episode data from Kitsu (https://kitsu.io).

    Key feature: walks the sequel chain so that multiple seasons
    of the same anime are mapped with correct season numbers and
    absolute episode counters.
    """

    name = "Kitsu"
    BASE = "https://kitsu.io/api/edge"
    HEADERS = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }

    def __init__(
        self,
        kitsu_id: int | None = None,
        cfg: Config | None = None,
    ):
        super().__init__()
        self._id = kitsu_id
        self._cfg = cfg or Config()

    # ── HTTP helper ────────────────────────────────────────
    def _kitsu_get(
        self,
        url: str,
        params: dict | None = None,
    ) -> dict[str, Any] | None:
        """HTTP GET with caching for Kitsu API."""
        return self._get(
            url,
            params=params,
            cfg=self._cfg,
            headers=self.HEADERS,
        )

    # ── Series search ──────────────────────────────────────
    def find_series(self, name: str) -> tuple[int, str] | None:
        """
        Search Kitsu by name.
        Returns (kitsu_id, romaji_name) or None.
        """
        data = self._kitsu_get(
            f"{self.BASE}/anime",
            {"filter[text]": name, "page[limit]": "5"},
        )
        if not data:
            return None

        results = data.get("data", [])
        if not results:
            log.warning("Kitsu search for '%s' returned no results.", name)
            return None

        top = results[0]
        kitsu_id = int(top["id"])
        titles = top.get("attributes", {}).get("titles", {})
        romaji = titles.get("en_jp") or titles.get("en") or name
        romaji = anime_title_case(romaji)

        log.info("Kitsu search: '%s' -> id=%d romaji='%s'", name, kitsu_id, romaji)
        return kitsu_id, romaji

    # ── Sequel chain ───────────────────────────────────────
    def _walk_sequel_chain(self, start_id: int) -> list[dict[str, Any]]:
        """
        Starting from *start_id*, follow sequel relationships to build
        an ordered list of anime entries (first season -> last).
        Each entry is a dict with 'id', 'attributes', and 'season_index'.
        """
        chain: list[dict[str, Any]] = []
        visited: set[int] = set()
        current_id = start_id

        season_idx = 0
        while current_id and current_id not in visited:
            visited.add(current_id)

            data = self._kitsu_get(
                f"{self.BASE}/anime/{current_id}",
                {"include": "sequels,prequels"},
            )
            if not data:
                break

            anime_data = data.get("data")
            if not anime_data:
                break

            chain.append(
                {
                    "id": int(anime_data["id"]),
                    "attributes": anime_data.get("attributes", {}),
                    "season_index": season_idx,
                }
            )
            season_idx += 1

            # Find next sequel
            sequel_id = self._get_sequel_id(data)
            current_id = sequel_id

        log.info(
            "Kitsu sequel chain: %d anime(s) starting from id=%d",
            len(chain),
            start_id,
        )
        return chain

    def _get_sequel_id(self, data: dict) -> int | None:
        """Extract the first sequel ID from included data or relationships."""
        # Check included entries for sequels
        included = data.get("included", [])
        for _item in included:
            # If this is a sequel relationship entry, check if it links forward
            pass

        # Check relationships directly
        relationships = data.get("data", {}).get("relationships", {})
        sequels = relationships.get("sequels", {}).get("data", [])
        if sequels:
            return int(sequels[0]["id"])

        # Try from included
        for item in included:
            item_type = item.get("type")
            if item_type == "anime":
                # Check if this item's prequel is our current anime
                prequels = item.get("relationships", {}).get("prequels", {}).get("data", [])
                for prequel in prequels:
                    if int(prequel.get("id", 0)) == int(data.get("data", {}).get("id", 0)):
                        return int(item["id"])

        return None

    # ── Episode fetching per anime ─────────────────────────
    def _fetch_episodes_for_anime(self, kitsu_id: int) -> list[dict[str, Any]]:
        """Fetch all episodes for a single Kitsu anime (paginated)."""
        episodes: list[dict[str, Any]] = []
        offset = 0
        limit = 20

        while True:
            data = self._kitsu_get(
                f"{self.BASE}/anime/{kitsu_id}/episodes",
                {"page[limit]": str(limit), "page[offset]": str(offset)},
            )
            if not data:
                break

            batch = data.get("data", [])
            if not batch:
                break

            episodes.extend(batch)

            # Check if there are more pages
            links = data.get("links", {})
            if "next" not in links:
                break

            offset += limit

        return episodes

    # ── Main fetch ─────────────────────────────────────────
    def fetch(self) -> dict[int, EpisodeInfo] | None:
        if not self._id:
            log.error("Kitsu ID is required for fetch().")
            return None

        log.info("Kitsu — fetching episodes for id=%d …", self._id)

        # Walk the sequel chain to get all seasons
        chain = self._walk_sequel_chain(self._id)
        if not chain:
            log.error("Could not build sequel chain for Kitsu id=%d", self._id)
            return None

        romaniser = get_romaniser()
        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1

        for anime_entry in chain:
            season_num = anime_entry["season_index"] + 1  # 1-based seasons
            anime_id = anime_entry["id"]
            attrs = anime_entry["attributes"]
            ep_count = attrs.get("episodeCount") or 0

            log.info(
                "Kitsu season %d: anime id=%d (%d episodes)",
                season_num,
                anime_id,
                ep_count,
            )

            # Fetch episodes for this anime
            raw_episodes = self._fetch_episodes_for_anime(anime_id)
            if not raw_episodes:
                # If API returns nothing, generate placeholder episodes
                for ep_num in range(1, max(ep_count, 1) + 1):
                    mapping[abs_counter] = EpisodeInfo(
                        absolute=abs_counter,
                        season=season_num,
                        episode=ep_num,
                        title=f"Episode {ep_num}",
                        source=self.name,
                    )
                    abs_counter += 1
                continue

            # Sort episodes by number
            ep_list = sorted(
                raw_episodes,
                key=lambda e: e.get("attributes", {}).get("number") or 0,
            )

            for raw_ep in ep_list:
                ep_attrs = raw_ep.get("attributes", {})
                ep_num = ep_attrs.get("number")
                if ep_num is None:
                    continue

                # Get title
                ep_titles = ep_attrs.get("titles", {})
                title = (
                    ep_titles.get("en_jp")
                    or ep_attrs.get("canonicalTitle")
                    or ep_titles.get("en")
                    or f"Episode {ep_num}"
                )

                # Romanise if Japanese
                title = romaniser.to_romaji(title)

                mapping[abs_counter] = EpisodeInfo(
                    absolute=abs_counter,
                    season=season_num,
                    episode=int(ep_num),
                    title=title,
                    source=self.name,
                )
                abs_counter += 1

        log.info("Kitsu: mapped %d episodes across %d season(s).", len(mapping), len(chain))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Kitsu specials — not yet implemented."""
        return {}
