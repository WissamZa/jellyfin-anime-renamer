"""
renamer.providers.base
======================
Abstract base class, shared dataclasses, and HTTP helper for all providers.

Key improvements over v5.6
---------------------------
* ``requests.Session`` is reused across calls (connection pooling).
* ``_get`` accepts an optional ``session`` so tests can inject a mock.
* ``EpisodeFetcher`` no longer takes a ``Config`` — it receives only what it
  needs (api_key, series_id, etc.) from the concrete subclass.
* ``RenameResult`` uses a proper ``status`` field instead of three separate
  booleans.
* All dataclasses have ``__slots__ = ()`` removed to stay compatible with
  ``dataclasses.replace``.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, ClassVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from renamer.config import Config, get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EpisodeInfo:
    """Normalised episode metadata returned by every provider."""

    absolute: int
    season: int  # 0 = special / OVA
    episode: int
    title: str
    air_date: str = ""
    overview: str = ""
    source: str = ""
    is_special: bool = False


@dataclass
class RenameResult:
    """Outcome of a single file-rename attempt."""

    class Status(Enum):
        PENDING = auto()
        SUCCESS = auto()
        SKIPPED = auto()
        ERROR = auto()

    original: str
    renamed: str
    episode: EpisodeInfo
    dest_dir: str = ""
    status: RenameResult.Status = field(default_factory=lambda: RenameResult.Status.PENDING)
    error: str = ""

    # Convenience helpers (keep backwards compatibility)
    @property
    def success(self) -> bool:
        return self.status == RenameResult.Status.SUCCESS

    @success.setter
    def success(self, value: bool) -> None:
        if value:
            self.status = RenameResult.Status.SUCCESS

    @property
    def skipped(self) -> bool:
        return self.status == RenameResult.Status.SKIPPED

    @skipped.setter
    def skipped(self, value: bool) -> None:
        if value:
            self.status = RenameResult.Status.SKIPPED


@dataclass
class EpisodeGroupInfo:
    """Summary of a TMDB episode group."""

    id: str
    name: str
    description: str
    episode_count: int
    group_count: int
    type: int  # 1=Air Date, 2=Absolute, 3=DVD, 4=Digital, 5=Story Arc, 6=Production, 7=TV

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
    """Result from a provider's series-search."""

    provider: str
    series_id: int
    series_name: str
    tmdb_id: int | None = None
    anilist_id: int | None = None
    kitsu_id: int | None = None


# ---------------------------------------------------------------------------
# HTTP helper (shared session with retry adapter)
# ---------------------------------------------------------------------------


def _build_session(retries: int = 0) -> requests.Session:
    """Return a ``requests.Session`` with a retry-aware HTTP adapter."""
    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=retries,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Module-level shared session (one per process, reset via clear_session())
_shared_session: requests.Session | None = None


def _get_shared_session() -> requests.Session:
    global _shared_session
    if _shared_session is None:
        _shared_session = _build_session()
    return _shared_session


def clear_session() -> None:
    """Reset the shared HTTP session (useful in tests)."""
    global _shared_session
    if _shared_session:
        _shared_session.close()
    _shared_session = None


# ---------------------------------------------------------------------------
# Abstract fetcher
# ---------------------------------------------------------------------------


class EpisodeFetcher(ABC):
    """Abstract base class for episode-metadata fetchers."""

    name: str = "unknown"

    def __init__(self) -> None:
        # Per-instance response cache keyed by (url, frozen_params)
        self._cache: dict[tuple, dict[str, Any] | None] = {}

    @abstractmethod
    def fetch(self) -> dict[int, EpisodeInfo] | None:
        """Return a mapping of absolute-episode-number -> EpisodeInfo."""
        ...

    @abstractmethod
    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Return a mapping of special-number -> EpisodeInfo."""
        ...

    def _get(
        self,
        url: str,
        params: dict | None = None,
        cfg: Config | None = None,
        headers: dict | None = None,
        *,
        session: requests.Session | None = None,
    ) -> dict[str, Any] | None:
        """
        HTTP GET with in-memory caching and rate-limit back-off.

        Parameters
        ----------
        url:
            Full URL to fetch.
        params:
            Query-string parameters.
        cfg:
            Config for timeout / retry settings (falls back to defaults).
        headers:
            Extra HTTP headers.
        session:
            Inject a custom session (e.g. a mock in tests).  Falls back to
            the module-level shared session.
        """
        cache_key = (url, frozenset((params or {}).items()))
        if cache_key in self._cache:
            log.debug("Cache hit: %s", url)
            return self._cache[cache_key]

        _cfg = cfg or Config()
        _session = session or _get_shared_session()
        result: dict[str, Any] | None = None

        for attempt in range(1, _cfg.retry_attempts + 1):
            try:
                r = _session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=_cfg.request_timeout,
                )
                if r.status_code == 200:
                    result = r.json()
                    break
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", _cfg.retry_delay * attempt))
                    log.warning("Rate-limited — retrying in %ss …", wait)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    log.debug("HTTP 404: %s", url)
                    break
                log.warning("HTTP %s for %s", r.status_code, url)
                if 400 <= r.status_code < 500:
                    break  # non-transient client error
            except requests.exceptions.ConnectionError:
                log.warning(
                    "Connection error (attempt %d/%d): %s", attempt, _cfg.retry_attempts, url
                )
            except requests.exceptions.Timeout:
                log.warning("Timeout (attempt %d/%d): %s", attempt, _cfg.retry_attempts, url)
            except requests.exceptions.RequestException as exc:
                log.error("Request failed: %s", exc)
                break

            if attempt < _cfg.retry_attempts:
                time.sleep(_cfg.retry_delay)

        self._cache[cache_key] = result
        return result

    def clear_cache(self) -> None:
        """Evict all cached API responses."""
        self._cache.clear()
