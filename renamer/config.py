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

# ANSI colour codes for terminal output — disabled automatically when stdout
# is not a TTY (e.g. when piped or redirected).
_LEVEL_COLOURS: dict[int, str] = {
    logging.DEBUG: "\033[90m",  # dark grey
    logging.INFO: "\033[0m",  # default
    logging.WARNING: "\033[93m",  # yellow
    logging.ERROR: "\033[91m",  # red
    logging.CRITICAL: "\033[97;41m",  # white-on-red
}
_RESET = "\033[0m"

# File format includes module name for easier log triage
_FILE_FMT = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  [%(name)s:%(lineno)d]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console format is more compact — no module/line for readability
_CONSOLE_FMT_PLAIN = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _ColouredFormatter(logging.Formatter):
    """Formatter that prefixes each record with an ANSI colour based on level."""

    _FMT = "%(asctime)s  %(levelname)-8s  %(message)s"
    _DATEFMT = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelno, "")
        msg = super().format(record)
        return f"{colour}{msg}{_RESET}" if colour else msg

    def __init__(self) -> None:
        super().__init__(self._FMT, datefmt=self._DATEFMT)


def get_logger(name: str = "renamer") -> logging.Logger:
    """
    Return a namespaced logger with two handlers:

    * **Rotating file** — ``renamer.log`` next to the project root.
      Captures DEBUG and above; includes module name and line number.
      Rotates at 1 MB, keeps 5 backups.
    * **Console (stdout)** — INFO and above; ANSI-coloured when the
      terminal supports it, plain otherwise.

    Safe to call multiple times with the same ``name`` — handlers are
    added exactly once per process.

    Example::

        log = get_logger(__name__)
        log.debug("Loaded %d episodes from cache", len(episodes))
        log.info("Renaming: %s → %s", src, dst)
        log.warning("Skipping %s — no match found", filename)
        log.error("Could not write history: %s", exc)
    """
    logger = logging.getLogger(name)
    if name in _LOGGERS_CONFIGURED:
        return logger

    logger.setLevel(logging.DEBUG)

    # ── Rotating file handler ─────────────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_FILE_FMT)
        logger.addHandler(fh)
    except OSError:
        # Log file may not be writable in some environments (CI, read-only FS)
        pass

    # ── Console handler ───────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    if sys.stdout.isatty():
        ch.setFormatter(_ColouredFormatter())
    else:
        ch.setFormatter(_CONSOLE_FMT_PLAIN)
    logger.addHandler(ch)

    logger.propagate = False  # prevent double-logging through root logger
    _LOGGERS_CONFIGURED.add(name)
    return logger


log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider enum
# ---------------------------------------------------------------------------


class Provider(StrEnum):
    """Metadata provider."""

    TMDB = "tmdb"
    AniList = "anilist"
    Kitsu = "kitsu"
    AniDB = "anidb"

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

