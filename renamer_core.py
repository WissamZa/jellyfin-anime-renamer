#!/usr/bin/env -S uv run
"""
renamer_core.py — shared logic imported by jellyfin_renamer.py and qbit_hook.py
"""

import datetime
import gzip
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

# Load .env from the same directory as this file
load_dotenv(Path(__file__).parent / ".env")

# ─────────────────────── LOGGING ────────────────────────
LOG_FILE = Path(__file__).parent / "renamer.log"


def get_logger(name: str = "renamer") -> logging.Logger:
    """
    Returns a logger that writes to both the console and a rotating
    log file (5 × 1 MB). Call once per entry-point script.
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured (e.g. re-imported)
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler — INFO and above only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = get_logger()


# ─────────────────── SMART TITLE-CASE ──────────────────
PARTICLES_FILE = Path(__file__).parent / "title_case_particles.json"


def _load_lowercase_words() -> set[str]:
    """
    Load the union of all word lists from title_case_particles.json.
    Returns a set of lowercase strings that should NOT be capitalised
    when they appear mid-title.
    """
    try:
        data = json.loads(PARTICLES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "Could not load %s (%s) — using built-in fallback.",
            PARTICLES_FILE.name,
            exc,
        )
        return {
            "no",
            "ni",
            "wa",
            "ga",
            "wo",
            "to",
            "de",
            "ka",
            "na",
            "mo",
            "ya",
            "e",
            "a",
            "an",
            "the",
            "at",
            "by",
            "for",
            "in",
            "of",
            "on",
            "or",
            "and",
        }
    words: set[str] = set()
    for key, lst in data.items():
        if key.startswith("_"):  # skip comment keys
            continue
        if isinstance(lst, list):
            words.update(w.lower().strip() for w in lst if isinstance(w, str))
    return words


def anime_title_case(text: str) -> str:
    """
    Title-case a romanised anime title while keeping Japanese particles
    and short English prepositions in lowercase (matching AniList style).

    Rules:
      • First word is always capitalised.
      • Words in the particles JSON are kept lowercase unless they are first.
      • All other words are capitalised.

    Example:
        "honzuki no gekokujou shisho ni naru tame ni wa"
        → "Honzuki no Gekokujou Shisho ni Naru Tame ni wa"
    """
    lowercase_words = _load_lowercase_words()
    words = text.split()
    result = []
    for i, word in enumerate(words):
        # Preserve any leading punctuation / brackets so we test the bare word
        bare = word.lstrip("([{").rstrip(")]},.:;!?")
        if i == 0 or bare.lower() not in lowercase_words:
            result.append(word[0].upper() + word[1:] if word else word)
        else:
            result.append(word.lower())
    return " ".join(result)


# ══════════════════════ ROMANISER ════════════════════════
class Romaniser:
    """
    Converts Japanese text (hiragana, katakana, kanji) to Hepburn romaji.
    Uses pykakasi which handles all three scripts including kanji.

    If the input is already ASCII/Latin, it is returned unchanged so
    titles like "One Piece" are never mangled.

    Examples:
        "\u30ef\u30f3\u30d4\u30fc\u30b9"         -> "Wanpiisu"
        "\u9032\u6483\u306e\u5de8\u4eba"         -> "Shingeki no Kyojin"
        "One Piece"          -> "One Piece"   (unchanged)
    """

    _JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

    def __init__(self) -> None:
        try:
            import pykakasi

            self._kks = pykakasi.kakasi()
            self._available = True
        except ImportError:
            log.warning(
                "pykakasi not installed — romaji conversion disabled. "
                "Run: uv add pykakasi"
            )
            self._kks = None
            self._available = False

    def is_japanese(self, text: str) -> bool:
        return bool(self._JAPANESE.search(text))

    def to_romaji(self, text: str) -> str:
        """
        Romanise text. Returns original string if:
          - no Japanese characters detected
          - pykakasi is not installed
        """
        if not self._available or not self.is_japanese(text):
            return text

        result = self._kks.convert(text)
        parts = []
        for item in result:
            hep = item["hepburn"].strip()
            orig = item["orig"]
            if hep:
                parts.append(hep)
            else:
                parts.append(orig)

        romanised = " ".join(p for p in parts if p.strip())
        # Collapse multiple spaces, clean up around punctuation
        romanised = re.sub(r" {2,}", " ", romanised)
        romanised = re.sub(r" ([!?.,\'\"])", r"\1", romanised)
        romanised = anime_title_case(romanised)
        log.debug("Romanised: '%s' -> '%s'", text, romanised)
        return romanised.strip()


# Singleton — shared across all fetchers
_romaniser = Romaniser()


# ═══════════════════════ PROVIDERS ═════════════════════
class Provider(str, Enum):
    """
    Metadata provider for episode data.

    TMDB    — The Movie Database (requires API key, best season/episode data)
    AniList — Free GraphQL API (no key needed, human-curated romaji titles)
    Kitsu   — Kitsu.io free JSON API (no key needed, best anime season splits)
    """

    TMDB = "tmdb"
    AniList = "anilist"
    Kitsu = "kitsu"

    @classmethod
    def from_str(cls, value: str) -> "Provider":
        """Parse a provider string (case-insensitive). Falls back to TMDB."""
        mapping = {"tmdb": cls.TMDB, "anilist": cls.AniList, "kitsu": cls.Kitsu}
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
            # Derive from the media directory's folder name
            self.SERIES_NAME = self.MEDIA_DIR.resolve().name
            if self.SERIES_NAME == ".":
                self.SERIES_NAME = os.getcwd().rsplit(os.sep, 1)[-1]

        # TMDB series ID: explicit parameter > env var > None (resolved later via search)
        self.TMDB_SERIES_ID: Optional[int] = (
            tmdb_series_id
            if tmdb_series_id is not None
            else self._parse_tmdb_series_id(os.getenv("TMDB_SERIES_ID", ""))
        )

        self.ANILIST_ID: Optional[int] = (
            anilist_id
            if anilist_id is not None
            else self._parse_anilist_id(os.getenv("ANILIST_ID", ""))
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
        # Priority: explicit parameter > env var > TMDB (default)
        if provider is not None:
            self.PROVIDER = provider
        else:
            env_provider = os.getenv("PROVIDER", "").strip()
            self.PROVIDER = (
                Provider.from_str(env_provider) if env_provider else Provider.TMDB
            )

        # Naming templates (not overridable at runtime — change in .env or here)
        self.NAME_TEMPLATE = "{series} - S{season:02d}E{episode:02d} - {title}{ext}"
        self.SPECIAL_TEMPLATE = "{series} - S00E{episode:02d} - {title}{ext}"
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
    def _parse_tmdb_series_id(raw: str) -> Optional[int]:
        """
        Safely parse TMDB_SERIES_ID from env.
        Returns None if the value is missing or blank — the ID will be
        resolved automatically via TMDB search from the series name.
        """
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            log.warning(
                "TMDB_SERIES_ID in .env is not a valid integer: '%s' — will auto-resolve via search",
                val,
            )
            return None

    @staticmethod
    def _parse_anilist_id(raw: str) -> Optional[int]:
        """
        Safely parse ANILIST_ID from env.
        Returns None (not 0, not a crash) if the value is missing or blank.
        The actual lookup by series name happens in AniListFetcher.find_id().
        """
        val = raw.strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            log.warning(
                "ANILIST_ID in .env is not a valid integer: '%s' — will auto-lookup",
                val,
            )
            return None

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


# ════════════════════════ DATA TYPES ════════════════════
@dataclass
class EpisodeInfo:
    absolute: int
    season: int  # 0 = special/OVA
    episode: int
    title: str
    air_date: str = ""
    overview: str = ""
    source: str = ""
    is_special: bool = False


@dataclass
class RenameResult:
    original: str
    renamed: str
    episode: EpisodeInfo
    dest_dir: str = ""
    success: bool = False
    error: str = ""
    skipped: bool = False


# ════════════════════════ FETCHERS ═══════════════════════
class EpisodeFetcher(ABC):
    name: str = "unknown"

    @abstractmethod
    def fetch(self) -> Optional[dict[int, EpisodeInfo]]: ...

    @abstractmethod
    def fetch_specials(self) -> dict[int, EpisodeInfo]: ...

    @staticmethod
    def _get(
        url: str,
        params: Optional[dict] = None,
        retries: Optional[int] = None,
        cfg: Optional[Config] = None,
    ) -> Optional[dict]:
        _cfg = cfg or Config()
        attempts = retries or _cfg.RETRY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                r = requests.get(url, params=params, timeout=_cfg.REQUEST_TIMEOUT)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", _cfg.RETRY_DELAY * attempt))
                    log.warning("Rate-limited — waiting %ss …", wait)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    log.info(
                        "HTTP 404 (Not Found) for %s — resource does not exist.", url
                    )
                    break
                log.warning("HTTP %s for %s", r.status_code, url)
                if 400 <= r.status_code < 500:
                    break
            except requests.exceptions.ConnectionError:
                log.warning(
                    "Connection error (attempt %d/%d) for %s",
                    attempt,
                    attempts,
                    url,
                )
            except requests.exceptions.Timeout:
                log.warning("Timeout (attempt %d/%d) for %s", attempt, attempts, url)
            except requests.exceptions.RequestException as e:
                log.error("Request failed: %s", e)
                break
            time.sleep(_cfg.RETRY_DELAY)
        return None


# ── TMDB search by name ──────────────────────────────────
class TMDBSearch:
    """
    Resolves a human-readable series name to a TMDB series ID.
    Used by qbit_hook.py which only knows the torrent name.
    """

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str):
        self._params = {"api_key": api_key}

    def find(self, name: str) -> Optional[tuple[int, str]]:
        """
        Returns (tmdb_id, romaji_name) for the top search result.

        Romaji resolution order:
          1. AniList native romaji title  (human-curated)
          2. pykakasi romanisation of TMDB Japanese title
          3. TMDB English title (last resort)
        """
        data = EpisodeFetcher._get(
            f"{self.BASE}/search/tv",
            {**self._params, "query": name, "language": "en-US"},
        )
        if not data:
            return None
        results = data.get("results", [])
        if not results:
            log.warning("TMDB search for '%s' returned no results.", name)
            return None
        top = results[0]
        tmdb_id = top["id"]
        en_name = top["name"]

        # TMDB Japanese title → pykakasi
        ja_name = self._fetch_japanese_name(tmdb_id) or en_name
        tmdb_romaji = _romaniser.to_romaji(ja_name)

        # Cross-reference with AniList and pick the best romaji
        resolver = RomajiResolver()
        romaji = resolver.resolve(
            search_name=name,
            tmdb_romaji=tmdb_romaji,
            english_name=en_name,
        )

        log.info("Final series name: '%s' (TMDB id=%d)", romaji, tmdb_id)
        return tmdb_id, romaji

    def _fetch_japanese_name(self, series_id: int) -> Optional[str]:
        data = EpisodeFetcher._get(
            f"{self.BASE}/tv/{series_id}",
            {**self._params, "language": "ja"},
        )
        if data:
            return data.get("name") or data.get("original_name")
        return None


# ── Fetcher 1: TMDB ──────────────────────────────────────
class TMDBFetcher(EpisodeFetcher):
    name = "TMDB"
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, series_id: int, cfg: Optional[Config] = None):
        self._key = api_key
        self._series_id = series_id
        self._params = {"api_key": api_key}
        self._cfg = cfg or Config()
        self._has_specials = True

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        log.info("TMDB — fetching series id=%s …", self._series_id)
        show = self._get(
            f"{self.BASE}/tv/{self._series_id}", self._params, cfg=self._cfg
        )
        if not show:
            return None

        self._has_specials = any(
            s.get("season_number") == 0 for s in show.get("seasons", [])
        )
        seasons = [s for s in show.get("seasons", []) if s["season_number"] > 0]
        log.info("Found %d seasons — fetching episodes in parallel …", len(seasons))

        season_data: dict[int, list] = {}

        # ── Pass 1: fetch English episode lists (structural data) ──────────
        # One request per season, in parallel. English is the reliable source
        # for episode numbers and air dates. We never skip a season due to a
        # failed Japanese request.
        def fetch_season_en(s: dict) -> tuple[int, list]:
            sn = s["season_number"]
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data = self._get(url, self._params, cfg=self._cfg)
            if not data:
                log.warning(
                    "TMDB: failed to fetch season %d (English) — will retry once", sn
                )
                # One immediate retry before giving up
                data = self._get(url, self._params, cfg=self._cfg)
            eps = data.get("episodes", []) if data else []
            if not eps:
                log.warning("TMDB: season %d returned 0 episodes", sn)
            return sn, sorted(eps, key=lambda x: x["episode_number"])

        with ThreadPoolExecutor(max_workers=self._cfg.MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_season_en, s): s for s in seasons}
            for future in as_completed(futures):
                sn, eps = future.result()
                season_data[sn] = eps

        # ── Pass 2: enrich with Japanese titles (best-effort, non-blocking) ─
        # Fetch Japanese season data sequentially (avoids TMDB rate-limits).
        # If any request fails, we simply keep the English title for that season.
        def enrich_with_ja_titles(sn: int, eps: list) -> list:
            url = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data_ja = self._get(url, {**self._params, "language": "ja"}, cfg=self._cfg)
            if not data_ja:
                return eps
            ja_map = {
                e["episode_number"]: e.get("name", "")
                for e in data_ja.get("episodes", [])
            }
            for ep in eps:
                ja_title = ja_map.get(ep["episode_number"], "")
                if ja_title:
                    ep["name"] = _romaniser.to_romaji(ja_title)
            return eps

        for sn in sorted(season_data):
            season_data[sn] = enrich_with_ja_titles(sn, season_data[sn])

        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        for sn in sorted(season_data):
            # Use position within the season (1, 2, 3 …) as the episode number.
            # TMDB sometimes stores episode_number as a global counter across
            # seasons (e.g. S2E1 → episode_number=13). We always want S02E01
            # regardless of what TMDB's internal numbering says.
            for pos, ep in enumerate(season_data[sn], start=1):
                mapping[abs_counter] = EpisodeInfo(
                    absolute=abs_counter,
                    season=sn,
                    episode=pos,
                    title=ep.get("name", ""),
                    air_date=ep.get("air_date", ""),
                    overview=ep.get("overview", ""),
                    source=self.name,
                    is_special=False,
                )
                abs_counter += 1

        log.info("TMDB: mapped %d episodes.", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        if not self._has_specials:
            log.info("No specials (Season 0) listed for this series on TMDB.")
            return {}

        log.info("TMDB — fetching specials (Season 0) …")
        url = f"{self.BASE}/tv/{self._series_id}/season/0"
        data = self._get(url, self._params, cfg=self._cfg)
        if not data:
            log.info("No specials found on TMDB.")
            return {}

        specials: dict[int, EpisodeInfo] = {}
        for ep in data.get("episodes", []):
            ep_num = ep["episode_number"]
            specials[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=0,
                episode=ep_num,
                title=_romaniser.to_romaji(ep.get("name", f"Special {ep_num}")),
                air_date=ep.get("air_date", ""),
                overview=ep.get("overview", ""),
                source=self.name,
                is_special=True,
            )

        log.info("TMDB: found %d specials.", len(specials))
        return specials


# ── Fetcher 2: AniList ───────────────────────────────────
class AniListFetcher(EpisodeFetcher):
    """
    Fetches series and episode data from AniList's free GraphQL API.
    Returns the official romaji title directly — no mechanical conversion.
    Used both as a fallback fetcher and as a romaji cross-reference.

    When used as the primary provider, AniList resolves the series name
    and fetches episode titles without needing TMDB at all.  No API key
    is required — the GraphQL endpoint is free and unauthenticated.
    """

    name = "AniList"
    URL = "https://graphql.anilist.co"

    # Fetches: romaji/english/native titles + episode count
    SERIES_QUERY = """
    query ($id: Int, $search: String) {
      Media(id: $id, search: $search, type: ANIME) {
        id
        title {
          romaji
          english
          native
        }
        episodes
        format
        status
        startDate { year month day }
        endDate { year month day }
      }
    }
    """

    # AniList episode titles via streaming episodes list
    EPISODES_QUERY = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        streamingEpisodes {
          title
        }
      }
    }
    """

    # Full episode list with airing schedule for better season data
    AIRING_QUERY = """
    query ($id: Int, $page: Int) {
      Media(id: $id, type: ANIME) {
        id
        airingSchedule(page: $page, perPage: 50) {
          pageInfo { hasNextPage }
          nodes {
            episode
            airingAt
          }
        }
      }
    }
    """

    def __init__(self, anime_id: Optional[int] = None):
        self._id = anime_id

    def _gql(self, query: str, variables: dict) -> Optional[dict]:
        try:
            r = requests.post(
                self.URL,
                json={"query": query, "variables": variables},
                timeout=15,
            )
            if r.status_code == 200:
                return r.json().get("data", {}).get("Media")
            log.warning("AniList HTTP %s", r.status_code)
        except requests.exceptions.RequestException as e:
            log.error("AniList request failed: %s", e)
        return None

    def find_id(self, name: str) -> Optional[int]:
        """Search AniList by name and return the AniList series ID."""
        media = self._gql(self.SERIES_QUERY, {"search": name})
        return media.get("id") if media else None

    def find_romaji(self, name: str) -> Optional[str]:
        """
        Search AniList by name and return the official romaji title.
        Also caches the resolved AniList ID on self._id for later use.
        """
        media = self._gql(self.SERIES_QUERY, {"search": name})
        if not media:
            return None
        # Cache the resolved ID so fetch() can use it without a second request
        if not self._id:
            self._id = media.get("id")
        titles = media.get("title", {})
        romaji = titles.get("romaji")
        english = titles.get("english")
        native = titles.get("native")
        log.info(
            "AniList title lookup '%s' → id=%s romaji='%s' english='%s' native='%s'",
            name,
            self._id,
            romaji,
            english,
            native,
        )
        return romaji or english

    def find_series(self, name: str) -> Optional[tuple[int, str]]:
        """
        Search AniList by name and return (anilist_id, romaji_name).

        This is the AniList equivalent of TMDBSearch.find() — it resolves
        a search string to a series ID and the best title for renaming.

        Priority for the series name:
          1. AniList romaji title (human-curated, most accurate for anime)
          2. AniList english title (fallback)

        Returns None if the search fails or returns no results.
        """
        media = self._gql(self.SERIES_QUERY, {"search": name})
        if not media:
            log.warning("AniList search for '%s' returned no results.", name)
            return None

        anilist_id = media.get("id")
        if not anilist_id:
            return None

        # Cache the resolved ID so fetch() can use it without a second request
        if not self._id:
            self._id = anilist_id

        titles = media.get("title", {})
        romaji = titles.get("romaji")
        english = titles.get("english")
        native = titles.get("native")

        # Prefer romaji — it is the standard for anime file naming
        chosen = romaji or english
        if not chosen:
            return None

        # Apply anime_title_case for consistency (particles, capitalisation)
        chosen = anime_title_case(chosen)

        log.info(
            "AniList find_series '%s' → id=%d romaji='%s' english='%s' native='%s' → '%s'",
            name,
            anilist_id,
            romaji,
            english,
            native,
            chosen,
        )
        return anilist_id, chosen

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        if not self._id:
            return None
        log.info("AniList — fetching episode titles for id=%d …", self._id)
        media = self._gql(self.EPISODES_QUERY, {"id": self._id})
        if not media:
            return None

        streaming = media.get("streamingEpisodes", [])
        if not streaming:
            log.info(
                "AniList: no streaming episodes found for id=%d — using airing schedule",
                self._id,
            )
            return self._fetch_from_airing_schedule()

        mapping: dict[int, EpisodeInfo] = {}
        for idx, ep_data in enumerate(streaming, start=1):
            raw = ep_data.get("title", f"Episode {idx}")
            # Strip leading "Episode N - " prefix from streaming titles
            clean = re.sub(r"^Episode\s+\d+\s*[-–]\s*", "", raw).strip()
            mapping[idx] = EpisodeInfo(
                absolute=idx,
                season=1,
                episode=idx,
                title=clean or raw,
                source=self.name,
            )

        log.info("AniList: mapped %d episode titles.", len(mapping))
        return mapping

    def _fetch_from_airing_schedule(self) -> Optional[dict[int, EpisodeInfo]]:
        """
        Fall back to the airing schedule when streamingEpisodes is empty.
        This provides episode numbers and air dates but no titles.
        """
        page = 1
        all_nodes: list[dict] = []

        while True:
            media = self._gql(self.AIRING_QUERY, {"id": self._id, "page": page})
            if not media:
                break
            schedule = media.get("airingSchedule", {})
            nodes = schedule.get("nodes", [])
            all_nodes.extend(nodes)

            has_next = schedule.get("pageInfo", {}).get("hasNextPage", False)
            if not has_next:
                break
            page += 1

        if not all_nodes:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        for node in all_nodes:
            ep_num = node.get("episode", 0)
            if ep_num <= 0:
                continue
            airing_at = node.get("airingAt", 0)
            air_date = ""
            if airing_at:
                air_date = datetime.datetime.fromtimestamp(airing_at).strftime(
                    "%Y-%m-%d"
                )
            mapping[ep_num] = EpisodeInfo(
                absolute=ep_num,
                season=1,
                episode=ep_num,
                title=f"Episode {ep_num}",
                air_date=air_date,
                source=self.name,
            )

        log.info("AniList: mapped %d episodes from airing schedule.", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        return {}


# ── Fetcher 3: Kitsu ────────────────────────────────────
class KitsuFetcher(EpisodeFetcher):
    """
    Kitsu.io as a full metadata provider.

    Uses the free Kitsu JSON API (no API key needed) to:
      1. Search for anime by name
      2. Discover ALL seasons via sequel/prequel relationship chains
      3. Fetch episode lists for each season with titles

    Kitsu is especially good for anime with complex season structures
    (e.g. Re:Zero which has 5+ separate entries) because each cour/season
    gets its own Kitsu entry with its own episode list, and the sequel
    chain links them all together.
    """

    name = "Kitsu"

    API_BASE = "https://kitsu.app/api/edge"
    API_HEADERS = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }

    # Minimum similarity (0-1) to accept a name match
    MIN_SIMILARITY = 0.45

    def __init__(self, kitsu_id: Optional[int] = None):
        self._id = kitsu_id
        self._seasons: Optional[list[tuple[int, str, int]]] = (
            None  # [(season_num, title, kitsu_id), …]
        )
        self._specials_cache: dict[int, EpisodeInfo] = {}

    # ── Kitsu API helpers ──────────────────────────────────

    def _kitsu_get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET request to Kitsu API with caching and error handling."""
        cache_key = ("kitsu", url, frozenset((params or {}).items()))
        if cache_key in _api_cache:
            log.debug("Kitsu cache hit: %s", url)
            return _api_cache[cache_key]

        try:
            r = requests.get(
                url,
                params=params,
                headers=self.API_HEADERS,
                timeout=15,
            )
            if r.status_code == 200:
                result = r.json()
                _api_cache[cache_key] = result
                return result
            log.warning("Kitsu API HTTP %s for %s", r.status_code, url)
        except requests.exceptions.RequestException as e:
            log.error("Kitsu API request failed: %s", e)

        _api_cache[cache_key] = None
        return None

    def _fetch_all_pages(self, url: str, params: Optional[dict] = None) -> list[dict]:
        """Fetch all pages of a paginated Kitsu API response."""
        all_data: list[dict] = []
        current_url: Optional[str] = url
        current_params = params
        page = 0

        while current_url and page < 20:  # Safety limit of 20 pages
            data = self._kitsu_get(current_url, current_params)
            if not data:
                break

            items = data.get("data", [])
            all_data.extend(items)

            # Follow the "next" link for pagination
            links = data.get("links", {})
            next_url = links.get("next")
            if next_url and next_url != current_url:
                current_url = next_url
                current_params = None  # next URL already has params
                page += 1
            else:
                break

        return all_data

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Token-set similarity — order-insensitive, handles partial matches."""
        a_tokens = set(a.lower().split())
        b_tokens = set(b.lower().split())
        if not a_tokens or not b_tokens:
            return 0.0
        intersection = a_tokens & b_tokens
        union = a_tokens | b_tokens
        return len(intersection) / len(union)

    # ── Search by name ─────────────────────────────────────

    def find_series(self, name: str) -> Optional[tuple[int, str]]:
        """
        Search Kitsu for an anime by name.

        Returns (kitsu_id, romaji_title) for the best match, or None.
        Considers both romaji (en_jp) and English titles.
        """
        log.info("Kitsu — searching for '%s' …", name)

        data = self._kitsu_get(
            f"{self.API_BASE}/anime",
            params={
                "filter[text]": name,
                "page[limit]": 10,
            },
        )

        if not data or not data.get("data"):
            log.warning("Kitsu search returned no results for '%s'.", name)
            return None

        best_id: Optional[int] = None
        best_title: Optional[str] = None
        best_score: float = 0.0

        for anime in data["data"]:
            attrs = anime.get("attributes", {})
            titles = attrs.get("titles", {})

            # Kitsu title options: en_jp (romaji), en (English), ja_jp (Japanese)
            romaji = titles.get("en_jp") or titles.get("en")
            english = titles.get("en")
            slug = attrs.get("slug", "")

            # Score against each title variant
            candidates = [t for t in [romaji, english, slug] if t]
            for candidate in candidates:
                score = self._similarity(name, candidate)
                if score > best_score:
                    best_score = score
                    best_id = int(anime["id"])
                    best_title = romaji or english or slug

        if best_id is None or best_score < self.MIN_SIMILARITY:
            log.info(
                "Kitsu found no match for '%s' above threshold (best %.0f%%)",
                name,
                best_score * 100,
            )
            return None

        chosen = anime_title_case(best_title)

        # Cache the ID so fetch() can use it
        if not self._id:
            self._id = best_id

        log.info(
            "Kitsu find_series '%s' → id=%d title='%s' (similarity %.0f%%)",
            name,
            best_id,
            chosen,
            best_score * 100,
        )
        return best_id, chosen

    def find_romaji(self, name: str) -> Optional[str]:
        """Search Kitsu by name and return the romaji title."""
        result = self.find_series(name)
        return result[1] if result else None

    # ── Multi-season discovery ─────────────────────────────

    def _discover_seasons(self, kitsu_id: int) -> list[tuple[int, str, int]]:
        """
        Discover all seasons of an anime by following the sequel chain.

        Returns a list of (season_number, title, kitsu_id) tuples ordered
        from Season 1 to the last sequel found.

        Kitsu links sequels via the media-relationships endpoint.
        We walk the chain: Season 1 → sequel → Season 2 → sequel → …
        The 'include=destination' parameter ensures the destination anime
        data is included inline so we don't need extra lookups.
        """
        seasons: list[tuple[int, str, int]] = []

        # Get the first season's title
        first_data = self._kitsu_get(f"{self.API_BASE}/anime/{kitsu_id}")
        if not first_data or not first_data.get("data"):
            return [(1, "Unknown", kitsu_id)]

        attrs = first_data["data"].get("attributes", {})
        titles = attrs.get("titles", {})
        first_title = (
            titles.get("en_jp") or titles.get("en") or attrs.get("slug", "Season 1")
        )
        seasons.append((1, first_title, kitsu_id))

        # Walk the sequel chain
        current_id = kitsu_id
        season_num = 1
        visited = {kitsu_id}

        for _ in range(20):  # Safety limit
            # Fetch media-relationships with destination included inline
            rel_data = self._kitsu_get(
                f"{self.API_BASE}/anime/{current_id}/media-relationships",
                params={"page[limit]": 20, "include": "destination"},
            )
            if not rel_data or not rel_data.get("data"):
                break

            # Build a lookup map from the included (sideloaded) resources
            included_map: dict[tuple[str, str], dict] = {}
            for inc in rel_data.get("included", []):
                included_map[(inc.get("type"), inc.get("id"))] = inc

            sequel_id: Optional[int] = None

            for rel in rel_data["data"]:
                rel_attrs = rel.get("attributes", {})
                role = rel_attrs.get("role", "")

                # Look for sequel relationships
                if role.lower() != "sequel":
                    continue

                # The destination is referenced in relationships.destination.data
                dest_ref = (
                    rel.get("relationships", {}).get("destination", {}).get("data", {})
                )
                dest_type = dest_ref.get("type")
                dest_id_str = dest_ref.get("id")

                if dest_type != "anime" or not dest_id_str:
                    continue

                sid = int(dest_id_str)
                if sid in visited:
                    continue

                # Look up the destination anime in the included data
                dest_anime = included_map.get(("anime", dest_id_str))
                if dest_anime:
                    d_attrs = dest_anime.get("attributes", {})
                    d_titles = d_attrs.get("titles", {})
                    d_title = (
                        d_titles.get("en_jp")
                        or d_titles.get("en")
                        or d_attrs.get("slug", f"Season {season_num + 1}")
                    )
                    season_num += 1
                    seasons.append((season_num, d_title, sid))
                    sequel_id = sid
                    break
                else:
                    # Destination not in included data — fetch it separately
                    season_num += 1
                    sequel_info = self._kitsu_get(f"{self.API_BASE}/anime/{sid}")
                    if sequel_info and sequel_info.get("data"):
                        s_attrs = sequel_info["data"].get("attributes", {})
                        s_titles = s_attrs.get("titles", {})
                        s_title = (
                            s_titles.get("en_jp")
                            or s_titles.get("en")
                            or s_attrs.get("slug", f"Season {season_num}")
                        )
                        seasons.append((season_num, s_title, sid))
                    else:
                        seasons.append((season_num, f"Season {season_num}", sid))
                    sequel_id = sid
                    break

            if sequel_id is None:
                break

            visited.add(sequel_id)
            current_id = sequel_id

        log.info(
            "Kitsu: discovered %d season(s) — %s",
            len(seasons),
            ", ".join(f"S{s[0]}: {s[1]} (id={s[2]})" for s in seasons),
        )
        return seasons

    # ── Episode fetching ───────────────────────────────────

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        """
        Fetch episode data for ALL discovered seasons from Kitsu.

        If this fetcher was constructed with a kitsu_id, first discovers
        all seasons via the sequel chain, then fetches episodes for each.

        Returns an absolute-numbered episode map spanning all seasons.
        """
        if not self._id:
            return None

        log.info("Kitsu — fetching episode data for id=%d …", self._id)

        # Discover all seasons
        if self._seasons is None:
            self._seasons = self._discover_seasons(self._id)

        if not self._seasons:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        specials: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        special_counter = 1

        for season_num, season_title, season_kitsu_id in self._seasons:
            log.info(
                "Kitsu — fetching episodes for S%02d '%s' (id=%d) …",
                season_num,
                season_title,
                season_kitsu_id,
            )

            episodes = self._fetch_all_pages(
                f"{self.API_BASE}/anime/{season_kitsu_id}/episodes",
                params={"page[limit]": 20, "sort": "number"},
            )

            if not episodes:
                log.warning(
                    "Kitsu: no episodes returned for S%02d (id=%d) — skipping.",
                    season_num,
                    season_kitsu_id,
                )
                continue

            # Sort by episode number (Kitsu may return them out of order)
            valid_eps = []
            for ep in episodes:
                attrs = ep.get("attributes", {})
                ep_num = attrs.get("number")
                if ep_num is not None:
                    try:
                        valid_eps.append((int(ep_num), ep))
                    except (ValueError, TypeError):
                        continue

            valid_eps.sort(key=lambda x: x[0])

            for ep_num, ep in valid_eps:
                attrs = ep.get("attributes", {})
                title = attrs.get("titles", {})
                ep_title = title.get("en_jp") or title.get("en") or f"Episode {ep_num}"

                # Romanise if Japanese
                ep_title = _romaniser.to_romaji(ep_title)
                ep_title = anime_title_case(ep_title)

                # Determine if special
                is_special = attrs.get("category", "") == "special"

                air_date = ""
                aired = attrs.get("aired")
                if aired:
                    air_date = aired[:10] if isinstance(aired, str) else ""

                if is_special:
                    specials[special_counter] = EpisodeInfo(
                        absolute=special_counter,
                        season=0,
                        episode=special_counter,
                        title=ep_title,
                        air_date=air_date,
                        source=self.name,
                        is_special=True,
                    )
                    special_counter += 1
                else:
                    mapping[abs_counter] = EpisodeInfo(
                        absolute=abs_counter,
                        season=season_num,
                        episode=ep_num,
                        title=ep_title,
                        air_date=air_date,
                        source=self.name,
                        is_special=False,
                    )
                    abs_counter += 1

        self._specials_cache = specials

        log.info(
            "Kitsu: mapped %d regular episodes across %d season(s), %d specials.",
            len(mapping),
            len(self._seasons),
            len(specials),
        )
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Return cached specials from the last fetch() call."""
        return self._specials_cache


# ── Romaji resolver ──────────────────────────────────────
class RomajiResolver:
    """
    Determines the best romaji series name by cross-referencing
    TMDB (Japanese title → pykakasi) against AniList (native romaji).

    Priority:
      1. AniList romaji  — human-curated, most accurate
      2. pykakasi romaji — mechanical but handles all kanji
      3. TMDB English    — last resort fallback

    The two candidates are compared; if they are close (fuzzy ratio > 80)
    AniList wins outright. If very different, both are logged and
    AniList is still preferred (it is the authoritative anime database).
    """

    def __init__(self) -> None:
        self._anilist = AniListFetcher()
        self._kitsu = KitsuFetcher()

    def resolve(
        self,
        search_name: str,
        tmdb_romaji: str,
        english_name: str,
    ) -> str:
        # ── 1. Try AniList (human-curated, highest quality) ──────────────
        anilist_romaji = self._anilist.find_romaji(search_name)

        if anilist_romaji:
            similarity = self._similarity(tmdb_romaji, anilist_romaji)
            log.info(
                "Romaji comparison — TMDB: '%s'  AniList: '%s'  similarity: %.0f%%",
                tmdb_romaji,
                anilist_romaji,
                similarity * 100,
            )
            if similarity >= 0.80:
                log.info("High similarity — using AniList romaji: '%s'", anilist_romaji)
            else:
                log.info(
                    "Low similarity (%.0f%%) — preferring AniList: '%s'  "
                    "(TMDB was: '%s')",
                    similarity * 100,
                    anilist_romaji,
                    tmdb_romaji,
                )
            return anilist_romaji

        log.info("AniList returned nothing for '%s' — trying Kitsu …", search_name)

        # ── 2. Try Kitsu (free API, good anime coverage) ───
        kitsu_romaji = self._kitsu.find_romaji(search_name)
        if kitsu_romaji:
            log.info("Using Kitsu romaji: '%s'", kitsu_romaji)
            return kitsu_romaji

        # ── 3. Fall back to pykakasi romanisation (always available) ─────
        log.info(
            "Kitsu also found nothing — falling back to TMDB romaji: '%s'",
            tmdb_romaji,
        )
        return tmdb_romaji

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Simple character-level similarity ratio (no extra deps)."""
        a, b = a.lower().strip(), b.lower().strip()
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        # Count matching characters in order (LCS-lite)
        longer = max(len(a), len(b))
        matches = sum(c1 == c2 for c1, c2 in zip(a, b))
        return matches / longer


