"""
providers.registry — ProviderRegistry (plugin system).
"""

from typing import Optional, Type

from renamer.config import Config, Provider, get_logger
from renamer.providers.base import EpisodeFetcher, SeriesSearchResult

log = get_logger()


class ProviderRegistry:
    """
    Plugin registry for metadata providers.

    Usage:
        registry = ProviderRegistry()
        registry.register(Provider.TMDB, TMDBFetcher, TMDBSearch)
        fetcher = registry.create_fetcher(Provider.TMDB, cfg)
        result  = registry.search(Provider.TMDB, name, cfg)
    """

    def __init__(self) -> None:
        self._fetchers: dict[Provider, Type[EpisodeFetcher]] = {}
        self._search_classes: dict[Provider, type] = {}

    def register(
        self,
        provider: Provider,
        fetcher_cls: Type[EpisodeFetcher],
        search_cls: Optional[type] = None,
    ) -> None:
        """Register a fetcher (and optional search class) for a provider."""
        self._fetchers[provider] = fetcher_cls
        if search_cls is not None:
            self._search_classes[provider] = search_cls
        log.debug("Registered provider: %s", provider.value)

    def create_fetcher(
        self,
        provider: Provider,
        cfg: Config,
    ) -> Optional[EpisodeFetcher]:
        """
        Create and return a fetcher instance for the given provider,
        configured from *cfg*.
        """
        cls = self._fetchers.get(provider)
        if cls is None:
            log.error("No fetcher registered for provider: %s", provider.value)
            return None

        if provider == Provider.TMDB:
            from renamer.providers.tmdb import TMDBFetcher
            if not cfg.TMDB_SERIES_ID:
                log.error("TMDB_SERIES_ID is required for TMDB provider.")
                return None
            return TMDBFetcher(cfg.TMDB_API_KEY, cfg.TMDB_SERIES_ID, cfg)

        if provider == Provider.AniList:
            from renamer.providers.anilist import AniListFetcher
            return AniListFetcher(cfg.ANILIST_ID)

        if provider == Provider.Kitsu:
            from renamer.providers.kitsu import KitsuFetcher
            return KitsuFetcher(cfg.KITSU_ID, cfg)

        # Fallback — try no-arg constructor
        try:
            return cls()
        except Exception as exc:
            log.error("Cannot construct fetcher for %s: %s", provider.value, exc)
            return None

    def search(
        self,
        provider: Provider,
        name: str,
        cfg: Config,
    ) -> Optional[SeriesSearchResult]:
        """Search for a series by name using the provider's search class."""
        search_cls = self._search_classes.get(provider)
        if search_cls is None:
            log.warning("No search class registered for provider: %s", provider.value)
            return None

        if provider == Provider.TMDB:
            from renamer.providers.tmdb import TMDBSearch
            searcher = TMDBSearch(cfg.TMDB_API_KEY)
            result = searcher.find(name)
            if result:
                tmdb_id, romaji = result
                return SeriesSearchResult(
                    provider=provider.value,
                    series_id=tmdb_id,
                    series_name=romaji,
                    tmdb_id=tmdb_id,
                )

        if provider == Provider.Kitsu:
            from renamer.providers.kitsu import KitsuFetcher
            fetcher = KitsuFetcher(None, cfg)
            result = fetcher.find_series(name)
            if result:
                kitsu_id, romaji = result
                return SeriesSearchResult(
                    provider=provider.value,
                    series_id=kitsu_id,
                    series_name=romaji,
                    kitsu_id=kitsu_id,
                )

        return None

    def registered_providers(self) -> list[Provider]:
        """Return list of registered provider enums."""
        return list(self._fetchers.keys())


# ── Global registry ──────────────────────────────────────
_global_registry = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    """Return the global ProviderRegistry (auto-populated on first import)."""
    if not _global_registry.registered_providers():
        _auto_register()
    return _global_registry


def _auto_register() -> None:
    """Register all built-in providers."""
    from renamer.providers.tmdb import TMDBFetcher, TMDBSearch
    from renamer.providers.anilist import AniListFetcher
    from renamer.providers.kitsu import KitsuFetcher

    _global_registry.register(Provider.TMDB, TMDBFetcher, TMDBSearch)
    _global_registry.register(Provider.AniList, AniListFetcher)
    _global_registry.register(Provider.Kitsu, KitsuFetcher)
