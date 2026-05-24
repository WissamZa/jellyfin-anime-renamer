"""
registry.py — Provider plugin registry.

The registry maps Provider enum values to their fetcher classes
and search functions.  Adding a new provider only requires:
  1. Adding the enum value to Provider
  2. Adding a register() call here

The rest of the codebase uses the registry to create fetchers
and resolve series names, so no other files need modification.
"""

from typing import Optional, TYPE_CHECKING

from renamer.config import Config, Provider, log
from renamer.providers.base import EpisodeFetcher, SeriesSearchResult

if TYPE_CHECKING:
    pass


class ProviderRegistry:
    """
    Central registry that maps Provider enum values to their implementations.

    Usage:
        # Get the right fetcher for the current provider
        fetcher = ProviderRegistry.create_fetcher(cfg)

        # Search for a series using the current provider
        result = ProviderRegistry.search(cfg)

    Adding a new provider:
        1. Create providers/your_provider.py with a subclass of EpisodeFetcher
        2. Add the Provider enum value in config.py
        3. Add a register() call in this file's _PROVIDERS dict
    """

    # Registry of provider implementations.
    # Key: Provider enum value
    # Value: dict with:
    #   'fetcher_class': subclass of EpisodeFetcher
    #   'search_fn': callable(cfg) -> Optional[SeriesSearchResult]
    _PROVIDERS: dict[Provider, dict] = {}

    @classmethod
    def register(
        cls,
        provider: Provider,
        fetcher_class: type[EpisodeFetcher],
        search_fn: callable,
    ) -> None:
        """Register a provider implementation."""
        cls._PROVIDERS[provider] = {
            "fetcher_class": fetcher_class,
            "search_fn": search_fn,
        }

    @classmethod
    def create_fetcher(cls, cfg: Config) -> EpisodeFetcher:
        """
        Create and return the appropriate fetcher for the configured provider.

        The fetcher is initialised with all the IDs from cfg that
        the provider needs.  If the provider-specific ID is None,
        the fetcher may attempt to resolve it during fetch().
        """
        entry = cls._PROVIDERS.get(cfg.PROVIDER)
        if not entry:
            raise ValueError(f"Provider {cfg.PROVIDER} is not registered in the registry")

        fetcher_cls = entry["fetcher_class"]
        provider = cfg.PROVIDER

        if provider == Provider.TMDB:
            return fetcher_cls(cfg.TMDB_API_KEY, cfg.TMDB_SERIES_ID, cfg)
        elif provider == Provider.AniList:
            return fetcher_cls(anime_id=cfg.ANILIST_ID)
        elif provider == Provider.Kitsu:
            return fetcher_cls(kitsu_id=cfg.KITSU_ID)
        else:
            # Generic fallback — pass cfg and let the fetcher figure it out
            return fetcher_cls(cfg=cfg)

    @classmethod
    def search(cls, cfg: Config) -> Optional[SeriesSearchResult]:
        """
        Search for a series using the configured provider.

        Delegates to the provider's registered search_fn.
        Returns SeriesSearchResult if found, None otherwise.
        """
        entry = cls._PROVIDERS.get(cfg.PROVIDER)
        if not entry:
            raise ValueError(f"Provider {cfg.PROVIDER} is not registered in the registry")

        search_fn = entry["search_fn"]
        return search_fn(cfg)

    @classmethod
    def registered_providers(cls) -> list[Provider]:
        """Return list of all registered provider enum values."""
        return list(cls._PROVIDERS.keys())


# ═══════════════════ Register built-in providers ═══════════════════

def _register_builtin_providers() -> None:
    """Import and register all built-in providers."""

    # ── TMDB ──────────────────────────────────────────────
    from renamer.providers.tmdb import TMDBFetcher, TMDBSearch

    def _tmdb_search(cfg: Config) -> Optional[SeriesSearchResult]:
        log.info(
            "No cache found — searching TMDB for '%s' …",
            cfg.SERIES_NAME,
        )
        searcher = TMDBSearch(cfg.TMDB_API_KEY)
        result = searcher.find(cfg.SERIES_NAME)
        if not result:
            log.error("Could not find '%s' on TMDB — aborting.", cfg.SERIES_NAME)
            return None
        tmdb_id, romaji_name = result
        return SeriesSearchResult(
            provider_id=tmdb_id,
            series_name=romaji_name,
            provider_name="TMDB",
        )

    ProviderRegistry.register(Provider.TMDB, TMDBFetcher, _tmdb_search)

    # ── AniList ───────────────────────────────────────────
    from renamer.providers.anilist import AniListFetcher

    def _anilist_search(cfg: Config) -> Optional[SeriesSearchResult]:
        log.info(
            "No cache found — searching AniList for '%s' …",
            cfg.SERIES_NAME,
        )
        al = AniListFetcher()
        result = al.find_series(cfg.SERIES_NAME)
        if not result:
            log.error("Could not find '%s' on AniList — aborting.", cfg.SERIES_NAME)
            return None
        anilist_id, romaji_name = result
        return SeriesSearchResult(
            provider_id=anilist_id,
            series_name=romaji_name,
            provider_name="AniList",
        )

    ProviderRegistry.register(Provider.AniList, AniListFetcher, _anilist_search)

    # ── Kitsu ─────────────────────────────────────────────
    from renamer.providers.kitsu import KitsuFetcher

    def _kitsu_search(cfg: Config) -> Optional[SeriesSearchResult]:
        log.info(
            "No cache found — searching Kitsu for '%s' …",
            cfg.SERIES_NAME,
        )
        kf = KitsuFetcher()
        result = kf.find_series(cfg.SERIES_NAME)
        if not result:
            log.error("Could not find '%s' on Kitsu — aborting.", cfg.SERIES_NAME)
            return None
        kitsu_id, romaji_name = result
        return SeriesSearchResult(
            provider_id=kitsu_id,
            series_name=romaji_name,
            provider_name="Kitsu",
        )

    ProviderRegistry.register(Provider.Kitsu, KitsuFetcher, _kitsu_search)


# Auto-register on import
_register_builtin_providers()
