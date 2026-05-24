"""
renamer — Jellyfin Anime Renamer v4.0.0

Package structure:
  renamer/
    __init__.py        — version & public API re-exports
    config.py          — Config, Provider
    cache.py           — SeriesCache (per-folder metadata cache)
    history.py         — RenameHistory (undo log)
    romaniser.py       — Romaniser, anime_title_case
    renamer.py         — AnimeRenamer (main engine)
    parsers.py         — EpisodeNumberParser, SpecialParser
    providers/
      __init__.py      — ProviderRegistry (plugin system)
      base.py          — EpisodeFetcher, SeriesSearchResult
      tmdb.py          — TMDBFetcher, TMDBSearch
      anilist.py       — AniListFetcher
      kitsu.py         — KitsuFetcher
      romaji_resolver.py — RomajiResolver
"""

__version__ = "4.0.0"

# ── Convenience re-exports so `from renamer import X` still works ──
from renamer.config import Config, Provider
from renamer.cache import SeriesCache
from renamer.history import RenameHistory
from renamer.romaniser import Romaniser, anime_title_case
from renamer.renamer import AnimeRenamer
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.providers import ProviderRegistry
from renamer.providers.base import EpisodeFetcher, SeriesSearchResult
from renamer.providers.tmdb import TMDBFetcher, TMDBSearch
from renamer.providers.anilist import AniListFetcher
from renamer.providers.kitsu import KitsuFetcher
from renamer.providers.romaji_resolver import RomajiResolver

__all__ = [
    "Config",
    "Provider",
    "SeriesCache",
    "RenameHistory",
    "Romaniser",
    "anime_title_case",
    "AnimeRenamer",
    "EpisodeNumberParser",
    "SpecialParser",
    "ProviderRegistry",
    "EpisodeFetcher",
    "SeriesSearchResult",
    "TMDBFetcher",
    "TMDBSearch",
    "AniListFetcher",
    "KitsuFetcher",
    "RomajiResolver",
    "get_logger",
]

# Lazy import to avoid circular dependency — get_logger is defined in config
def get_logger(name: str = "renamer"):
    from renamer.config import get_logger as _gl
    return _gl(name)
