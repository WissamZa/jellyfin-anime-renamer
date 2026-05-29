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

__version__ = "1.8.0"

from renamer.cache import SeriesCache
from renamer.config import Config, Provider, get_logger
from renamer.history import RenameHistory
from renamer.icons import (
    PosterResult,
    fetch_poster,
    has_folder_icon,
    remove_folder_icon,
    set_folder_icon,
    set_folder_icon_batch,
)
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.picker import MultiPicker, Picker, multi_pick, pick
from renamer.providers.base import (
    EpisodeGroupInfo,
    EpisodeInfo,
    RenameResult,
    SeriesSearchResult,
)
from renamer.providers.registry import ProviderRegistry, get_registry
from renamer.renamer import (
    AnimeRenamer,
    _clean_folder_name,
    _extract_alt_title_from_folder,
    _extract_name_from_files,
    sanitize_name,
)
from renamer.romaniser import Romaniser, anime_title_case
from renamer.subtitles import (
    find_matching_subtitles,
    process_subtitles_for_video,
    rename_subtitle,
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
    "_clean_folder_name",
    "_extract_alt_title_from_folder",
    "_extract_name_from_files",
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
    # Subtitles
    "find_matching_subtitles",
    "process_subtitles_for_video",
    "rename_subtitle",
]
