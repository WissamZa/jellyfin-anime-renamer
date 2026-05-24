#!/usr/bin/env -S uv run
"""
qbit_hook.py — called by qBittorrent on torrent completion.

qBittorrent setup (Settings → Downloads → Run external program):
    python /path/to/qbit_hook.py "%I" "%N" "%F"

    %I = torrent hash
    %N = torrent name
    %F = content path (the actual file or root folder on disk)

Flow:
  1. Look up the real title from Nyaa (by hash)
  2. Extract the series name from the title
  3. Search TMDB for the series → get series ID
  4. Move the torrent to BASE_DOWNLOAD_PATH/<series>/ via qBit API
  5. Run AnimeRenamer on that folder, using qBit API for file moves
     so qBittorrent keeps tracking the files and seeding continues
"""

import re
import sys
import time
from pathlib import Path
from typing import Optional  # needed at module level for torrent_info hint

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import os

from renamer_core import (
    AnimeRenamer,
    Config,
    Provider,
    SeriesCache,
    TMDBFetcher,
    TMDBSearch,
    get_logger,
)

log = get_logger("qbit_hook")

# ═══════════════════════ CONFIG ══════════════════════════
QBIT_URL = os.getenv("QBIT_URL", "http://localhost:8080")
QBIT_USERNAME = os.getenv("QBIT_USERNAME", "")
QBIT_PASSWORD = os.getenv("QBIT_PASSWORD", "")
BASE_DOWNLOAD_PATH = Path(os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent"))
# Provider: "tmdb" (default) or "anilist" (free, no API key needed)
ACTIVE_PROVIDER = Provider.from_str(os.getenv("PROVIDER", "tmdb"))

# Regex to extract series name from a typical fansub torrent title:
# "[SubGroup] Series Name (2024) - 1100 [1080p]"  →  "Series Name"
TITLE_PATTERN = re.compile(r"\[[^\]]+\]\s+(.+?)(?:\s+\(.*?\))?\s+-\s+[^\[]+")


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*,]', "", name).strip()


# ════════════════════ QBIT SESSION ═══════════════════════
class QBitClient:
    """Thin wrapper around the qBittorrent Web API v2."""

    def __init__(self, url: str, username: str, password: str):
        self._url = url.rstrip("/")
        self._s = requests.Session()
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        try:
            r = self._s.post(
                f"{self._url}/api/v2/auth/login",
                data={"username": username, "password": password},
                timeout=10,
            )
            if r.status_code not in (200, 204):
                log.error("qBit login failed — status %s: %s", r.status_code, r.text)
                sys.exit(1)
            log.info("Logged into qBittorrent at %s", self._url)
        except requests.exceptions.RequestException as e:
            log.error("Could not connect to qBittorrent: %s", e)
            sys.exit(1)

    def set_location(self, torrent_hash: str, location: str) -> bool:
        """Move the whole torrent's save path (keeps seeding)."""
        r = self._s.post(
            f"{self._url}/api/v2/torrents/setLocation",
            data={"hashes": torrent_hash, "location": location},
        )
        ok = r.status_code in (200, 204)
        if not ok:
            log.error("setLocation failed — status %s", r.status_code)
        return ok

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> bool:
        """
        Rename a single file inside a torrent.
        Paths are relative to the torrent's save location.
        qBit keeps seeding under the new name.
        """
        r = self._s.post(
            f"{self._url}/api/v2/torrents/renameFile",
            data={
                "hash": torrent_hash,
                "oldPath": old_path,
                "newPath": new_path,
            },
        )
        ok = r.status_code in (200, 204)
        if not ok:
            log.error(
                "renameFile failed — status %s | %s → %s",
                r.status_code,
                old_path,
                new_path,
            )
        return ok

    def get_files(self, torrent_hash: str) -> list[dict]:
        """Return the file list for a torrent."""
        try:
            r = self._s.get(
                f"{self._url}/api/v2/torrents/files",
                params={"hash": torrent_hash},
                timeout=10,
            )
            return r.json() if r.status_code == 200 else []
        except Exception as e:
            log.error("Could not fetch file list: %s", e)
            return []

    def torrent_info(self, torrent_hash: str) -> Optional[dict]:
        try:
            r = self._s.get(
                f"{self._url}/api/v2/torrents/info",
                params={"hashes": torrent_hash},
                timeout=10,
            )
            items = r.json() if r.status_code == 200 else []
            return items[0] if items else None
        except Exception as e:
            log.error("Could not fetch torrent info: %s", e)
            return None

    def get_last_completed_torrents(self, limit: int = 1) -> list[dict]:
        """Fetch all torrents and return up to `limit` most recently completed ones."""
        try:
            r = self._s.get(f"{self._url}/api/v2/torrents/info", timeout=10)
            if r.status_code != 200:
                return []
            torrents = r.json()
            # Filter completed torrents (progress == 1)
            completed = [t for t in torrents if t.get("progress", 0) == 1]
            if not completed:
                # Fallback to any torrent if none are marked 100% complete
                completed = torrents
            if not completed:
                return []
            # Sort by completion time (completion_on) or added time (added_on)
            sorted_torrents = sorted(
                completed,
                key=lambda t: max(t.get("completion_on", 0), t.get("added_on", 0)),
                reverse=True,
            )
            return sorted_torrents[:limit]
        except Exception as e:
            log.error("Could not fetch last completed torrents: %s", e)
            return []


