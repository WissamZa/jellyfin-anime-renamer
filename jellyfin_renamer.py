"""
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️  JELLYFIN ANIME RENAMER  🏴‍☠️              ║
║         Multi-source | Undo-safe | Configurable      ║
╚══════════════════════════════════════════════════════╝

Renames anime episodes to Jellyfin-compatible SxxExx format
by fetching metadata from TMDB (primary) and AniList (fallback).
Optionally organises files into Season XX / Specials folders.

Setup:
  1. pip install requests python-dotenv
  2. Copy .env.example to .env and fill in your values
  3. Run: python jellyfin_renamer.py
"""

import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

# ─────────────────────── LOGGING ────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


# ═══════════════════════ CONFIGURATION ══════════════════
class Config:
    # ── Required — loaded from .env ───────────────────────
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")

    # ── Series ────────────────────────────────────────────
    SERIES_NAME:    str = os.getenv("SERIES_NAME",    "One Piece")
    TMDB_SERIES_ID: int = int(os.getenv("TMDB_SERIES_ID", "37854"))
    ANILIST_ID:     int = int(os.getenv("ANILIST_ID",     "21"))

    # ── Paths ─────────────────────────────────────────────
    MEDIA_DIR: Path = Path(os.getenv("MEDIA_DIR", "."))

    # ── Naming templates ──────────────────────────────────
    # Placeholders: {series}, {season:02d}, {episode:02d}, {title}, {ext}
    NAME_TEMPLATE: str = (
        "{series} - S{season:02d}E{episode:02d} - {title}{ext}"
    )
    # Specials: season 0
    SPECIAL_TEMPLATE: str = (
        "{series} - S00E{episode:02d} - {title}{ext}"
    )

    # ── Folder organisation ───────────────────────────────
    # When True, episodes are moved into Season XX sub-folders
    # and specials into a "Specials" folder — Jellyfin-ready layout.
    ORGANIZE_INTO_FOLDERS: bool = (
        os.getenv("ORGANIZE_INTO_FOLDERS", "true").lower() == "true"
    )
    # Folder name templates
    SEASON_FOLDER_TEMPLATE:   str = "Season {season:02d}"   # e.g. Season 01
    SPECIALS_FOLDER_NAME:     str = "Specials"

    # ── Behaviour ─────────────────────────────────────────
    VIDEO_EXTENSIONS: tuple = (".mp4", ".mkv", ".avi", ".m4v", ".flv", ".webm")
    REQUEST_TIMEOUT:  int   = 15
    MAX_WORKERS:      int   = 8
    RETRY_ATTEMPTS:   int   = 3
    RETRY_DELAY:      float = 1.5

    # ── Derived ───────────────────────────────────────────
    @property
    def history_file(self) -> Path:
        return self.MEDIA_DIR / "rename_history.json"

    def validate(self) -> list[str]:
        errors = []
        if not self.TMDB_API_KEY:
            errors.append("TMDB_API_KEY is not set. Add it to your .env file.")
        if not self.MEDIA_DIR.exists():
            errors.append(f"MEDIA_DIR does not exist: {self.MEDIA_DIR}")
        return errors


CFG = Config()


# ════════════════════════ DATA TYPES ════════════════════
@dataclass
class EpisodeInfo:
    absolute:  int
    season:    int        # 0 = special/OVA
    episode:   int
    title:     str
    air_date:  str = ""
    overview:  str = ""
    source:    str = ""
    is_special: bool = False


@dataclass
class RenameResult:
    original:   str
    renamed:    str
    episode:    EpisodeInfo
    dest_dir:   str   = ""   # relative subfolder (e.g. "Season 01")
    success:    bool  = False
    error:      str   = ""
    skipped:    bool  = False