# ══════════════════════ PARSERS ══════════════════════════
class SpecialParser:
    PATTERNS: list[str] = [
        r"[Ss][Pp]\s*(\d+)",
        r"\bOVA\s*(\d+)\b",
        r"\b[Ss]pecial\s*(\d+)\b",
        r"Fan\s+Letter",
        r"\bPilot\b",
    ]

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        stem = Path(filename).stem
        for pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                return int(m.group(1)) if m.lastindex else 0
        return None


class EpisodeNumberParser:
    PATTERNS: list[tuple[str, str]] = [
        ("SxxExx", r"[Ss]\d+[Ee](\d{1,4})"),
        ("Explicit keyword", r"(?:ep|episode)[.\s_-]*(\d{1,4})\b"),
        ("Brackets [NNN]", r"\[(\d{2,4})\]"),
        ("Dash-space NNN", r"[-–]\s*(\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
        ("Trailing number", r"[\s._](\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
    ]

    # Expanded patterns to match explicit Season and Episode together
    SEASON_EP_PATTERNS = [
        # Pattern 1: SxxExx or SxxEPxx or Sxx Ep xx (e.g. S02E08, S2 Ep 8, S02EP08)
        re.compile(r"[Ss](\d{1,2})\s*[Ee][Pp]?\s*(\d{1,4})", re.IGNORECASE),
        # Pattern 2: Sxx - xx or Season xx - xx or Sxx_xx (e.g. S4 - 07, Season 2 - 05, S2_08)
        re.compile(r"\b(?:Season|S)(\d{1,2})\s*[-–_]\s*(\d{1,4})\b", re.IGNORECASE),
        # Pattern 3: xxExx or xx_xx like 2x08 (cross-format)
        re.compile(r"\b(\d{1,2})x(\d{1,4})\b", re.IGNORECASE),
        # Pattern 4: Ordinal seasons - 1st Season, 2nd Season, 3rd Season, etc. (e.g., "2nd Season - 07")
        re.compile(
            r"\b(\d{1,2})(?:st|nd|rd|th)\s+Season\s*[-–_]\s*(\d{1,4})\b", re.IGNORECASE
        ),
    ]

    @classmethod
    def parse_season_episode(cls, filename: str) -> tuple[Optional[int], Optional[int]]:
        stem = Path(filename).stem

        for pattern in cls.SEASON_EP_PATTERNS:
            m = pattern.search(stem)
            if m:
                return int(m.group(1)), int(m.group(2))

        return None, None

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        stem = Path(filename).stem
        for _label, pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None


# ══════════════════════ SERIES CACHE ════════════════════
class SeriesCache:
    """
    Per-folder cache for resolved series metadata.

    After the first successful search for a folder, the resolved
    series name, TMDB ID, AniList ID, and Kitsu ID are saved to a JSON
    file inside the media directory.  Subsequent runs read from this
    file instead of hitting TMDB / AniList / Kitsu again.

    The cache file is .series_cache.json, stored in the media directory.
    It can be deleted manually to force a re-search.
    """

    FILENAME = ".series_cache.json"

    # Fields we persist
    _FIELDS = ("series_name", "tmdb_series_id", "anilist_id", "provider", "resolved_at")

    def __init__(self, media_dir: Path):
        self._path = media_dir / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[dict]:
        """
        Load cached series info from the media directory.
        Returns None if the cache doesn't exist or is invalid.
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read series cache: %s", exc)
            return None

        # Basic validation — must have series_name and at least one ID field
        if not isinstance(data, dict):
            return None
        if "series_name" not in data:
            log.warning("Series cache is missing required fields — ignoring.")
            return None
        if (
            "tmdb_series_id" not in data
            and "anilist_id" not in data
            and "kitsu_id" not in data
        ):
            log.warning("Series cache has no provider ID — ignoring.")
            return None

        provider_str = data.get("provider", "tmdb")
        data["provider"] = Provider.from_str(provider_str)

        log.info(
            "Loaded series cache: '%s' (provider=%s, TMDB id=%s, AniList id=%s, Kitsu id=%s)",
            data.get("series_name"),
            data.get("provider").value,
            data.get("tmdb_series_id"),
            data.get("anilist_id"),
            data.get("kitsu_id"),
        )
        return data

    def save(
        self,
        series_name: str,
        provider: Provider = Provider.TMDB,
        tmdb_series_id: Optional[int] = None,
        anilist_id: Optional[int] = None,
        kitsu_id: Optional[int] = None,
    ) -> None:
        """
        Persist resolved series metadata to the media directory.
        """
        data = {
            "series_name": series_name,
            "provider": provider.value,
            "tmdb_series_id": tmdb_series_id,
            "anilist_id": anilist_id,
            "kitsu_id": kitsu_id,
            "resolved_at": datetime.datetime.now().isoformat(),
        }
        try:
            self._path.write_text(
                json.dumps(data, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Saved series cache → %s", self._path)
        except OSError as exc:
            log.warning("Could not save series cache: %s", exc)

    def clear(self) -> None:
        """Delete the cache file so the next run re-searches."""
        if self._path.exists():
            try:
                self._path.unlink()
                log.info("Deleted series cache: %s", self._path)
            except OSError as exc:
                log.warning("Could not delete series cache: %s", exc)


# ═══════════════════════ HISTORY ═════════════════════════
class RenameHistory:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error("Could not read history file: %s", e)
            return {}

    def save(self, data: dict[str, str]) -> None:
        existing = self.load()
        existing.update(data)
        self._path.write_text(
            json.dumps(existing, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

    def clear_entries(self, keys: list[str]) -> None:
        data = self.load()
        for k in keys:
            data.pop(k, None)
        self._path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )


# ══════════════════════ RENAMER CORE ════════════════════
class AnimeRenamer:
    """
    Main renaming engine.

    Used by jellyfin_renamer.py (interactive CLI) and qbit_hook.py
    (automated, called after a torrent completes).

    When called from qbit_hook.py, the `rename_via_qbit` callable is
    provided so that file moves go through the qBittorrent API instead
    of os.rename(), keeping qBit's seeding state intact.
    """

    def __init__(
        self,
        cfg: Config,
        rename_via_qbit: Optional[Callable] = None,
    ):
        self._cfg = cfg
        self._history = RenameHistory(cfg.history_file)
        self._parser = EpisodeNumberParser()
        self._special = SpecialParser()
        # If provided, use qBit API for moves instead of os.rename
        self._rename_via_qbit = rename_via_qbit

    # ── public ───────────────────────────────────────────
    def run(self, dry_run: bool = True) -> list[RenameResult]:
        errors = self._cfg.validate()
        if errors:
            for e in errors:
                log.error("Config error: %s", e)
            return []

        cache = SeriesCache(self._cfg.MEDIA_DIR)

        # ── Step 1: Try loading from folder cache ──────────────────
        needs_resolve = (
            (self._cfg.PROVIDER == Provider.TMDB and self._cfg.TMDB_SERIES_ID is None)
            or (self._cfg.PROVIDER == Provider.AniList and self._cfg.ANILIST_ID is None)
            or (self._cfg.PROVIDER == Provider.Kitsu and self._cfg.KITSU_ID is None)
        )

        if needs_resolve:
            cached = cache.load()
            if cached:
                # Apply cached values — provider-aware
                cached_provider = cached.get("provider", Provider.TMDB)
                self._cfg.SERIES_NAME = cached["series_name"]
                if cached.get("tmdb_series_id"):
                    self._cfg.TMDB_SERIES_ID = cached["tmdb_series_id"]
                if cached.get("anilist_id"):
                    self._cfg.ANILIST_ID = cached["anilist_id"]
                if cached.get("kitsu_id"):
                    self._cfg.KITSU_ID = cached["kitsu_id"]
                # If the cached provider differs from the current one, switch
                if cached_provider != self._cfg.PROVIDER:
                    log.info(
                        "Cache provider (%s) differs from configured (%s) — switching to cached",
                        cached_provider.value,
                        self._cfg.PROVIDER.value,
                    )
                    self._cfg.PROVIDER = cached_provider

                log.info(
                    "Series cache hit → '%s' (provider=%s, TMDB id=%s, AniList id=%s, Kitsu id=%s) — skipping API search",
                    self._cfg.SERIES_NAME,
                    self._cfg.PROVIDER.value,
                    self._cfg.TMDB_SERIES_ID,
                    self._cfg.ANILIST_ID,
                    self._cfg.KITSU_ID,
                )
                needs_resolve = False

        # ── Step 2: If still unresolved, search using the selected provider ─
        if (
            needs_resolve
            or (
                self._cfg.PROVIDER == Provider.TMDB and self._cfg.TMDB_SERIES_ID is None
            )
            or (self._cfg.PROVIDER == Provider.AniList and self._cfg.ANILIST_ID is None)
            or (self._cfg.PROVIDER == Provider.Kitsu and self._cfg.KITSU_ID is None)
        ):
            if self._cfg.PROVIDER == Provider.AniList:
                self._resolve_via_anilist(cache)
            elif self._cfg.PROVIDER == Provider.Kitsu:
                self._resolve_via_kitsu(cache)
            else:
                self._resolve_via_tmdb(cache)

        # ── Step 3: Build the episode map using the selected provider ──────
        if self._cfg.PROVIDER == Provider.AniList:
            fetcher = AniListFetcher(anime_id=self._cfg.ANILIST_ID)
        elif self._cfg.PROVIDER == Provider.Kitsu:
            fetcher = KitsuFetcher(kitsu_id=self._cfg.KITSU_ID)
        else:
            fetcher = TMDBFetcher(
                self._cfg.TMDB_API_KEY,
                self._cfg.TMDB_SERIES_ID,
                self._cfg,
            )

        episode_map = fetcher.fetch()
        specials_map = fetcher.fetch_specials()

        if not episode_map:
            log.error("Could not build episode map — aborting.")
            return []

        return self._process_files(episode_map, specials_map, dry_run)

    def undo(self) -> None:
        history = self._history.load()
        if not history:
            log.info("No rename history found — nothing to undo.")
            return

        log.info("Found %d entries in rename history.", len(history))
        confirm = input("  Revert all to originals? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

        restored, skipped_count, restored_keys = 0, 0, []
        for rel_new, orig_name in history.items():
            src = self._cfg.MEDIA_DIR / rel_new
            dest = self._cfg.MEDIA_DIR / orig_name
            if src.exists():
                try:
                    src.rename(dest)
                    log.info("Restored: %s → %s", rel_new, orig_name)
                    restored_keys.append(rel_new)
                    restored += 1
                except OSError as e:
                    log.error("Could not revert '%s': %s", rel_new, e)
            else:
                log.warning("Not found (skipped): %s", rel_new)
                skipped_count += 1

        self._history.clear_entries(restored_keys)
        log.info("Restored %d file(s). Skipped %d.", restored, skipped_count)

    def _resolve_via_tmdb(self, cache: SeriesCache) -> None:
        """
        Resolve series metadata using TMDB as the primary provider.
        Searches TMDB for the series name, then cross-references with
        AniList/Kitsu for the best romaji title.
        """
        log.info(
            "No cache found — searching TMDB for '%s' …",
            self._cfg.SERIES_NAME,
        )
        searcher = TMDBSearch(self._cfg.TMDB_API_KEY)
        result = searcher.find(self._cfg.SERIES_NAME)
        if not result:
            log.error(
                "Could not find '%s' on TMDB — aborting.",
                self._cfg.SERIES_NAME,
            )
            return
        tmdb_id, romaji_name = result
        self._cfg.TMDB_SERIES_ID = tmdb_id
        self._cfg.SERIES_NAME = romaji_name
        log.info(
            "Resolved via TMDB → ID: %d  Series name: '%s'",
            tmdb_id,
            romaji_name,
        )

        # Also resolve AniList ID if not set (for cache completeness)
        if self._cfg.ANILIST_ID is None:
            al = AniListFetcher()
            al_id = al.find_id(self._cfg.SERIES_NAME)
            if al_id:
                self._cfg.ANILIST_ID = al_id

        # Save to cache so next run skips the search
        cache.save(
            series_name=romaji_name,
            provider=Provider.TMDB,
            tmdb_series_id=tmdb_id,
            anilist_id=self._cfg.ANILIST_ID,
        )

    def _resolve_via_anilist(self, cache: SeriesCache) -> None:
        """
        Resolve series metadata using AniList as the primary provider.
        Searches AniList for the series name and returns the romaji title.
        No API key is required — AniList's GraphQL endpoint is free.
        """
        log.info(
            "No cache found — searching AniList for '%s' …",
            self._cfg.SERIES_NAME,
        )
        al = AniListFetcher()
        result = al.find_series(self._cfg.SERIES_NAME)
        if not result:
            log.error(
                "Could not find '%s' on AniList — aborting.",
                self._cfg.SERIES_NAME,
            )
            return
        anilist_id, romaji_name = result
        self._cfg.ANILIST_ID = anilist_id
        self._cfg.SERIES_NAME = romaji_name
        log.info(
            "Resolved via AniList → ID: %d  Series name: '%s'",
            anilist_id,
            romaji_name,
        )

        # Also try to resolve TMDB ID if not set (for cache completeness)
        if self._cfg.TMDB_SERIES_ID is None and self._cfg.TMDB_API_KEY:
            try:
                searcher = TMDBSearch(self._cfg.TMDB_API_KEY)
                tmdb_result = searcher.find(self._cfg.SERIES_NAME)
                if tmdb_result:
                    self._cfg.TMDB_SERIES_ID = tmdb_result[0]
                    log.info("Also resolved TMDB ID: %d", self._cfg.TMDB_SERIES_ID)
            except Exception:
                pass  # Non-critical — TMDB is not the primary provider

        # Save to cache so next run skips the search
        cache.save(
            series_name=romaji_name,
            provider=Provider.AniList,
            tmdb_series_id=self._cfg.TMDB_SERIES_ID,
            anilist_id=anilist_id,
        )

    def _resolve_via_kitsu(self, cache: SeriesCache) -> None:
        """
        Resolve series metadata using Kitsu as the primary provider.
        Searches Kitsu for the series name and discovers all seasons via
        the sequel chain.  No API key is required.
        Best for anime with complex season structures (e.g. Re:Zero).
        """
        log.info(
            "No cache found — searching Kitsu for '%s' …",
            self._cfg.SERIES_NAME,
        )
        kf = KitsuFetcher()
        result = kf.find_series(self._cfg.SERIES_NAME)
        if not result:
            log.error(
                "Could not find '%s' on Kitsu — aborting.",
                self._cfg.SERIES_NAME,
            )
            return
        kitsu_id, romaji_name = result
        self._cfg.KITSU_ID = kitsu_id
        self._cfg.SERIES_NAME = romaji_name
        log.info(
            "Resolved via Kitsu → ID: %d  Series name: '%s'",
            kitsu_id,
            romaji_name,
        )

        # Also try to resolve other IDs for cache completeness
        if self._cfg.ANILIST_ID is None:
            try:
                al = AniListFetcher()
                al_id = al.find_id(self._cfg.SERIES_NAME)
                if al_id:
                    self._cfg.ANILIST_ID = al_id
            except Exception:
                pass

        if self._cfg.TMDB_SERIES_ID is None and self._cfg.TMDB_API_KEY:
            try:
                searcher = TMDBSearch(self._cfg.TMDB_API_KEY)
                tmdb_result = searcher.find(self._cfg.SERIES_NAME)
                if tmdb_result:
                    self._cfg.TMDB_SERIES_ID = tmdb_result[0]
                    log.info("Also resolved TMDB ID: %d", self._cfg.TMDB_SERIES_ID)
            except Exception:
                pass  # Non-critical

        # Save to cache so next run skips the search
        cache.save(
            series_name=romaji_name,
            provider=Provider.Kitsu,
            tmdb_series_id=self._cfg.TMDB_SERIES_ID,
            anilist_id=self._cfg.ANILIST_ID,
            kitsu_id=kitsu_id,
        )

    # ── internals ────────────────────────────────────────
    def _season_folder(self, season: int) -> Path:
        name = (
            self._cfg.SPECIALS_FOLDER_NAME
            if season == 0
            else self._cfg.SEASON_FOLDER_TEMPLATE.format(season=season)
        )
        folder = self._cfg.MEDIA_DIR / name
        folder.mkdir(exist_ok=True)
        return folder

    def _format_name(self, info: EpisodeInfo, ext: str) -> str:
        clean = re.sub(r'[\\/*?:"<>|]', "", info.title)
        if info.is_special:
            return self._cfg.SPECIAL_TEMPLATE.format(
                series=self._cfg.SERIES_NAME,
                episode=info.episode,
                title=clean,
                ext=ext,
            )
        return self._cfg.NAME_TEMPLATE.format(
            series=self._cfg.SERIES_NAME,
            season=info.season,
            episode=info.episode,
            title=clean,
            ext=ext,
        )

    def _process_files(
        self,
        episode_map: dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        dry_run: bool,
    ) -> list[RenameResult]:
        media_dir = self._cfg.MEDIA_DIR
        files = sorted(p for p in media_dir.rglob("*") if p.is_file())
        results: list[RenameResult] = []
        session_history: dict[str, str] = {}

        organize = self._cfg.ORGANIZE_INTO_FOLDERS
        mode_label = (
            "DRY RUN — no files will be changed"
            if dry_run
            else "LIVE — files will be renamed"
            + (" & organised into season folders" if organize else "")
        )
        log.info("Scanning: %s | %s", media_dir, mode_label)

        # episode.episode is now always position-within-season (1, 2, 3 …)
        # regardless of how TMDB numbers episodes internally.
        season_ep_map: dict[tuple[int, int], EpisodeInfo] = {
            (ep.season, ep.episode): ep for ep in episode_map.values()
        }

        for path in files:
            if path.suffix.lower() not in self._cfg.VIDEO_EXTENSIONS:
                continue

            sp_num = self._special.parse(path.name)
            if sp_num is not None:
                info = specials_map.get(sp_num) or EpisodeInfo(
                    absolute=sp_num,
                    season=0,
                    episode=sp_num,
                    title=path.stem,
                    source="filename",
                    is_special=True,
                )
                self._handle_file(
                    path, info, dry_run, organize, results, session_history
                )
                continue

            # First, check if there is an explicit season and episode
            season_num, ep_num = self._parser.parse_season_episode(path.name)
            info = None
            if season_num is not None and ep_num is not None:
                # Direct lookup — episode.episode is always position-within-season
                info = season_ep_map.get((season_num, ep_num))

                # Fallback: season number exceeds TMDB seasons → use latest season
                if not info:
                    max_season = max((s for s, e in season_ep_map.keys()), default=1)
                    if season_num > max_season:
                        info = season_ep_map.get((max_season, ep_num))
                        if info:
                            log.info(
                                "Season %d > max TMDB season %d — mapped ep %d to S%02d",
                                season_num,
                                max_season,
                                ep_num,
                                max_season,
                            )

                if not info:
                    log.warning(
                        "S%02dE%02d not in episode map — skipped.", season_num, ep_num
                    )
                    results.append(
                        RenameResult(
                            path.name,
                            "",
                            EpisodeInfo(0, season_num, ep_num, ""),
                            skipped=True,
                        )
                    )
                    continue
            else:
                # Fallback to absolute episode number lookup
                abs_num = self._parser.parse(path.name)
                if abs_num is None:
                    log.warning("Skipped (unrecognised): %s", path.name)
                    results.append(
                        RenameResult(
                            path.name, "", EpisodeInfo(0, 0, 0, ""), skipped=True
                        )
                    )
                    continue

                if abs_num not in episode_map:
                    log.warning("Ep %d not in episode map — skipped.", abs_num)
                    results.append(
                        RenameResult(
                            path.name, "", EpisodeInfo(abs_num, 0, 0, ""), skipped=True
                        )
                    )
                    continue
                info = episode_map[abs_num]

            self._handle_file(
                path,
                info,
                dry_run,
                organize,
                results,
                session_history,
            )

        done = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if r.error)

        if not dry_run and session_history:
            self._history.save(session_history)

        tag = "Would process" if dry_run else "Processed"
        log.info("%s: %d | Skipped: %d | Errors: %d", tag, done, skipped, failed)
        if not dry_run and session_history:
            log.info("Undo log: %s", self._cfg.history_file)

        return results

    def _handle_file(
        self,
        path: Path,
        info: EpisodeInfo,
        dry_run: bool,
        organize: bool,
        results: list[RenameResult],
        session_history: dict[str, str],
    ) -> None:
        new_name = self._format_name(info, path.suffix)

        if organize:
            dest_folder = (
                self._season_folder(info.season)
                if not dry_run
                else self._cfg.MEDIA_DIR
                / (
                    self._cfg.SPECIALS_FOLDER_NAME
                    if info.season == 0
                    else self._cfg.SEASON_FOLDER_TEMPLATE.format(season=info.season)
                )
            )
            dest_path = dest_folder / new_name
            rel_key = str(dest_folder.relative_to(self._cfg.MEDIA_DIR) / new_name)
        else:
            dest_path = self._cfg.MEDIA_DIR / new_name
            rel_key = new_name

        if path.resolve() == dest_path.resolve():
            results.append(RenameResult(path.name, new_name, info, skipped=True))
            return

        season_tag = (
            "SP" if info.is_special else f"S{info.season:02d}E{info.episode:02d}"
        )
        log.info(
            "%s  |  %s  →  %s",
            season_tag,
            path.name,
            (dest_path.parent.name + "/" if organize else "") + new_name,
        )

        result = RenameResult(
            path.name,
            new_name,
            info,
            dest_dir=str(dest_path.parent.relative_to(self._cfg.MEDIA_DIR)),
        )

        if not dry_run:
            try:
                if organize:
                    dest_path.parent.mkdir(exist_ok=True)

                if self._rename_via_qbit:
                    # ── qBittorrent-safe move ────────────────
                    # Delegate to the qBit API so seeding continues
                    self._rename_via_qbit(
                        old_path=path,
                        new_path=dest_path,
                        new_name=new_name,
                    )
                else:
                    # ── Standard filesystem move ─────────────
                    path.rename(dest_path)

                result.success = True
                session_history[rel_key] = path.name

            except Exception as e:
                result.error = str(e)
                log.error("Failed to rename '%s': %s", path.name, e)
        else:
            result.success = True

        results.append(result)
