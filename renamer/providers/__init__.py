"""
providers — EpisodeFetcher ABC, data classes, and ProviderRegistry.
"""

from renamer.providers.base import EpisodeFetcher, EpisodeInfo, RenameResult, SeriesSearchResult
from renamer.providers.registry import ProviderRegistry

# Auto-register built-in providers on import
from renamer.providers.tmdb import TMDBFetcher, TMDBSearch
from renamer.providers.anilist import AniListFetcher
from renamer.providers.kitsu import KitsuFetcher

__all__ = [
    "EpisodeFetcher",
    "EpisodeInfo",
    "RenameResult",
    "SeriesSearchResult",
    "ProviderRegistry",
    "TMDBFetcher",
    "TMDBSearch",
    "AniListFetcher",
    "KitsuFetcher",
]