# ═══════════════════ NYAA TITLE LOOKUP ═══════════════════
def lookup_nyaa_title(torrent_hash: str, fallback: str) -> str:
    """
    Searches Nyaa for the torrent hash and returns the page title.
    Falls back to the name qBittorrent passed if Nyaa is unreachable.
    """
    try:
        r = requests.get(
            f"https://nyaa.si/?f=0&c=0_0&q={torrent_hash}",
            timeout=10,
        )
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(r.text, "html.parser")
        if soup.h3:
            title = soup.h3.text.strip()
            log.info("Nyaa title: %s", title)
            return title
    except Exception as e:
        log.warning("Nyaa lookup failed (%s) — using torrent name.", e)
    return fallback


# ══════════════════ SERIES NAME EXTRACTION ════════════════
def extract_series_name(title: str) -> Optional[str]:
    """
    Tries the fansub regex first, then falls back to stripping
    common suffixes (episode numbers, resolution tags, group tags).
    """
    m = TITLE_PATTERN.match(title)
    if m:
        return sanitize(m.group(1).split("(")[0].strip())

    # Fallback: strip leading [Group], trailing - NNN [tags]
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title)  # remove [Group]
    cleaned = re.sub(r"\s*-\s*\d+.*$", "", cleaned)  # remove - 1100 …
    cleaned = re.sub(r"\s*\(.*?\)\s*$", "", cleaned)  # remove (2024)
    result = sanitize(cleaned)
    if result:
        log.info("Extracted series name (fallback): %s", result)
        return result

    log.warning("Could not extract series name from: %s", title)
    return None