# Episode title language options
TITLE_LANG_ROMAJI = "romaji"
TITLE_LANG_ENGLISH = "english"
_VALID_TITLE_LANGS = (TITLE_LANG_ROMAJI, TITLE_LANG_ENGLISH)

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

    # ── AniDB credentials ───────────────────────────────────
    anidb_username: str = ""
    anidb_password: str = ""
    anidb_api_key: str = ""
    anidb_client: str = "jenameramer"
    anidb_client_ver: int = 1
    anidb_offline: bool = False

    # ── Series identity ──────────────────────────────────────
    series_name: str = ""
    tmdb_series_id: int | None = None
    anilist_id: int | None = None
    kitsu_id: int | None = None
    episode_group_id: str | None = None

    # ── Paths ────────────────────────────────────────────────
    media_dir: Path = field(default_factory=lambda: Path("."))
    base_download_path: Path | None = None

    # ── Behaviour toggles ────────────────────────────────────
    provider: Provider = Provider.TMDB
    organize_into_folders: bool = True
    absolute_numbering: bool = False
    episode_start_mode: str = START_MODE_PER_SEASON
    episode_title_lang: str = TITLE_LANG_ROMAJI  # romaji or english episode titles
    season_arc_names: dict[int, str] = field(
        default_factory=dict
    )  # {season_num: "East Blue (1-61)"}
    use_hash: bool = False  # Enable ED2K hash fallback for unidentified files
    scan_recursive: bool = False  # Scan subfolders recursively
    scan_depth: int = 3  # Max recursion depth for scanning

    # ── Naming templates (rarely need changing) ──────────────
    name_template: str = "{series} - S{season:02d}E{episode:02d} - {title}{ext}"
    special_template: str = "{series} - S00E{episode:02d} - {title}{ext}"
    season_folder_template: str = "Season {season:d}"
    specials_folder_name: str = "Specials"

    # ── File handling ────────────────────────────────────────
    video_extensions: Sequence[str] = field(
        default_factory=lambda: (".mp4", ".mkv", ".avi", ".m4v", ".flv", ".webm")
    )
    subtitle_extensions: Sequence[str] = field(
        default_factory=lambda: (".srt", ".ass", ".ssa", ".sub", ".vtt")
    )

    # ── HTTP ─────────────────────────────────────────────────
    request_timeout: int = 15
    max_workers: int = 8
    retry_attempts: int = 3
    retry_delay: float = 1.5

    def __post_init__(self) -> None:
        """Coerce types and guard against None values that slip through."""
        # provider=None happens when CLI passes no --provider flag
        if self.provider is None:  # type: ignore[comparison-overlap]
            self.provider = Provider.TMDB  # type: ignore[assignment]
        # media_dir might arrive as a plain string from some callers
        if not isinstance(self.media_dir, Path):
            self.media_dir = Path(self.media_dir)

    # ── Derived paths ────────────────────────────────────────
    @property
    def history_file(self) -> Path:
        return self.media_dir / "rename_history.json"

    @property
    def all_media_extensions(self) -> tuple[str, ...]:
        """Combined video + subtitle extensions for scanning purposes."""
        return tuple(self.video_extensions) + tuple(self.subtitle_extensions)

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
        title_lang_raw = _str("EPISODE_TITLE_LANG", TITLE_LANG_ROMAJI)

        # Parse subtitle extensions from env (comma-separated, e.g. ".srt,.ass,.ssa")
        _subtitle_ext_raw = _str("SUBTITLE_EXTENSIONS")
        subtitle_exts = None
        if _subtitle_ext_raw:
            subtitle_exts = tuple(
                ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}"
                for ext in _subtitle_ext_raw.split(",")
                if ext.strip()
            )

        # Parse scan depth
        _scan_depth_raw = _str("SCAN_DEPTH", "3")
        try:
            scan_depth = int(_scan_depth_raw)
        except ValueError:
            scan_depth = 3

        env_values: dict = dict(
            tmdb_api_key=_str("TMDB_API_KEY"),
            series_name=_str("SERIES_NAME"),
            tmdb_series_id=_int("TMDB_SERIES_ID"),
            anilist_id=_int("ANILIST_ID"),
            kitsu_id=_int("KITSU_ID"),
            episode_group_id=_str("EPISODE_GROUP_ID") or None,
            media_dir=Path(_str("MEDIA_DIR") or "."),
            base_download_path=Path(_str("BASE_DOWNLOAD_PATH"))
            if _str("BASE_DOWNLOAD_PATH")
            else None,
            organize_into_folders=_bool("ORGANIZE_INTO_FOLDERS", True),
            absolute_numbering=_bool("ABSOLUTE_NUMBERING", False),
            provider=Provider.from_str(provider_raw) if provider_raw else Provider.TMDB,
            episode_start_mode=(
                start_mode_raw if start_mode_raw in _VALID_START_MODES else START_MODE_PER_SEASON
            ),
            episode_title_lang=(
                title_lang_raw if title_lang_raw in _VALID_TITLE_LANGS else TITLE_LANG_ROMAJI
            ),
            # AniDB credentials
            anidb_username=_str("ANIDB_USERNAME"),
            anidb_password=_str("ANIDB_PASSWORD"),
            anidb_api_key=_str("ANIDB_API_KEY"),
            anidb_client=_str("ANIDB_CLIENT", "jenameramer"),
            anidb_client_ver=_int("ANIDB_CLIENT_VER") or 1,
            anidb_offline=_bool("ANIDB_OFFLINE", False),
            # Hash-based identification
            use_hash=_bool("USE_HASH", False),
            scan_recursive=_bool("SCAN_RECURSIVE", False),
            scan_depth=scan_depth,
        )
        if subtitle_exts is not None:
            env_values["subtitle_extensions"] = subtitle_exts
        env_values.update(overrides)
        return cls(**env_values)

    # ── Validation ───────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return a list of human-readable error strings (empty = valid)."""
        errors: list[str] = []
        if self.provider == Provider.TMDB and not self.tmdb_api_key:
            errors.append("TMDB_API_KEY is not set — add it to your .env file.")
        if self.provider == Provider.AniDB and not self.anidb_username:
            errors.append(
                "ANIDB_USERNAME is not set — add it to your .env file "
                "(required for AniDB provider). Register at anidb.net for a free account."
            )
        if self.provider == Provider.AniDB and not self.anidb_password:
            errors.append("ANIDB_PASSWORD is not set — add it to your .env file.")
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
        from dotenv import load_dotenv  # type: ignore[import-untyped]

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass  # python-dotenv not installed — rely on real env vars
    _DOTENV_LOADED = True
