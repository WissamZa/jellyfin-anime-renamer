"""
renamer.providers.registry
==========================
Plugin registry that maps Provider enums to fetcher factories.

Design notes
------------
* Factories are plain callables ``(Config) -> EpisodeFetcher``, so providers
  can be registered by third-party code without subclassing anything here.
* ``create_fetcher`` raises a ``LookupError`` instead of returning ``None``
  — callers that want a soft failure should catch it.
* The global singleton is populated lazily on first access.
"""

from __future__ import annotations

from collections.abc import Callable

from renamer.config import Config, Provider, get_logger
from renamer.providers.base import EpisodeFetcher, SeriesSearchResult

log = get_logger()

# Type alias for a factory callable
FetcherFactory = Callable[[Config], EpisodeFetcher]
SearchFactory = Callable[[str, Config], SeriesSearchResult | None]


class ProviderRegistry:
    """
    Registry that maps a ``Provider`` enum to a fetcher factory and an
    optional search factory.

    Usage::

        registry = ProviderRegistry()
        registry.register(Provider.TMDB, tmdb_factory, tmdb_search)
        fetcher = registry.create_fetcher(Provider.TMDB, cfg)
    """

    def __init__(self) -> None:
        self._fetchers: dict[Provider, FetcherFactory] = {}
        self._searchers: dict[Provider, SearchFactory] = {}

    # ── Registration ─────────────────────────────────────────

    def register(
        self,
        provider: Provider,
        fetcher_factory: FetcherFactory,
        search_factory: SearchFactory | None = None,
    ) -> None:
        """Register a fetcher factory (and optional search factory)."""
        self._fetchers[provider] = fetcher_factory
        if search_factory is not None:
            self._searchers[provider] = search_factory
        log.debug("Registered provider: %s", provider.value)

    # ── Fetcher creation ─────────────────────────────────────

    def create_fetcher(self, provider: Provider, cfg: Config) -> EpisodeFetcher:
        """
        Return a configured fetcher for *provider*.

        Raises
        ------
        LookupError
            If no factory is registered for *provider*.
        ValueError
            If the provider's required config fields are missing.
        """
        if provider is None:
            raise LookupError(
                "provider is None — check your .env PROVIDER setting or series cache. "
                "Expected one of: tmdb, anilist, kitsu."
            )
        factory = self._fetchers.get(provider)
        if factory is None:
            raise LookupError(f"No fetcher registered for provider: {provider.value}")
        return factory(cfg)

    # ── Series search ─────────────────────────────────────────

    def search(
        self,
        provider: Provider,
        name: str,
        cfg: Config,
    ) -> SeriesSearchResult | None:
        """Search for a series by name using the provider's search factory."""
        factory = self._searchers.get(provider)
        if factory is None:
            log.warning("No search factory registered for provider: %s", provider.value)
            return None
        return factory(name, cfg)

    def registered_providers(self) -> list[Provider]:
        return list(self._fetchers.keys())


# ---------------------------------------------------------------------------
# Default fetcher / search factories
# ---------------------------------------------------------------------------


def _tmdb_factory(cfg: Config) -> EpisodeFetcher:
    from renamer.providers.tmdb import TMDBFetcher

    if not cfg.tmdb_series_id:
        raise ValueError("TMDB_SERIES_ID is required for the TMDB provider.")
    return TMDBFetcher(cfg.tmdb_api_key, cfg.tmdb_series_id, cfg)


def _tmdb_search(name: str, cfg: Config) -> SeriesSearchResult | None:
    from renamer.providers.tmdb import TMDBSearch

    result = TMDBSearch(cfg.tmdb_api_key).find(name)
    if result:
        tmdb_id, romaji = result
        return SeriesSearchResult(
            provider=Provider.TMDB.value,
            series_id=tmdb_id,
            series_name=romaji,
            tmdb_id=tmdb_id,
        )
    return None


def _anilist_factory(cfg: Config) -> EpisodeFetcher:
    from renamer.providers.anilist import AniListFetcher

    return AniListFetcher(cfg.anilist_id)


def _kitsu_factory(cfg: Config) -> EpisodeFetcher:
    from renamer.providers.kitsu import KitsuFetcher

    return KitsuFetcher(cfg.kitsu_id, cfg)


def _kitsu_search(name: str, cfg: Config) -> SeriesSearchResult | None:
    from renamer.providers.kitsu import KitsuFetcher

    result = KitsuFetcher(None, cfg).find_series(name)
    if result:
        kitsu_id, romaji = result
        return SeriesSearchResult(
            provider=Provider.Kitsu.value,
            series_id=kitsu_id,
            series_name=romaji,
            kitsu_id=kitsu_id,
        )
    return None


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_global_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """Return the global registry, populating it on first call."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ProviderRegistry()
        _global_registry.register(Provider.TMDB, _tmdb_factory, _tmdb_search)
        _global_registry.register(Provider.AniList, _anilist_factory)
        _global_registry.register(Provider.Kitsu, _kitsu_factory, _kitsu_search)
    return _global_registry
