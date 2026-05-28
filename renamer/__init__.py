"""
Jellyfin Anime Renamer
======================
Rename & organise anime episodes for Jellyfin using TMDB, AniList, or Kitsu
as metadata providers.

Supports:
    - Season-folder organisation
    - Romaji title resolution (pykakasi + AniList cross-reference)
    - Absolute episode numbering
    - TMDB Episode Groups for custom season orderings
    - qBittorrent-safe moves (keeps seeding state)
    - Interactive terminal picker (fzf-style)
    - Undo-safe renames via JSON history log
    - Folder icons from provider posters (Dolphin / Nautilus / Thunar)
"""

__version__ = "5.8.0"

from renamer.config import Config, Provider, get_logger
from renamer.cache import SeriesCache
from renamer.history import RenameHistory
from renamer.romaniser import Romaniser, anime_title_case
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.renamer import AnimeRenamer, sanitize_name
from renamer.picker import Picker, pick, MultiPicker, multi_pick
from renamer.providers.base import (
    EpisodeGroupInfo,
    EpisodeInfo,
    RenameResult,
    SeriesSearchResult,
)
from renamer.providers.registry import ProviderRegistry, get_registry
from renamer.icons import (
    PosterResult,
    fetch_poster,
    has_folder_icon,
    remove_folder_icon,
    set_folder_icon,
    set_folder_icon_batch,
)

__all__ = [
    "__version__",
    # Config
    "Config",
    "Provider",
    "get_logger",
    # Core
    "AnimeRenamer",
    "sanitize_name",
    # Storage
    "SeriesCache",
    "RenameHistory",
    # Utilities
    "Romaniser",
    "anime_title_case",
    "EpisodeNumberParser",
    "SpecialParser",
    # UI
    "Picker",
    "pick",
    "MultiPicker",
    "multi_pick",
    # Data classes
    "EpisodeGroupInfo",
    "EpisodeInfo",
    "RenameResult",
    "SeriesSearchResult",
    # Registry
    "ProviderRegistry",
    "get_registry",
    # Icons
    "PosterResult",
    "fetch_poster",
    "has_folder_icon",
    "remove_folder_icon",
    "set_folder_icon",
    "set_folder_icon_batch",
]
