"""
renamer.icons.fetcher
=====================
Fetch poster/cover-image URLs from metadata providers (TMDB, AniList, Kitsu).

Each provider returns a direct image URL that can be downloaded and saved
as a folder icon.  Results are cached in-memory per session so repeated
calls for the same series don't hit the API again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from renamer.config import Config, Provider, get_logger
from renamer.providers.base import EpisodeFetcher

if TYPE_CHECKING:
    from renamer.providers.base import EpisodeInfo

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data class for a resolved poster URL
# ---------------------------------------------------------------------------


@dataclass
class PosterResult:
    """A resolved poster image URL with metadata about its source."""

    url: str
    provider: str
    width: int = 0
    height: int = 0


# ---------------------------------------------------------------------------
# TMDB poster fetcher
# ---------------------------------------------------------------------------


def fetch_tmdb_poster(cfg: Config) -> PosterResult | None:
    """
    Fetch the poster URL for the configured series from TMDB.

    Uses the ``/tv/{id}/images`` endpoint with ``include_image_language=en,null``
    so that both the primary poster and language-neutral posters are returned.
    Falls back to the ``poster_path`` field on the series detail endpoint.
    """
    if not cfg.tmdb_api_key or not cfg.tmdb_series_id:
        return None

    params = {"api_key": cfg.tmdb_api_key}

    # Try /tv/{id}/images first — gives us size metadata
    helper = _IconGetHelper()
    data = helper._get(
        f"https://api.themoviedb.org/3/tv/{cfg.tmdb_series_id}/images",
        {**params, "include_image_language": "en,null"},
        cfg=cfg,
    )
    if data:
        posters = data.get("posters", [])
        if posters:
            # Pick the highest-rated or first poster
            best = sorted(posters, key=lambda p: p.get("vote_average", 0), reverse=True)[0]
            path = best.get("file_path", "")
            if path:
                return PosterResult(
                    url=f"https://image.tmdb.org/t/p/w500{path}",
                    provider="tmdb",
                    width=best.get("width", 500),
                    height=best.get("height", 750),
                )

    # Fallback: use the poster_path from the series detail
    show = helper._get(
        f"https://api.themoviedb.org/3/tv/{cfg.tmdb_series_id}",
        params,
        cfg=cfg,
    )
    if show and show.get("poster_path"):
        return PosterResult(
            url=f"https://image.tmdb.org/t/p/w500{show['poster_path']}",
            provider="tmdb",
        )

    log.info("TMDB: no poster found for series id=%s", cfg.tmdb_series_id)
    return None


# ---------------------------------------------------------------------------
# AniList poster fetcher
# ---------------------------------------------------------------------------

_ANILIST_POSTER_QUERY = """
query ($id: Int, $search: String) {
  Media(id: $id, search: $search, type: ANIME) {
    id
    coverImage {
      large
      medium
      extraLarge
    }
  }
}
"""


def fetch_anilist_poster(cfg: Config) -> PosterResult | None:
    """
    Fetch the poster URL for the configured series from AniList.

    AniList provides cover images in multiple sizes: extraLarge, large, medium.
    We prefer extraLarge > large > medium.
    """
    import requests as _requests

    variables: dict = {}
    if cfg.anilist_id:
        variables["id"] = cfg.anilist_id
    elif cfg.series_name:
        variables["search"] = cfg.series_name
    else:
        return None

    try:
        r = _requests.post(
            "https://graphql.anilist.co",
            json={"query": _ANILIST_POSTER_QUERY, "variables": variables},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("AniList poster: HTTP %s", r.status_code)
            return None

        media = r.json().get("data", {}).get("Media")
        if not media:
            return None

        cover = media.get("coverImage", {})
        # Prefer highest quality
        url = cover.get("extraLarge") or cover.get("large") or cover.get("medium")
        if url:
            return PosterResult(url=url, provider="anilist")

    except Exception as exc:
        log.warning("AniList poster fetch failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Kitsu poster fetcher
# ---------------------------------------------------------------------------


def fetch_kitsu_poster(cfg: Config) -> PosterResult | None:
    """
    Fetch the poster URL for the configured series from Kitsu.

    Kitsu provides poster images at various sizes in the anime attributes.
    """
    kitsu_id = cfg.kitsu_id
    if not kitsu_id:
        return None

    helper = _IconGetHelper()
    headers = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }
    data = helper._get(
        f"https://kitsu.io/api/edge/anime/{kitsu_id}",
        headers=headers,
        cfg=cfg,
    )
    if not data:
        return None

    anime = data.get("data", {})
    attrs = anime.get("attributes", {})
    poster = attrs.get("posterImage", {})

    # Kitsu provides: original, large, medium, small, tiny
    url = poster.get("original") or poster.get("large") or poster.get("medium")
    if url:
        return PosterResult(
            url=url,
            provider="kitsu",
            width=poster.get("meta", {}).get("dimensions", {}).get("original", {}).get("width", 0),
            height=poster.get("meta", {})
            .get("dimensions", {})
            .get("original", {})
            .get("height", 0),
        )

    log.info("Kitsu: no poster found for anime id=%s", kitsu_id)
    return None


# ---------------------------------------------------------------------------
# Unified fetch: try all providers, return the best result
# ---------------------------------------------------------------------------


def fetch_poster(cfg: Config) -> PosterResult | None:
    """
    Try to fetch a poster URL using the configured provider first,
    then fall back to the other providers if the primary one fails.

    Provider priority:
      1. The active provider (cfg.provider)
      2. The other providers as fallbacks (TMDB -> AniList -> Kitsu)
    """
    # Build the ordered list of fetch functions to try
    fetch_fns = {
        Provider.TMDB: fetch_tmdb_poster,
        Provider.AniList: fetch_anilist_poster,
        Provider.Kitsu: fetch_kitsu_poster,
    }

    # Try the active provider first
    primary = fetch_fns.get(cfg.provider)
    if primary:
        result = primary(cfg)
        if result:
            log.info(
                "Poster found via %s (primary): %s",
                result.provider,
                result.url,
            )
            return result

    # Try fallback providers
    for provider, fn in fetch_fns.items():
        if provider == cfg.provider:
            continue  # already tried
        try:
            result = fn(cfg)
            if result:
                log.info(
                    "Poster found via %s (fallback): %s",
                    result.provider,
                    result.url,
                )
                return result
        except Exception as exc:
            log.debug("Fallback poster fetch (%s) failed: %s", provider.value, exc)

    log.warning("No poster found for '%s' from any provider.", cfg.series_name)
    return None


# ---------------------------------------------------------------------------
# Minimal EpisodeFetcher subclass just for its HTTP _get helper
# ---------------------------------------------------------------------------


class _IconGetHelper(EpisodeFetcher):
    """Minimal fetcher subclass to reuse the shared _get() HTTP helper."""

    name = "IconFetchHelper"

    def fetch(self) -> dict[int, EpisodeInfo] | None:  # noqa: D401
        """Not used — exists only to satisfy the abstract base class."""
        return None

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}
