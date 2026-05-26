"""
config — Provider enum, Config, and logging setup.
"""

import logging
import logging.handlers
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env once
_ENV_LOADED = False


def _ensure_dotenv() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        _ENV_LOADED = True


_ensure_dotenv()

# ─────────────────────── LOGGING ────────────────────────
LOG_FILE = Path(__file__).resolve().parent.parent / "renamer.log"


def get_logger(name: str = "renamer") -> logging.Logger:
    """
    Returns a logger that writes to both the console and a rotating
    log file (5 x 1 MB). Call once per entry-point script.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = get_logger()


# ──────────────────── PROVIDER ENUM ─────────────────────
class Provider(str, Enum):
    """Metadata provider enum."""

    TMDB = "tmdb"
    AniList = "anilist"
    Kitsu = "kitsu"

    @classmethod
    def from_str(cls, value: str) -> "Provider":
        """Case-insensitive lookup; defaults to TMDB."""
        try:
            return cls(value.strip().lower())
        except ValueError:
            log.warning(
                "Unknown provider '%s' — defaulting to TMDB.", value
            )
            return cls.TMDB


# ═══════════════════════ CONFIGURATION ══════════════════
class Config:
    """
    All values come from environment variables (loaded from .env).
    Can also be constructed with explicit overrides for programmatic use:

        cfg = Config(series_name="Naruto", media_dir=Path("/mnt/D/Torrent/Naruto"))
    """

    def __init__(
        self,
        tmdb_api_key:          Optional[str]    = None,
        series_name:           Optional[str]    = None,
        tmdb_series_id:        Optional[int]    = None,
        anilist_id:            Optional[int]    = None,
        kitsu_id:              Optional[int]    = None,
        media_dir:             Optional[Path]   = None,
        organize_into_folders: Optional[bool]   = None,
        provider:              Optional[Provider] = None,
        absolute_numbering:    Optional[bool]   = None,
        episode_group_id:     Optional[str]    = None,
    ):
        self.TMDB_API_KEY = tmdb_api_key or os.getenv("TMDB_API_KEY", "").strip()

        # SERIES_NAME defaults to folder name (no more One Piece default)
        raw_series = os.getenv("SERIES_NAME", "").strip()
        self.SERIES_NAME: str = series_name or raw_series or ""

        self.TMDB_SERIES_ID: Optional[int] = (
            tmdb_series_id
            if tmdb_series_id is not None
            else self._parse_optional_int(os.getenv("TMDB_SERIES_ID", ""))
        )
        self.ANILIST_ID: Optional[int] = (
            anilist_id
            if anilist_id is not None
            else self._parse_optional_int(os.getenv("ANILIST_ID", ""))
        )
        self.KITSU_ID: Optional[int] = (
            kitsu_id
            if kitsu_id is not None
            else self._parse_optional_int(os.getenv("KITSU_ID", ""))
        )

        raw_media_dir = os.getenv("MEDIA_DIR", "").strip()
        self.MEDIA_DIR: Path = media_dir or Path(
            raw_media_dir if raw_media_dir else "."
        )

        self.ORGANIZE_INTO_FOLDERS: bool = (
            organize_into_folders
            if organize_into_folders is not None
            else os.getenv("ORGANIZE_INTO_FOLDERS", "true").lower() == "true"
        )

        # Provider selection
        raw_provider = os.getenv("PROVIDER", "").strip()
        self.provider: Provider = provider or (
            Provider.from_str(raw_provider) if raw_provider else Provider.TMDB
        )

        # Absolute episode numbering
        self.ABSOLUTE_NUMBERING: bool = (
            absolute_numbering
            if absolute_numbering is not None
            else os.getenv("ABSOLUTE_NUMBERING", "false").lower() == "true"
        )

        # TMDB Episode Group ID (custom episode ordering)
        self.EPISODE_GROUP_ID: Optional[str] = (
            episode_group_id
            if episode_group_id is not None
            else os.getenv("EPISODE_GROUP_ID", "").strip() or None
        )

        # Naming templates
        self.NAME_TEMPLATE = (
            "{series} - S{season:02d}E{episode:02d} - {title}{ext}"
        )
        self.SPECIAL_TEMPLATE = (
            "{series} - S00E{episode:02d} - {title}{ext}"
        )
        self.SEASON_FOLDER_TEMPLATE = "Season {season:02d}"
        self.SPECIALS_FOLDER_NAME = "Specials"

        self.VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m4v", ".flv", ".webm")
        self.REQUEST_TIMEOUT = 15
        self.MAX_WORKERS = 8
        self.RETRY_ATTEMPTS = 3
        self.RETRY_DELAY = 1.5

    @property
    def history_file(self) -> Path:
        return self.MEDIA_DIR / "rename_history.json"

    @staticmethod
    def _parse_optional_int(raw: str) -> Optional[int]:
        """Safely parse an optional int from env. Returns None if blank."""
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            log.warning("Integer env value is not valid: '%s'", val)
            return None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.provider == Provider.TMDB and not self.TMDB_API_KEY:
            errors.append("TMDB_API_KEY is not set. Add it to your .env file.")
        if not self.MEDIA_DIR.exists():
            errors.append(f"MEDIA_DIR does not exist: {self.MEDIA_DIR}")
        return errors
