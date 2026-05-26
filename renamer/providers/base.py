"""
providers.base — ABC, data classes, and shared HTTP helper.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

import requests

from renamer.config import Config, get_logger

log = get_logger()


@dataclass
class EpisodeInfo:
    absolute:   int
    season:     int          # 0 = special/OVA
    episode:    int
    title:      str
    air_date:   str  = ""
    overview:   str  = ""
    source:     str  = ""
    is_special: bool = False


@dataclass
class RenameResult:
    original:  str
    renamed:   str
    episode:   EpisodeInfo
    dest_dir:  str  = ""
    success:   bool = False
    error:     str  = ""
    skipped:   bool = False


@dataclass
class EpisodeGroupInfo:
    """Summary of an available TMDB episode group."""
    id:             str
    name:           str
    description:    str
    episode_count:  int
    group_count:    int
    type:           int   # 1=Air Date, 2=Absolute, 3=DVD, 4=Digital, 5=Story Arc, 6=Production, 7=TV

    # Human-readable type label
    TYPE_LABELS: ClassVar[dict[int, str]] = {
        1: "Original Air Date",
        2: "Absolute Order",
        3: "DVD Order",
        4: "Digital Order",
        5: "Story Arc",
        6: "Production Order",
        7: "TV Order",
    }

    @property
    def type_label(self) -> str:
        return self.TYPE_LABELS.get(self.type, f"Unknown ({self.type})")


@dataclass
class SeriesSearchResult:
    """Result from a provider search (name -> ID + title)."""
    provider:      str
    series_id:     int
    series_name:   str
    tmdb_id:       Optional[int] = None
    anilist_id:    Optional[int] = None
    kitsu_id:      Optional[int] = None


class EpisodeFetcher(ABC):
    """Abstract base class for episode metadata fetchers."""

    name: str = "unknown"

    def __init__(self) -> None:
        # Instance-level API response cache (fixes the old _api_cache NameError)
        self._api_cache: dict[tuple, Optional[dict[str, Any]]] = {}

    @abstractmethod
    def fetch(self) -> Optional[dict[int, EpisodeInfo]]: ...

    @abstractmethod
    def fetch_specials(self) -> dict[int, EpisodeInfo]: ...

    def _get(
        self,
        url: str,
        params:  Optional[dict] = None,
        retries: Optional[int]  = None,
        cfg:     Optional[Config] = None,
        headers: Optional[dict] = None,
    ) -> Optional[dict[str, Any]]:
        """HTTP GET with retries, rate-limit handling, and in-memory caching."""
        cache_key = (url, frozenset((params or {}).items()))
        if cache_key in self._api_cache:
            log.debug("Cache hit: %s", url)
            return self._api_cache[cache_key]

        _cfg = cfg or Config()
        attempts = retries or _cfg.RETRY_ATTEMPTS
        result: Optional[dict[str, Any]] = None

        for attempt in range(1, attempts + 1):
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=_cfg.REQUEST_TIMEOUT,
                )
                if r.status_code == 200:
                    result = r.json()
                    break
                if r.status_code == 429:
                    wait = int(
                        r.headers.get("Retry-After", _cfg.RETRY_DELAY * attempt)
                    )
                    log.warning("Rate-limited — waiting %ss …", wait)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    log.info(
                        "HTTP 404 (Not Found) for %s — resource does not exist.",
                        url,
                    )
                    break
                log.warning("HTTP %s for %s", r.status_code, url)
                if 400 <= r.status_code < 500:
                    break
            except requests.exceptions.ConnectionError:
                log.warning(
                    "Connection error (attempt %d/%d) for %s",
                    attempt, attempts, url,
                )
            except requests.exceptions.Timeout:
                log.warning(
                    "Timeout (attempt %d/%d) for %s",
                    attempt, attempts, url,
                )
            except requests.exceptions.RequestException as e:
                log.error("Request failed: %s", e)
                break
            time.sleep(_cfg.RETRY_DELAY)

        self._api_cache[cache_key] = result
        return result

    def clear_cache(self) -> None:
        """Clear the instance API response cache."""
        self._api_cache.clear()
