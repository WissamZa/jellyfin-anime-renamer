#!/usr/bin/env -S uv run
"""
qbit_hook.py — called by qBittorrent on torrent completion.

qBittorrent setup (Settings -> Downloads -> Run external program):
    python /path/to/qbit_hook.py "%I" "%N" "%F"

    %I = torrent hash
    %N = torrent name
    %F = content path (the actual file or root folder on disk)

Flow:
  1. Look up the real title from Nyaa (by hash)
  2. Extract the series name from the title
  3. Search provider for the series -> get series ID
  4. Move the torrent to BASE_DOWNLOAD_PATH/<series>/ via qBit API
  5. Run AnimeRenamer on that folder, using qBit API for file moves
     so qBittorrent keeps tracking the files and seeding continues
"""

import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from renamer import AnimeRenamer, Config, Provider  # noqa: E402
from renamer.config import get_logger  # noqa: E402
from renamer.icons import set_folder_icon  # noqa: E402
from renamer.providers.registry import get_registry  # noqa: E402

log = get_logger("qbit_hook")

# Load global & series configuration from JSON
import json  # noqa: E402


def load_hook_config() -> dict:
    conf_path = Path(__file__).resolve().parent / "qbit_hook.json"
    if conf_path.exists():
        try:
            return json.loads(conf_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read qbit_hook.json: %s", exc)
    return {}

HOOK_CONFIG = load_hook_config()
HOOK_GLOBAL = HOOK_CONFIG.get("global", {})
HOOK_SERIES = HOOK_CONFIG.get("series", {})

# ═══════════════════════ CONFIG ══════════════════════════
QBIT_URL = os.getenv("QBIT_URL", "http://localhost:8080")
QBIT_USERNAME = os.getenv("QBIT_USERNAME", "")
QBIT_PASSWORD = os.getenv("QBIT_PASSWORD", "")
BASE_DOWNLOAD_PATH = Path(os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent"))

# Active provider (from json, .env or default TMDB)
ACTIVE_PROVIDER = Provider.from_str(HOOK_GLOBAL.get("provider", os.getenv("PROVIDER", "tmdb")))

# Default episode start mode (per_season or continuing)
EPISODE_START_MODE = HOOK_GLOBAL.get("episode_start_mode", os.getenv("EPISODE_START_MODE", "per_season"))

# Use absolute episode numbering (True or False)
ABSOLUTE_NUMBERING = HOOK_GLOBAL.get("absolute_numbering", os.getenv("ABSOLUTE_NUMBERING", "false").lower() == "true")

# Regex to extract series name from a typical fansub torrent title:
# "[SubGroup] Series Name (2024) - 1100 [1080p]"  ->  "Series Name"
TITLE_PATTERN = re.compile(
    r"\[[^\]]+\]\s+(.+?)(?:\s+\(.*?\))?\s+-\s+[^\[]+"
)


def _parse_optional_int(val: str | None) -> int | None:
    """Parse an optional string into an integer."""
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


def sanitize(name: str) -> str:
    """Remove characters invalid in filenames / problematic for Jellyfin."""
    cleaned = re.sub(r'[\\/:*?"<>|;]', '', name)
    # Normalise unicode dashes and ellipsis
    cleaned = cleaned.replace('\u2014', '-').replace('\u2013', '-')
    cleaned = cleaned.replace('\u2026', '...')
    cleaned = re.sub(r'[\u00a0\u2000-\u200b\u2028\u2029\u3000]', ' ', cleaned)
    cleaned = re.sub(r' {2,}', ' ', cleaned).strip(' .-_')
    return cleaned


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
                log.error(
                    "qBit login failed — status %s: %s",
                    r.status_code, r.text,
                )
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

    def rename_file(
        self, torrent_hash: str, old_path: str, new_path: str
    ) -> bool:
        """Rename a single file inside a torrent."""
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
                "renameFile failed — status %s | %s -> %s",
                r.status_code, old_path, new_path,
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

    def torrent_info(self, torrent_hash: str) -> dict | None:
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
            completed = [t for t in torrents if t.get("progress", 0) == 1]
            if not completed:
                completed = torrents
            if not completed:
                return []
            sorted_torrents = sorted(
                completed,
                key=lambda t: max(
                    t.get("completion_on", 0), t.get("added_on", 0)
                ),
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
def extract_series_name(title: str) -> str | None:
    """
    Tries the fansub regex first, then falls back to stripping
    common suffixes (episode numbers, resolution tags, group tags).
    """
    m = TITLE_PATTERN.match(title)
    if m:
        return sanitize(m.group(1).split("(")[0].strip())

    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title)
    cleaned = re.sub(r"\s*-\s*\d+.*$", "", cleaned)
    cleaned = re.sub(r"\s*\(.*?\)\s*$", "", cleaned)
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
    cfg: Config,
) -> AnimeRenamer:
    """
    Constructs an AnimeRenamer wired to use the qBittorrent API
    for file moves instead of os.rename(), so seeding is never interrupted.
    """
    _tracked: list[dict] = qbit.get_files(torrent_hash)
    log.info(
        "qBit is tracking %d file(s) for this torrent.", len(_tracked)
    )
    for f in _tracked:
        log.debug("  tracked: %s", f["name"])

    def _refresh_tracked() -> None:
        _tracked.clear()
        _tracked.extend(qbit.get_files(torrent_hash))

    def _qbit_path_for(filename: str) -> str:
        for f in _tracked:
            if Path(f["name"]).name == filename:
                return f["name"]
        log.warning(
            "File '%s' not found in qBit tracking — using bare name.",
            filename,
        )
        return filename

    def rename_via_qbit(
        old_path: Path,
        new_path: Path,
        new_name: str,
    ) -> None:
        old_rel_str = _qbit_path_for(old_path.name)
        new_rel = new_path.relative_to(series_folder)
        new_rel_str = str(new_rel)

        log.info("qBit renameFile: %s -> %s", old_rel_str, new_rel_str)
        new_path.parent.mkdir(parents=True, exist_ok=True)

        ok = qbit.rename_file(torrent_hash, old_rel_str, new_rel_str)
        if not ok:
            raise RuntimeError(
                f"qBit renameFile failed: {old_rel_str} -> {new_rel_str}"
            )

        time.sleep(0.5)
        _refresh_tracked()

    return AnimeRenamer(cfg, rename_via_qbit=rename_via_qbit)


# ══════════════════════════ MAIN ═════════════════════════
def process_torrent(
    qbit: QBitClient, torrent_hash: str, torrent_name: str
) -> None:
    log.info("=" * 60)
    log.info(
        "Processing torrent — hash=%s name=%s", torrent_hash, torrent_name
    )

    # 1. Resolve the real title from Nyaa
    real_title = lookup_nyaa_title(torrent_hash, torrent_name)

    # 2. Extract the series name
    series_name = extract_series_name(real_title)
    if not series_name:
        log.error("Could not determine series name — skipping.")
        return

    log.info("Series name: %s", series_name)

    # 3. Build config with overrides from JSON & .env
    # Check if there are series-specific config overrides in HOOK_SERIES
    series_conf = HOOK_SERIES.get(series_name, {})
    # If not found, try by Nyaa / official name if resolved, or lookup case-insensitive
    if not series_conf:
        for k, v in HOOK_SERIES.items():
            if k.lower() == series_name.lower():
                series_conf = v
                break

    tmdb_id_from_env = series_conf.get("tmdb_series_id") or _parse_optional_int(
        os.getenv("TMDB_SERIES_ID", "")
    )
    anilist_id_from_env = series_conf.get("anilist_id") or _parse_optional_int(
        os.getenv("ANILIST_ID", "")
    )
    kitsu_id_from_env = series_conf.get("kitsu_id") or _parse_optional_int(
        os.getenv("KITSU_ID", "")
    )
    series_name_from_env = series_conf.get("series_name") or os.getenv("SERIES_NAME", "").strip()

    # 4. Search provider for series ID — skip if already set
    official_name = series_name_from_env or series_name
    tmdb_id = tmdb_id_from_env
    anilist_id = anilist_id_from_env
    kitsu_id = kitsu_id_from_env

    # Active provider for this series
    provider = ACTIVE_PROVIDER
    if "provider" in series_conf:
        provider = Provider.from_str(series_conf["provider"])

    should_search = True
    if provider == Provider.TMDB and tmdb_id or provider == Provider.AniList and anilist_id or provider == Provider.Kitsu and kitsu_id:
        should_search = False

    if should_search:
        registry = get_registry()
        search_result = registry.search(provider, series_name, Config.from_env())
        if search_result:
            official_name = search_result.series_name or official_name
            if search_result.tmdb_id:
                tmdb_id = search_result.tmdb_id
            if search_result.anilist_id:
                anilist_id = search_result.anilist_id
            if search_result.kitsu_id:
                kitsu_id = search_result.kitsu_id
            if provider == Provider.TMDB and search_result.series_id:
                tmdb_id = search_result.series_id
            elif provider == Provider.Kitsu and search_result.series_id:
                kitsu_id = search_result.series_id
        else:
            log.warning(
                "Provider search returned nothing for '%s'.", series_name
            )

    log.info(
        "Series: '%s' | TMDB=%s AniList=%s Kitsu=%s",
        official_name, tmdb_id, anilist_id, kitsu_id,
    )

    # 5. Determine & create the series folder
    series_folder = BASE_DOWNLOAD_PATH / sanitize(official_name)
    series_folder.mkdir(parents=True, exist_ok=True)
    log.info("Series folder: %s", series_folder)

    # 6. Move the torrent's save location in qBit
    log.info("Moving torrent save path to: %s", series_folder)
    if not qbit.set_location(torrent_hash, str(series_folder)):
        log.error("Failed to move torrent — skipping.")
        return

    time.sleep(3)

    # Resolve local episode start mode and absolute numbering options
    ep_start_mode = series_conf.get("episode_start_mode", EPISODE_START_MODE)
    abs_numbering = series_conf.get("absolute_numbering", ABSOLUTE_NUMBERING)
    ep_group_id = series_conf.get("episode_group_id") or os.getenv("EPISODE_GROUP_ID", "").strip() or None

    # 7. Rename & organise via the renamer
    cfg = Config.from_env(
        series_name=official_name,
        tmdb_series_id=tmdb_id,
        anilist_id=anilist_id,
        kitsu_id=kitsu_id,
        media_dir=series_folder,
        organize_into_folders=True,
        provider=provider,
        episode_group_id=ep_group_id,
        episode_start_mode=ep_start_mode,
        absolute_numbering=abs_numbering,
    )

    log.info("Starting renamer on: %s", series_folder)
    renamer = build_qbit_renamer(qbit, torrent_hash, series_folder, cfg)
    results = renamer.run(dry_run=False)

    done = sum(1 for r in results if r.success)
    errors = sum(1 for r in results if r.error)

    log.info(
        "Done — renamed %d file(s), %d error(s) | hash=%s",
        done, errors, torrent_hash,
    )

    # 8. Set folder icon from provider poster
    try:
        if set_folder_icon(series_folder, cfg):
            log.info("Folder icon set for: %s", series_folder)
        else:
            log.info("No folder icon available for: %s", series_folder)
    except Exception as exc:
        log.warning("Folder icon setting failed (non-fatal): %s", exc)

    log.info("=" * 60)


def main() -> None:
    # 1. Connect to qBittorrent
    qbit = QBitClient(QBIT_URL, QBIT_USERNAME, QBIT_PASSWORD)

    # 2. Parse arguments or fallback to the last completed torrent
    n_limit = None
    target_hash = None

    if "--hash" in sys.argv:
        try:
            idx = sys.argv.index("--hash")
            target_hash = sys.argv[idx + 1]
        except IndexError:
            log.error("Missing hash value. Usage: python qbit_hook.py --hash <hash>")
            sys.exit(1)
    elif "-hash" in sys.argv:
        try:
            idx = sys.argv.index("-hash")
            target_hash = sys.argv[idx + 1]
        except IndexError:
            log.error("Missing hash value. Usage: python qbit_hook.py -hash <hash>")
            sys.exit(1)

    if "-n" in sys.argv:
        try:
            idx = sys.argv.index("-n")
            n_limit = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            log.error(
                "Invalid -n argument value. Usage: python qbit_hook.py -n <number>"
            )
            sys.exit(1)

    if target_hash:
        log.info("Explicit hash requested: %s", target_hash)
        t = qbit.torrent_info(target_hash)
        if not t:
            log.error("Could not find torrent with hash %s in qBittorrent.", target_hash)
            sys.exit(1)
        process_torrent(qbit, t["hash"], t["name"])
    elif n_limit is not None or len(sys.argv) < 3:
        limit = n_limit if n_limit is not None else 1
        log.info(
            "No explicit trigger arguments. Checking the last %d completed torrents...",
            limit,
        )
        targets = qbit.get_last_completed_torrents(limit)
        if not targets:
            log.error(
                "Could not find any completed torrents in qBittorrent."
            )
            sys.exit(1)

        log.info("Found %d completed torrent(s) to process.", len(targets))
        for t in targets:
            process_torrent(qbit, t["hash"], t["name"])
    else:
        torrent_hash = sys.argv[1]
        torrent_name = sys.argv[2]
        process_torrent(qbit, torrent_hash, torrent_name)


if __name__ == "__main__":
    main()
