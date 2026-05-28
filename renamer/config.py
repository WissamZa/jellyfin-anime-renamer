"""
renamer.config
==============
Configuration, logging setup, and the Provider enum.

Design notes
------------
* All env-variable reading is centralised here — nothing else calls os.getenv().
* Config is built from explicit parameters **or** from the environment; the two
  paths are unified through `Config.from_env()` so callers never need to know
  about environment variables.
* Logging is configured lazily (once per process) and never pollutes the root
  logger.
* No module-level side-effects (no logging.basicConfig, no load_dotenv at
  import time beyond what's explicitly requested).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_FILE = Path(__file__).resolve().parent.parent / "renamer.log"
_LOGGERS_CONFIGURED: set[str] = set()


def get_logger(name: str = "renamer") -> logging.Logger:
    """
    Return a logger that writes DEBUG+ to a rotating file and INFO+ to stdout.

    Safe to call multiple times with the same name — handlers are added only
    once per process.
    """
    logger = logging.getLogger(name)
    if name in _LOGGERS_CONFIGURED:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False  # don't double-log through root

    _LOGGERS_CONFIGURED.add(name)
    return logger


log = get_logger()

# ---------------------------------------------------------------------------
# Provider enum
# ---------------------------------------------------------------------------


class Provider(StrEnum):
    """Metadata provider."""

    TMDB = "tmdb"
    AniList = "anilist"
    Kitsu = "kitsu"

    @classmethod
    def from_str(cls, value: str) -> Provider:
        """Case-insensitive lookup; defaults to TMDB on unknown value."""
        try:
            return cls(value.strip().lower())
        except ValueError:
            log.warning("Unknown provider %r — defaulting to TMDB.", value)
            return cls.TMDB


# ---------------------------------------------------------------------------
# Episode-start modes
# ---------------------------------------------------------------------------

START_MODE_PER_SEASON = "per_season"
START_MODE_CONTINUING = "continuing"
_VALID_START_MODES = (START_MODE_PER_SEASON, START_MODE_CONTINUING)

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """
    All runtime configuration for the renamer.

    Instantiate directly for programmatic use::

        cfg = Config(series_name="Naruto", media_dir=Path("/srv/anime/Naruto"))

    Or load from environment variables::

        cfg = Config.from_env()

    The ``from_env`` class-method reads ``.env`` in the project root (if it
    exists) and merges with the real environment.  Keyword overrides take
    precedence over env values in either case.
    """

    # ── API credentials ──────────────────────────────────────
    tmdb_api_key: str = ""

    # ── Series identity ──────────────────────────────────────
    series_name: str = ""
    tmdb_series_id: int | None = None
    anilist_id: int | None = None
    kitsu_id: int | None = None
    episode_group_id: str | None = None

    # ── Paths ────────────────────────────────────────────────
    media_dir: Path = field(default_factory=lambda: Path("."))

    # ── Behaviour toggles ────────────────────────────────────
    provider: Provider = Provider.TMDB
    organize_into_folders: bool = True
    absolute_numbering: bool = False
    episode_start_mode: str = START_MODE_PER_SEASON

    # ── Naming templates (rarely need changing) ──────────────
    name_template: str = "{series} - S{season:02d}E{episode:02d} - {title}{ext}"
    special_template: str = "{series} - S00E{episode:02d} - {title}{ext}"
    season_folder_template: str = "Season {season:02d}"
    specials_folder_name: str = "Specials"

    # ── File handling ────────────────────────────────────────
    video_extensions: Sequence[str] = field(
        default_factory=lambda: (".mp4", ".mkv", ".avi", ".m4v", ".flv", ".webm")
    )

    # ── HTTP ─────────────────────────────────────────────────
    request_timeout: int = 15
    max_workers: int = 8
    retry_attempts: int = 3
    retry_delay: float = 1.5

    def __post_init__(self) -> None:
        """Coerce types and guard against None values that slip through."""
        # provider=None happens when CLI passes no --provider flag
        if self.provider is None:
            self.provider = Provider.TMDB
        # media_dir might arrive as a plain string from some callers
        if not isinstance(self.media_dir, Path):
            self.media_dir = Path(self.media_dir)

    # ── Derived paths ────────────────────────────────────────
    @property
    def history_file(self) -> Path:
        return self.media_dir / "rename_history.json"

    # ── Factory ─────────────────────────────────────────────

    @classmethod
    def from_env(cls, **overrides) -> Config:
        """
        Build a Config from environment variables (+ optional .env file).

        Keyword *overrides* are applied on top, allowing callers to inject
        CLI arguments without exposing os.getenv everywhere.
        """
        _load_dotenv_once()

        def _int(key: str) -> int | None:
            raw = os.getenv(key, "").strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                log.warning("Env var %s=%r is not a valid integer — ignored.", key, raw)
                return None

        def _bool(key: str, default: bool) -> bool:
            return os.getenv(key, str(default)).lower() == "true"

        def _str(key: str, default: str = "") -> str:
            return os.getenv(key, default).strip()

        provider_raw = _str("PROVIDER")
        start_mode_raw = _str("EPISODE_START_MODE", START_MODE_PER_SEASON)

        env_values: dict = dict(
            tmdb_api_key=_str("TMDB_API_KEY"),
            series_name=_str("SERIES_NAME"),
            tmdb_series_id=_int("TMDB_SERIES_ID"),
            anilist_id=_int("ANILIST_ID"),
            kitsu_id=_int("KITSU_ID"),
            episode_group_id=_str("EPISODE_GROUP_ID") or None,
            media_dir=Path(_str("MEDIA_DIR") or "."),
            organize_into_folders=_bool("ORGANIZE_INTO_FOLDERS", True),
            absolute_numbering=_bool("ABSOLUTE_NUMBERING", False),
            provider=Provider.from_str(provider_raw) if provider_raw else Provider.TMDB,
            episode_start_mode=(
                start_mode_raw if start_mode_raw in _VALID_START_MODES
                else START_MODE_PER_SEASON
            ),
        )
        env_values.update(overrides)
        return cls(**env_values)

    # ── Validation ───────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return a list of human-readable error strings (empty = valid)."""
        errors: list[str] = []
        if self.provider == Provider.TMDB and not self.tmdb_api_key:
            errors.append("TMDB_API_KEY is not set — add it to your .env file.")
        if not self.media_dir.exists():
            errors.append(f"MEDIA_DIR does not exist: {self.media_dir}")
        if self.episode_start_mode not in _VALID_START_MODES:
            errors.append(
                f"EPISODE_START_MODE must be one of {_VALID_START_MODES}; "
                f"got {self.episode_start_mode!r}."
            )
        return errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass  # python-dotenv not installed — rely on real env vars
    _DOTENV_LOADED = True