# ════════════════════════ FETCHERS ═══════════════════════
class EpisodeFetcher(ABC):
    """Base class — all fetchers return {abs_number: EpisodeInfo}."""
    name: str = "unknown"

    @abstractmethod
    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        ...

    @abstractmethod
    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Returns {special_episode_number: EpisodeInfo} for season 0."""
        ...

    @staticmethod
    def _get(
        url: str,
        params: Optional[dict] = None,
        retries: Optional[int] = None,
    ) -> Optional[dict]:
        attempts = retries or CFG.RETRY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                r = requests.get(
                    url, params=params, timeout=CFG.REQUEST_TIMEOUT
                )
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    wait = int(
                        r.headers.get("Retry-After", CFG.RETRY_DELAY * attempt)
                    )
                    log.warning("Rate-limited. Waiting %ss …", wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %s for %s", r.status_code, url)
            except requests.exceptions.ConnectionError:
                log.warning(
                    "Connection error (attempt %d/%d) for %s",
                    attempt, attempts, url,
                )
            except requests.exceptions.Timeout:
                log.warning(
                    "Timeout (attempt %d/%d) for %s", attempt, attempts, url
                )
            except requests.exceptions.RequestException as e:
                log.error("Request failed: %s", e)
                break
            time.sleep(CFG.RETRY_DELAY)
        return None


# ── Fetcher 1: TMDB ──────────────────────────────────────
class TMDBFetcher(EpisodeFetcher):
    """Primary fetcher using The Movie Database API."""
    name = "TMDB"
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, series_id: int):
        self._key       = api_key
        self._series_id = series_id
        self._params    = {"api_key": api_key}

    # ── regular episodes ─────────────────────────────────
    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        print("  → Fetching series info from TMDB …")
        show = self._get(
            f"{self.BASE}/tv/{self._series_id}", self._params
        )
        if not show:
            return None

        seasons = [
            s for s in show.get("seasons", []) if s["season_number"] > 0
        ]
        print(
            f"  → Found {len(seasons)} seasons."
            " Fetching episodes in parallel …"
        )

        season_data: dict[int, list] = {}

        def fetch_season(s: dict) -> tuple[int, list]:
            sn   = s["season_number"]
            url  = f"{self.BASE}/tv/{self._series_id}/season/{sn}"
            data = self._get(url, self._params)
            eps  = data.get("episodes", []) if data else []
            return sn, sorted(eps, key=lambda x: x["episode_number"])

        with ThreadPoolExecutor(max_workers=CFG.MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_season, s): s for s in seasons}
            for future in as_completed(futures):
                sn, eps = future.result()
                season_data[sn] = eps

        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        for sn in sorted(season_data):
            for ep in season_data[sn]:
                mapping[abs_counter] = EpisodeInfo(
                    absolute   = abs_counter,
                    season     = sn,
                    episode    = ep["episode_number"],
                    title      = ep.get("name", ""),
                    air_date   = ep.get("air_date", ""),
                    overview   = ep.get("overview", ""),
                    source     = self.name,
                    is_special = False,
                )
                abs_counter += 1

        print(f"  ✔ TMDB: mapped {len(mapping)} episodes.")
        return mapping

    # ── specials (season 0) ──────────────────────────────
    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        print("  → Fetching specials from TMDB (Season 0) …")
        url  = f"{self.BASE}/tv/{self._series_id}/season/0"
        data = self._get(url, self._params)
        if not data:
            print("  ℹ️  No specials found on TMDB.")
            return {}

        specials: dict[int, EpisodeInfo] = {}
        for ep in data.get("episodes", []):
            ep_num = ep["episode_number"]
            specials[ep_num] = EpisodeInfo(
                absolute   = ep_num,
                season     = 0,
                episode    = ep_num,
                title      = ep.get("name", f"Special {ep_num}"),
                air_date   = ep.get("air_date", ""),
                overview   = ep.get("overview", ""),
                source     = self.name,
                is_special = True,
            )

        print(f"  ✔ TMDB: found {len(specials)} specials.")
        return specials


# ── Fetcher 2: AniList (GraphQL) ─────────────────────────
class AniListFetcher(EpisodeFetcher):
    """
    Fallback fetcher using AniList's free GraphQL API.
    Returns a flat list (all episodes as S01Exx) since AniList
    doesn't expose Jellyfin-season splits — useful as a title source.
    """
    name  = "AniList"
    URL   = "https://graphql.anilist.co"
    QUERY = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        episodes
        streamingEpisodes {
          title
          thumbnail
          url
          site
        }
      }
    }
    """

    def __init__(self, anime_id: int):
        self._id = anime_id

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        print("  → Fetching episode titles from AniList …")
        try:
            r = requests.post(
                self.URL,
                json={"query": self.QUERY, "variables": {"id": self._id}},
                timeout=CFG.REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            log.error("AniList request failed: %s", e)
            return None

        if r.status_code != 200:
            log.warning("AniList HTTP %s", r.status_code)
            return None

        media     = r.json().get("data", {}).get("Media", {})
        streaming = media.get("streamingEpisodes", [])
        if not streaming:
            return None

        mapping: dict[int, EpisodeInfo] = {}
        for idx, ep_data in enumerate(streaming, start=1):
            raw_title = ep_data.get("title", f"Episode {idx}")
            clean = re.sub(
                r"^Episode\s+\d+\s*[-–]\s*", "", raw_title
            ).strip()
            mapping[idx] = EpisodeInfo(
                absolute = idx,
                season   = 1,
                episode  = idx,
                title    = clean or raw_title,
                source   = self.name,
            )

        print(f"  ✔ AniList: mapped {len(mapping)} episode titles.")
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        # AniList doesn't provide structured specials data
        return {}


# ── Fetcher 3: TMDB Episode Group ────────────────────────
class TMDBEpisodeGroupFetcher(TMDBFetcher):
    """
    Extended TMDB fetcher that tries to use an episode group
    (e.g. the Crunchyroll/DVD ordering) for more accurate season splits.
    Falls back to standard TMDB fetch if no group is found.
    """
    name = "TMDB-EpisodeGroup"

    def __init__(self, api_key: str, series_id: int, group_id: str = ""):
        super().__init__(api_key, series_id)
        self._group_id = group_id

    def fetch(self) -> Optional[dict[int, EpisodeInfo]]:
        if not self._group_id:
            return super().fetch()

        print(f"  → Fetching episode group '{self._group_id}' from TMDB …")
        url  = f"{self.BASE}/tv/episode_group/{self._group_id}"
        data = self._get(url, self._params)
        if not data:
            print(
                "  ⚠ Episode group not found."
                " Falling back to standard TMDB fetch."
            )
            return super().fetch()

        mapping: dict[int, EpisodeInfo] = {}
        abs_counter = 1
        for group in data.get("groups", []):
            sn = group.get("order", 1)
            for ep in group.get("episodes", []):
                mapping[abs_counter] = EpisodeInfo(
                    absolute = abs_counter,
                    season   = sn,
                    episode  = ep.get("episode_number", abs_counter),
                    title    = ep.get("name", ""),
                    air_date = ep.get("air_date", ""),
                    source   = self.name,
                )
                abs_counter += 1

        season_count = max(
            (info.season for info in mapping.values()), default=0
        )
        print(
            f"  ✔ Episode group: mapped {len(mapping)} episodes"
            f" across {season_count} seasons."
        )
        return mapping


# ══════════════════════ SPECIAL PARSER ══════════════════
class SpecialParser:
    """
    Detects files that are specials/OVAs rather than regular episodes.
    Matches patterns like: SP42, SP 42, OVA1, Fan Letter, etc.
    Returns the special episode number if found, else None.
    """
    PATTERNS: list[str] = [
        r"[Ss][Pp]\s*(\d+)",           # SP42, SP 42
        r"\bOVA\s*(\d+)\b",            # OVA1, OVA 2
        r"\b[Ss]pecial\s*(\d+)\b",     # Special 1
        r"Fan\s+Letter",               # Fan Letter (no number → ep 0)
        r"\bPilot\b",                  # Pilot
    ]

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        """Returns special episode number, or 0 for unnumbered specials."""
        stem = Path(filename).stem
        for pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                # Some patterns have no capture group (e.g. Fan Letter)
                return int(m.group(1)) if m.lastindex else 0
        return None


# ══════════════════════ EPISODE PARSER ══════════════════
class EpisodeNumberParser:
    """Extracts an absolute episode number from a filename."""

    PATTERNS: list[tuple[str, str]] = [
        # Already in SxxExx format — extract the episode part
        ("SxxExx",           r"[Ss]\d+[Ee](\d{1,4})"),
        # Explicit keyword: ep1100, episode 1100
        ("Explicit keyword", r"(?:ep|episode)[.\s_-]*(\d{1,4})\b"),
        # Bracketed: [1100]
        ("Brackets [NNN]",   r"\[(\d{2,4})\]"),
        # Dash then number, optional version suffix: - 1140, - 1140v2
        ("Dash-space NNN",   r"[-–]\s*(\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
        # Trailing number with optional version suffix
        ("Trailing number",  r"[\s._](\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
    ]

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        stem = Path(filename).stem
        for _label, pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None


# ═══════════════════════ HISTORY ═════════════════════════
class RenameHistory:
    """
    Persistent JSON log.
    Stores entries as:
      { "Season 01/New Name.mkv": "OldName.mkv" }
    The key is the path relative to MEDIA_DIR so undo can
    reconstruct the exact location.
    """

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
    def __init__(self, cfg: Config):
        self._cfg     = cfg
        self._history = RenameHistory(cfg.history_file)
        self._parser  = EpisodeNumberParser()
        self._special = SpecialParser()

    # ── public API ───────────────────────────────────────
    def run(self, dry_run: bool = True) -> list[RenameResult]:
        errors = self._cfg.validate()
        if errors:
            for e in errors:
                print(f"  ❌ Config error: {e}")
            return []

        fetcher = TMDBFetcher(self._cfg.TMDB_API_KEY, self._cfg.TMDB_SERIES_ID)

        print("\n[Fetcher] TMDB — regular episodes")
        episode_map = fetcher.fetch()
        if not episode_map:
            print("  ❌ Could not build episode map. Aborting.")
            return []

        print("\n[Fetcher] TMDB — specials")
        specials_map = fetcher.fetch_specials()

        return self._process_files(episode_map, specials_map, dry_run)

    def undo(self) -> None:
        history = self._history.load()
        if not history:
            print("\n  ℹ️  No rename history found — nothing to undo.")
            return

        print(f"\n  🔄 Found {len(history)} entries in rename history.")
        confirm = input("  Revert all to originals? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

        restored, skipped_count, restored_keys = 0, 0, []

        for rel_new, orig_name in history.items():
            # rel_new may be "Season 01/New Name.mkv" or just "New Name.mkv"
            src  = self._cfg.MEDIA_DIR / rel_new
            dest = self._cfg.MEDIA_DIR / orig_name   # always flat original

            if src.exists():
                try:
                    src.rename(dest)
                    print(f"  ↩️  {rel_new}  →  {orig_name}")
                    restored_keys.append(rel_new)
                    restored += 1
                except OSError as e:
                    print(f"  ❌ Could not revert '{rel_new}': {e}")
            else:
                print(f"  ⚠️  Not found (skipped): {rel_new}")
                skipped_count += 1

        self._history.clear_entries(restored_keys)
        print(f"\n  ✅ Restored {restored} file(s).  Skipped {skipped_count}.")

    # ── internals ────────────────────────────────────────
    def _season_folder(self, season: int) -> Path:
        """Return the absolute path of the season subfolder (create if needed)."""
        if season == 0:
            name = self._cfg.SPECIALS_FOLDER_NAME
        else:
            name = self._cfg.SEASON_FOLDER_TEMPLATE.format(season=season)
        folder = self._cfg.MEDIA_DIR / name
        folder.mkdir(exist_ok=True)
        return folder

    def _format_name(self, info: EpisodeInfo, ext: str) -> str:
        clean_title = re.sub(r'[\\/*?:"<>|]', "", info.title)
        if info.is_special:
            return self._cfg.SPECIAL_TEMPLATE.format(
                series  = self._cfg.SERIES_NAME,
                episode = info.episode,
                title   = clean_title,
                ext     = ext,
            )
        return self._cfg.NAME_TEMPLATE.format(
            series  = self._cfg.SERIES_NAME,
            season  = info.season,
            episode = info.episode,
            title   = clean_title,
            ext     = ext,
        )

    def _process_files(
        self,
        episode_map:  dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        dry_run: bool,
    ) -> list[RenameResult]:
        media_dir = self._cfg.MEDIA_DIR
        # Only scan files directly in MEDIA_DIR (not already-sorted subfolders)
        files   = sorted(p for p in media_dir.iterdir() if p.is_file())
        results: list[RenameResult] = []
        session_history: dict[str, str] = {}

        organize = self._cfg.ORGANIZE_INTO_FOLDERS
        mode_label = (
            "⚠️  DRY RUN — no files will be changed"
            if dry_run
            else "🚀  LIVE — files will be renamed"
            + (" & organised into season folders" if organize else "")
        )
        print(f"\n  📂  Scanning: {media_dir}")
        print(f"  {mode_label}\n  {'─'*52}")

        for path in files:
            if path.suffix.lower() not in self._cfg.VIDEO_EXTENSIONS:
                continue

            # ── 1. Check if it's a special ───────────────
            sp_num = self._special.parse(path.name)
            if sp_num is not None:
                info = specials_map.get(sp_num)
                if info is None:
                    # Not in TMDB specials map — create a placeholder
                    label = Path(path.stem).name  # strip ext
                    info  = EpisodeInfo(
                        absolute   = sp_num,
                        season     = 0,
                        episode    = sp_num,
                        title      = label,
                        source     = "filename",
                        is_special = True,
                    )
                self._handle_file(
                    path, info, dry_run, organize,
                    results, session_history,
                )
                continue

            # ── 2. Regular episode ────────────────────────
            abs_num = self._parser.parse(path.name)
            if abs_num is None:
                print(f"  ⚠️  Skipped (unrecognised): {path.name}")
                results.append(
                    RenameResult(
                        path.name, "", EpisodeInfo(0, 0, 0, ""), skipped=True
                    )
                )
                continue

            if abs_num not in episode_map:
                print(f"  ⚠️  Ep {abs_num} not in episode map — skipped.")
                results.append(
                    RenameResult(
                        path.name,
                        "",
                        EpisodeInfo(abs_num, 0, 0, ""),
                        skipped=True,
                    )
                )
                continue

            self._handle_file(
                path, episode_map[abs_num], dry_run, organize,
                results, session_history,
            )

        # ── summary ──────────────────────────────────────
        done    = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        failed  = sum(1 for r in results if r.error)

        if not dry_run and session_history:
            self._history.save(session_history)

        tag = "Would process" if dry_run else "Processed"
        print(f"\n  {'─'*52}")
        print(
            f"  {tag}: {done}  |  Skipped: {skipped}  |  Errors: {failed}"
        )
        if not dry_run and session_history:
            print(f"  📂  Undo log: {self._cfg.history_file}")

        return results

    def _handle_file(
        self,
        path:            Path,
        info:            EpisodeInfo,
        dry_run:         bool,
        organize:        bool,
        results:         list[RenameResult],
        session_history: dict[str, str],
    ) -> None:
        new_name = self._format_name(info, path.suffix)

        # Determine destination directory
        if organize:
            dest_dir  = self._season_folder(info.season) if not dry_run \
                        else self._cfg.MEDIA_DIR / (
                            self._cfg.SPECIALS_FOLDER_NAME
                            if info.season == 0
                            else self._cfg.SEASON_FOLDER_TEMPLATE.format(
                                season=info.season
                            )
                        )
            dest_path = dest_dir / new_name
            rel_key   = str(dest_dir.relative_to(self._cfg.MEDIA_DIR) / new_name)
        else:
            dest_path = self._cfg.MEDIA_DIR / new_name
            rel_key   = new_name

        # Skip if already in place
        if path.resolve() == dest_path.resolve():
            result = RenameResult(
                path.name, new_name, info, skipped=True
            )
            results.append(result)
            return

        season_tag = (
            "SP" if info.is_special
            else f"S{info.season:02d}E{info.episode:02d}"
        )
        folder_tag = (
            f" → {dest_path.parent.name}/" if organize else ""
        )
        print(f"  ✨  {season_tag}{folder_tag}")
        print(f"       From: {path.name}")
        print(f"       To:   {new_name}")
        print(f"       {'─'*48}")

        result = RenameResult(
            path.name, new_name, info,
            dest_dir=str(dest_path.parent.relative_to(self._cfg.MEDIA_DIR)),
        )

        if not dry_run:
            try:
                if organize:
                    dest_path.parent.mkdir(exist_ok=True)
                path.rename(dest_path)
                result.success = True
                session_history[rel_key] = path.name
            except OSError as e:
                result.error = str(e)
                print(f"       ❌  Failed: {e}")
        else:
            result.success = True

        results.append(result)


# ═══════════════════════════ UI ══════════════════════════
BANNER = r"""
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️   JELLYFIN ANIME RENAMER  🏴‍☠️              ║
║    TMDB + AniList  |  Season folders  |  Undo-safe   ║
╚══════════════════════════════════════════════════════╝"""

MENU = """
  ┌──────────────────────────────────────┐
  │  1. Dry Run  (preview only)          │
  │  2. Live Rename + Organise           │
  │  3. Undo previous renames            │
  │  4. Show config                      │
  │  5. Exit                             │
  └──────────────────────────────────────┘"""


def show_config() -> None:
    org = "Yes" if CFG.ORGANIZE_INTO_FOLDERS else "No"
    print(f"""
  Series          : {CFG.SERIES_NAME}
  TMDB ID         : {CFG.TMDB_SERIES_ID}
  AniList ID      : {CFG.ANILIST_ID}
  Media dir       : {CFG.MEDIA_DIR}
  Template        : {CFG.NAME_TEMPLATE}
  Special template: {CFG.SPECIAL_TEMPLATE}
  Organise folders: {org}
  Season folder   : {CFG.SEASON_FOLDER_TEMPLATE.format(season=1)}  (example)
  Specials folder : {CFG.SPECIALS_FOLDER_NAME}
  Extensions      : {', '.join(CFG.VIDEO_EXTENSIONS)}
  History         : {CFG.history_file}""")


def main() -> None:
    renamer = AnimeRenamer(CFG)
    print(BANNER)

    while True:
        print(MENU)
        choice = input("  Option (1-5): ").strip()

        if choice == "1":
            renamer.run(dry_run=True)
        elif choice == "2":
            print("\n  ⚠️  This will rename and move files on disk.")
            if input("  Continue? (y/N): ").strip().lower() == "y":
                renamer.run(dry_run=False)
            else:
                print("  Cancelled.")
        elif choice == "3":
            renamer.undo()
        elif choice == "4":
            show_config()
        elif choice == "5":
            print("\n  Smooth sailing! 🌊\n")
            sys.exit(0)
        else:
            print("  ❌  Invalid choice — enter 1 through 5.")


if __name__ == "__main__":
    main()
