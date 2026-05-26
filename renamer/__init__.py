"""
renamer — Jellyfin Anime Renamer v5.0.0

Rename & organise anime episodes for Jellyfin using TMDB, AniList, or Kitsu
as metadata providers.  Supports season-folder organisation, romaji title
resolution, absolute episode numbering, and qBittorrent-safe moves.
"""

__version__ = "5.3.0"

# ── Convenience re-exports ──────────────────────────────────
from renamer.config import Config, Provider, get_logger
from renamer.cache import SeriesCache
from renamer.history import RenameHistory
from renamer.romaniser import Romaniser, anime_title_case
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.renamer import AnimeRenamer
from renamer.picker import Picker, pick
from renamer.providers.base import EpisodeGroupInfo, EpisodeInfo, RenameResult, SeriesSearchResult
from renamer.providers import ProviderRegistry

__all__ = [
    "__version__",
    "Config",
    "Provider",
    "get_logger",
    "SeriesCache",
    "RenameHistory",
    "Romaniser",
    "anime_title_case",
    "EpisodeNumberParser",
    "SpecialParser",
    "AnimeRenamer",
    "Picker",
    "pick",
    "EpisodeGroupInfo",
    "EpisodeInfo",
    "RenameResult",
    "SeriesSearchResult",
    "ProviderRegistry",
]
