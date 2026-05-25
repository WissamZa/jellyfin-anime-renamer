"""
config.py — Configuration, logging setup, and Provider enum.

All runtime settings come from environment variables (loaded from .env).
Can also be constructed with explicit overrides for programmatic use.
"""

import json
import logging
import logging.handlers
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from the same directory as this package's parent
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ─────────────────────── LOGGING ────────────────────────
LOG_FILE = Path(__file__).resolve().parent.parent / "renamer.log"


def get_logger(name: str = "renamer") -> logging.Logger:
    """
    Returns a logger that writes to both the console and a rotating
    log file (5 x 1 MB).  Call once per entry-point script.
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured (e.g. re-imported)
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


# ═══════════════════════ PROVIDERS ═════════════════════
class Provider(str, Enum):
    """
    Metadata provider for episode data.

    Each provider is registered in ProviderRegistry.  Adding a new
    provider only requires:
      1. Create a new file in renamer/providers/
      2. Subclass EpisodeFetcher and implement fetch() / fetch_specials()
      3. Add the enum value here and register it in ProviderRegistry
    """
    TMDB = "tmdb"
    AniList = "anilist"
    Kitsu = "kitsu"

    @classmethod
    def from_str(cls, value: str) -> "Provider":
        """Parse a provider string (case-insensitive). Falls back to TMDB."""
        mapping = {p.value: p for p in cls}
        return mapping.get(value.strip().lower(), cls.TMDB)


# ═══════════════════════ CONFIGURATION ══════════════════
class Config:
    """
    All values come from environment variables (loaded from .env).
    Can also be constructed with explicit overrides for programmatic use:

        cfg = Config(series_name="Naruto", media_dir=Path("/mnt/D/Torrent/Naruto"))
    """

    def __init__(
        self,
        tmdb_api_key: Optional[str] = None,
        series_name: Optional[str] = None,
        tmdb_series_id: Optional[int] = None,
        anilist_id: Optional[int] = None,
        kitsu_id: Optional[int] = None,
        media_dir: Optional[Path] = None,
        organize_into_folders: Optional[bool] = None,
        provider: Optional[Provider] = None,
    ):
        self.TMDB_API_KEY = tmdb_api_key or os.getenv("TMDB_API_KEY", "").strip()

        # Resolve MEDIA_DIR first so we can derive series name from it
        raw_media_dir = os.getenv("MEDIA_DIR", "").strip()
        self.MEDIA_DIR = media_dir or Path(raw_media_dir if raw_media_dir else ".")

        # Series name priority: explicit parameter > env var > folder name
        env_series = os.getenv("SERIES_NAME", "").strip()
        if series_name:
            self.SERIES_NAME = series_name
        elif env_series:
            self.SERIES_NAME = env_series
        else:
            self.SERIES_NAME = self.MEDIA_DIR.resolve().name
            if self.SERIES_NAME == ".":
                self.SERIES_NAME = os.getcwd().rsplit(os.sep, 1)[-1]

        # Provider-specific IDs — each is optional; resolved via search if not set
        self.TMDB_SERIES_ID: Optional[int] = (
            tmdb_series_id
            if tmdb_series_id is not None
            else self._parse_int_env(os.getenv("TMDB_SERIES_ID", ""), "TMDB_SERIES_ID")
        )

        self.ANILIST_ID: Optional[int] = (
            anilist_id
            if anilist_id is not None
            else self._parse_int_env(os.getenv("ANILIST_ID", ""), "ANILIST_ID")
        )

        self.KITSU_ID: Optional[int] = (
            kitsu_id
            if kitsu_id is not None
            else self._parse_int_env(os.getenv("KITSU_ID", ""), "KITSU_ID")
        )

        self.ORGANIZE_INTO_FOLDERS = (
            organize_into_folders
            if organize_into_folders is not None
            else os.getenv("ORGANIZE_INTO_FOLDERS", "true").lower() == "true"
        )

        # Provider: which API to use as primary episode data source
        if provider is not None:
            self.PROVIDER = provider
        else:
            env_provider = os.getenv("PROVIDER", "").strip()
            self.PROVIDER = Provider.from_str(env_provider) if env_provider else Provider.TMDB

        # Naming templates (not overridable at runtime — change in .env or here)
        # Available placeholders:
        #   {series}   — series name
        #   {season}   — season number (always 2-digit padded)
        #   {episode}  — episode number within the season (2-digit padded)
        #   {absolute} — absolute (global) episode number — used for long-running anime
        #   {title}    — episode title
        #   {ext}      — file extension including dot (.mkv)
        self.NAME_TEMPLATE = "{series} - S{season:02d}E{episode} - {title}{ext}"
        self.SPECIAL_TEMPLATE = "{series} - S00E{episode:02d} - {title}{ext}"
        self.SEASON_FOLDER_TEMPLATE = "Season {season:02d}"
        self.SPECIALS_FOLDER_NAME = "Specials"

        # Long-running anime (e.g. One Piece, Naruto) use the absolute episode number
        # as the episode field so the filename reflects the true global episode count.
        #
        # When a series has more episodes than this threshold the episode number in the
        # filename is replaced with the absolute counter:
        #   One Piece ep 1163  →  One Piece - S23E1163 - Home ... .mkv
        #
        # Set ABSOLUTE_EPISODE_THRESHOLD=0 in .env to always use within-season numbering.
        # Set ABSOLUTE_EPISODE_THRESHOLD=-1 to always use absolute numbering.
        raw_threshold = os.getenv("ABSOLUTE_EPISODE_THRESHOLD", "100").strip()
        try:
            self.ABSOLUTE_EPISODE_THRESHOLD: int = int(raw_threshold)
        except ValueError:
            log.warning(
                "ABSOLUTE_EPISODE_THRESHOLD '%s' is not a valid integer — using 100",
                raw_threshold,
            )
            self.ABSOLUTE_EPISODE_THRESHOLD = 100

        self.VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m4v", ".flv", ".webm")
        self.REQUEST_TIMEOUT = 15
        self.MAX_WORKERS = 8
        self.RETRY_ATTEMPTS = 3
        self.RETRY_DELAY = 1.5

    @property
    def history_file(self) -> Path:
        return self.MEDIA_DIR / "rename_history.json"

    @staticmethod
    def _parse_int_env(raw: str, name: str) -> Optional[int]:
        """Generic safe parser for integer env vars. Returns None if blank/invalid."""
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            log.warning(
                "%s in .env is not a valid integer: '%s' — will auto-resolve",
                name,
                val,
            )
            return None

    def validate(self) -> list[str]:
        errors = []
        if self.PROVIDER == Provider.TMDB and not self.TMDB_API_KEY:
            errors.append(
                "TMDB_API_KEY is not set. Add it to your .env file (required for TMDB provider)."
            )
        if not self.MEDIA_DIR.exists():
            errors.append(f"MEDIA_DIR does not exist: {self.MEDIA_DIR}")
        return errors
