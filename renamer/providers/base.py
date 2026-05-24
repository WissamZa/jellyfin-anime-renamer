"""
base.py — Base classes for all metadata providers.

Every provider must subclass EpisodeFetcher and implement:
  - fetch()          -> dict[int, EpisodeInfo]  (absolute -> episode info)
  - fetch_specials() -> dict[int, EpisodeInfo]  (special number -> info)

Optionally, a provider can also implement a search function:
  - find_series(name) -> SeriesSearchResult | None

The shared _get() method provides retry logic with rate-limit handling.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests

from renamer.config import Config, log


@dataclass
class SeriesSearchResult:
    """
    Uniform result from any provider's series search.

    This decouples the search result from specific provider IDs —
    each provider stores its own ID in provider_id, and the
    renamer engine knows how to map it to the right Config field.
    """
    provider_id: int          # The provider-specific series ID
    series_name: str          # Best title for renaming (romaji preferred)
    provider_name: str        # Human-readable provider name (for logging)


class EpisodeFetcher(ABC):
    """
    Abstract base for all episode metadata fetchers.

    Subclasses must implement:
      - fetch() -> Optional[dict[int, EpisodeInfo]]
      - fetch_specials() -> dict[int, EpisodeInfo]

    Optionally override:
      - find_series(name) -> Optional[SeriesSearchResult]
    """
    name: str = "unknown"

    @abstractmethod
    def fetch(self) -> Optional[dict]:
        """
        Fetch regular episode data.

        Returns a mapping of absolute episode number -> EpisodeInfo,
        or None if the fetch fails entirely.
        """
        ...

    @abstractmethod
    def fetch_specials(self) -> dict:
        """
        Fetch special/OVA episode data.

        Returns a mapping of special episode number -> EpisodeInfo.
        Return empty dict if no specials exist or the provider
        doesn't support specials.
        """
        ...

    def find_series(self, name: str) -> Optional[SeriesSearchResult]:
        """
        Search for a series by name.

        Returns a SeriesSearchResult if found, or None.
        Not all providers support search — the default raises
        NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.name} provider does not implement find_series()"
        )

    # ── Shared HTTP helper with retry logic ──────────────────

    @staticmethod
    def _get(
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: Optional[int] = None,
        cfg: Optional[Config] = None,
    ) -> Optional[dict]:
        """
        GET request with automatic retries and rate-limit handling.

        This is the single shared HTTP helper for all providers,
        eliminating the duplicated retry logic that was in the
        old monolithic renamer_core.py.
        """
        _cfg = cfg or Config()
        attempts = retries or _cfg.RETRY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=_cfg.REQUEST_TIMEOUT,
                )
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", _cfg.RETRY_DELAY * attempt))
                    log.warning("Rate-limited — waiting %ss …", wait)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    log.info(
                        "HTTP 404 (Not Found) for %s — resource does not exist.", url
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
                log.warning("Timeout (attempt %d/%d) for %s", attempt, attempts, url)
            except requests.exceptions.RequestException as e:
                log.error("Request failed: %s", e)
                break
            time.sleep(_cfg.RETRY_DELAY)
        return None

    @staticmethod
    def _post(
        url: str,
        json_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: Optional[int] = None,
        cfg: Optional[Config] = None,
    ) -> Optional[dict]:
        """
        POST request with automatic retries.

        Used by providers that need POST (e.g. AniList GraphQL).
        """
        _cfg = cfg or Config()
        attempts = retries or _cfg.RETRY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                r = requests.post(
                    url,
                    json=json_data,
                    headers=headers,
                    timeout=_cfg.REQUEST_TIMEOUT,
                )
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", _cfg.RETRY_DELAY * attempt))
                    log.warning("Rate-limited — waiting %ss …", wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %s for %s", r.status_code, url)
                if 400 <= r.status_code < 500:
                    break
            except requests.exceptions.ConnectionError:
                log.warning(
                    "Connection error (attempt %d/%d) for %s",
                    attempt, attempts, url,
                )
            except requests.exceptions.Timeout:
                log.warning("Timeout (attempt %d/%d) for %s", attempt, attempts, url)
            except requests.exceptions.RequestException as e:
                log.error("Request failed: %s", e)
                break
            time.sleep(_cfg.RETRY_DELAY)
        return None


# ── Data types (imported by providers) ──────────────────────
from dataclasses import field


@dataclass
class EpisodeInfo:
    absolute: int
    season: int  # 0 = special/OVA
    episode: int
    title: str
    air_date: str = ""
    overview: str = ""
    source: str = ""
    is_special: bool = False


@dataclass
class RenameResult:
    original: str
    renamed: str
    episode: EpisodeInfo
    dest_dir: str = ""
    success: bool = False
    error: str = ""
    skipped: bool = False