# ════════════════════ QBIT-SAFE RENAMER ══════════════════
def build_qbit_renamer(
    qbit: QBitClient,
    torrent_hash: str,
    series_folder: Path,
    tmdb_id: int,
    series_name: str,
) -> AnimeRenamer:
    """
    Constructs an AnimeRenamer wired to use the qBittorrent API
    for file moves instead of os.rename(), so seeding is never interrupted.

    The rename_via_qbit closure:
      - Fetches qBit's live file list once (then refreshes after each rename)
        so oldPath always matches *exactly* what qBit is tracking, even when
        files were partially moved by a previous run into a subfolder.
      - Creates destination subdirectories on disk (qBit won't do that).
    """
    cfg = Config(
        tmdb_api_key=os.getenv("TMDB_API_KEY", ""),
        series_name=series_name,
        tmdb_series_id=tmdb_id,
        media_dir=series_folder,
        organize_into_folders=True,
        provider=ACTIVE_PROVIDER,
    )

    # Mutable list so the closure can refresh it after each rename
    _tracked: list[dict] = qbit.get_files(torrent_hash)
    log.info("qBit is tracking %d file(s) for this torrent.", len(_tracked))
    for f in _tracked:
        log.debug("  tracked: %s", f["name"])

    def _refresh_tracked() -> None:
        _tracked.clear()
        _tracked.extend(qbit.get_files(torrent_hash))

    def _qbit_path_for(filename: str) -> str:
        """
        Find the qBit-tracked relative path whose basename matches `filename`.
        """
        # Search all tracked files
        for f in _tracked:
            # Match if the basename of the tracked file matches the basename of our file
            # This handles both files in root and subdirectories correctly
            if Path(f["name"]).name == Path(filename).name:
                return f["name"]

        # DEBUG: log what we have
        log.warning("Available in qBit: %s", [f["name"] for f in _tracked])

        log.warning("File '%s' not found in qBit tracking — using bare name.", filename)
        return filename

    def rename_via_qbit(
        old_path: Path,
        new_path: Path,
        new_name: str,
    ) -> None:
        """
        Called by AnimeRenamer._handle_file() instead of os.rename().
        Uses the qBit-reported path as oldPath so we never send a stale
        or computed path that qBit doesn't recognise (which causes 409).
        """
        # Look up the ACTUAL path qBit is tracking for this file
        old_rel_str = _qbit_path_for(old_path.name)
        new_rel = new_path.relative_to(series_folder)
        new_rel_str = str(new_rel)

        log.info("qBit renameFile: %s → %s", old_rel_str, new_rel_str)

        # Create the destination subfolder on disk first
        new_path.parent.mkdir(parents=True, exist_ok=True)

        ok = qbit.rename_file(torrent_hash, old_rel_str, new_rel_str)
        if not ok:
            raise RuntimeError(f"qBit renameFile failed: {old_rel_str} → {new_rel_str}")

        # Refresh tracking so the next rename sees the updated path
        time.sleep(0.5)
        _refresh_tracked()

    return AnimeRenamer(cfg, rename_via_qbit=rename_via_qbit)


# ══════════════════════════ MAIN ═════════════════════════
def process_torrent(qbit: QBitClient, torrent_hash: str, torrent_name: str) -> None:
    log.info("=" * 60)
    log.info("Processing torrent — hash=%s name=%s", torrent_hash, torrent_name)

    # ── 1. Resolve the real title from Nyaa ───────────────
    real_title = lookup_nyaa_title(torrent_hash, torrent_name)

    # ── 2. Extract the series name ────────────────────────
    series_name = extract_series_name(real_title)
    if not series_name:
        log.error("Could not determine series name — skipping.")
        return

    log.info("Series name: %s", series_name)

    # ── 3. Search for series ID using the configured provider ──
    if ACTIVE_PROVIDER == Provider.AniList:
        from renamer_core import AniListFetcher
        al = AniListFetcher()
        al_result = al.find_series(series_name)
        if not al_result:
            log.error("AniList search returned nothing for '%s' — skipping.", series_name)
            return
        anilist_id, official_name = al_result
        tmdb_id = None
        # Also try to resolve TMDB ID for cache completeness
        if os.getenv("TMDB_API_KEY", ""):
            try:
                searcher = TMDBSearch(os.getenv("TMDB_API_KEY", ""))
                tmdb_result = searcher.find(series_name)
                if tmdb_result:
                    tmdb_id = tmdb_result[0]
            except Exception:
                pass
        log.info("AniList match: id=%d name='%s'", anilist_id, official_name)
    else:
        # TMDB (default)
        searcher = TMDBSearch(os.getenv("TMDB_API_KEY", ""))

        # Try multiple search strategies
        search_variants = [
            series_name,  # Original name
            re.sub(
                r"\s*(?:2nd|3rd|\d+st|\d+nd|\d+rd|\d+th)\s*Season\s*$",
                "",
                series_name,
                flags=re.IGNORECASE,
            ),  # Remove season suffix
            re.sub(
                r"\s*Season\s*\d+\s*$", "", series_name, flags=re.IGNORECASE
            ),  # Remove "Season 2" etc.
            re.sub(r"\s*-\s*\d+.*$", "", series_name),  # Remove trailing numbers and tags
        ]

        result = None
        for i, search_term in enumerate(search_variants):
            if search_term != series_name:
                log.info("Trying search variant %d: '%s'", i + 1, search_term)
            result = searcher.find(search_term)
            if result:
                break

        if not result:
            log.error(
                "TMDB search returned nothing for '%s' (tried %d variants) — skipping.",
                series_name,
                len(search_variants),
            )
            return

        tmdb_id, official_name = result
        anilist_id = None
        log.info("TMDB match: id=%d name='%s'", tmdb_id, official_name)

    # ── 4. Determine & create the series folder ───────────
    series_folder = BASE_DOWNLOAD_PATH / sanitize(official_name)
    series_folder.mkdir(parents=True, exist_ok=True)
    log.info("Series folder: %s", series_folder)

    # Save cache so next run on this folder skips the search
    SeriesCache(series_folder).save(
        series_name=official_name,
        provider=ACTIVE_PROVIDER,
        tmdb_series_id=tmdb_id,
        anilist_id=anilist_id,
    )

    # ── 5. Move the torrent's save location in qBit ───────
    log.info("Moving torrent save path to: %s", series_folder)
    if not qbit.set_location(torrent_hash, str(series_folder)):
        log.error("Failed to move torrent — skipping.")
        return

    # Give qBit a moment to complete the file move on disk
    time.sleep(3)

    # ── 6. Rename & organise via the renamer ──────────────
    log.info("Starting renamer on: %s (provider=%s)", series_folder, ACTIVE_PROVIDER.value)
    renamer = build_qbit_renamer(
        qbit, torrent_hash, series_folder, tmdb_id, official_name
    )
    results = renamer.run(dry_run=False)

    done = sum(1 for r in results if r.success)
    errors = sum(1 for r in results if r.error)

    log.info(
        "Done — renamed %d file(s), %d error(s) | hash=%s",
        done,
        errors,
        torrent_hash,
    )
    log.info("=" * 60)


# ══════════════════════════ MAIN ═════════════════════════
def main() -> None:
    # ── 1. Connect to qBittorrent ─────────────────────────
    qbit = QBitClient(QBIT_URL, QBIT_USERNAME, QBIT_PASSWORD)

    if "-hash" in sys.argv:
        try:
            idx = sys.argv.index("-hash")
            torrent_hash = sys.argv[idx + 1]
            # Need to fetch the name since we only have the hash
            info = qbit.torrent_info(torrent_hash)
            if not info:
                log.error("Could not find torrent with hash %s", torrent_hash)
                sys.exit(1)
            torrent_name = info["name"]
            process_torrent(qbit, torrent_hash, torrent_name)
            sys.exit(0)
        except (ValueError, IndexError):
            log.error("Invalid -hash argument. Usage: python qbit_hook.py -hash <hash>")
            sys.exit(1)

    # ── 2. Parse arguments or fallback to the last completed torrent ──
    n_limit = None
    if "-n" in sys.argv:
        try:
            idx = sys.argv.index("-n")
            n_limit = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            log.error(
                "Invalid -n argument value. Usage: python qbit_hook.py -n <number>"
            )
            sys.exit(1)

    if n_limit is not None or len(sys.argv) < 3:
        # Fallback / Batch mode
        limit = n_limit if n_limit is not None else 1
        log.info(
            "No explicit trigger arguments. Checking the last %d completed torrents...",
            limit,
        )
        targets = qbit.get_last_completed_torrents(limit)
        if not targets:
            log.error("Could not find any completed torrents in qBittorrent.")
            sys.exit(1)

        log.info("Found %d completed torrent(s) to process.", len(targets))
        for t in targets:
            process_torrent(qbit, t["hash"], t["name"])
    else:
        # qBittorrent Trigger Mode
        torrent_hash = sys.argv[1]
        torrent_name = sys.argv[2]
        process_torrent(qbit, torrent_hash, torrent_name)


if __name__ == "__main__":
    main()
