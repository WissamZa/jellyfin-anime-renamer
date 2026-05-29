"""
renamer.cli.multi_series
========================
Multi-series folder scanner and batch renaming menu.

Features
--------
* Scans MEDIA_DIR for immediate subfolders (each assumed to be one anime series).
* Auto-identifies the series title and provider ID by searching the active
  provider using the folder name.
* Presents a MultiPicker so the user can pick Single / Multiple / All series.
* For each selected series, creates an isolated Config + AnimeRenamer and runs
  Dry Run or Live Rename.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from pathlib import Path

from renamer.cache import SeriesCache
from renamer.config import Config, Provider, get_logger
from renamer.icons import has_folder_icon, set_folder_icon
from renamer.picker import MultiPicker, Picker
from renamer.providers.registry import get_registry
from renamer.renamer import AnimeRenamer, sanitize_name

log = get_logger()


# ---------------------------------------------------------------------------
# Central SQLite Database Cache Manager
# ---------------------------------------------------------------------------

class LibraryDBCache:
    """
    Manages a central SQLite database cache (anime_library.db) in MEDIA_DIR
    to avoid repeated network lookups and check for newly-added series folders.
    """

    def __init__(self, media_dir: Path):
        self.db_path = media_dir / "anime_library.db"

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # Create cache table if it doesn't exist
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS series_cache (
                    folder_name   TEXT PRIMARY KEY,
                    resolved_name TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    tmdb_id       INTEGER,
                    anilist_id    INTEGER,
                    kitsu_id      INTEGER,
                    updated_at    TEXT DEFAULT (datetime('now'))
                );
            """)
        return conn

    def get(self, folder_name: str) -> dict | None:
        """Fetch cached series metadata by folder name."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM series_cache WHERE folder_name = ?",
                    (folder_name,),
                ).fetchone()
                if row:
                    return dict(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("Database cache read error for '%s': %s", folder_name, exc)
        return None

    def save(
        self,
        folder_name: str,
        resolved_name: str,
        provider: Provider,
        tmdb_id: int | None = None,
        anilist_id: int | None = None,
        kitsu_id: int | None = None,
    ) -> None:
        """Insert or update series metadata in the central cache."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO series_cache
                    (folder_name, resolved_name, provider, tmdb_id, anilist_id, kitsu_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (folder_name, resolved_name, provider.value, tmdb_id, anilist_id, kitsu_id),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Database cache save error for '%s': %s", folder_name, exc)


# ---------------------------------------------------------------------------
# Data class for discovered series
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class DiscoveredSeries:
    """A series folder discovered under MEDIA_DIR."""
    folder: Path            # absolute path to the series folder
    folder_name: str        # raw folder name (used as the search query)
    resolved_name: str      # best title after provider lookup (may equal folder_name)
    tmdb_id: int | None = None
    anilist_id: int | None = None
    kitsu_id: int | None = None
    provider: Provider = Provider.TMDB


# ---------------------------------------------------------------------------
# Folder scanner
# ---------------------------------------------------------------------------

def scan_series_folders(media_dir: Path, cfg: Config | None = None) -> list[Path]:
    """
    Return all immediate subfolders of *media_dir* that look like anime series
    (i.e. contain at least one video file directly inside them, without
    recursing into deeper sub-folders).

    Only checks for video files in the folder itself (not in nested
    sub-directories) so that the selected folder is used exclusively —
    the scanner will not pick up files from unrelated sub-folders.
    """
    video_exts = set(cfg.video_extensions) if cfg else {".mkv", ".mp4", ".avi", ".m4v", ".flv", ".webm"}
    folders: list[Path] = []

    if not media_dir.is_dir():
        log.error("MEDIA_DIR does not exist: %s", media_dir)
        return folders

    for entry in sorted(media_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip hidden / system folders
        if entry.name.startswith("."):
            continue
        # Must contain at least one video file directly inside
        has_video = any(
            f.suffix.lower() in video_exts
            for f in entry.iterdir()
            if f.is_file()
        )
        # Also accept folders whose *immediate* sub-folders contain videos
        # (e.g. a folder that only has Season 01/ sub-folder with videos)
        if not has_video:
            has_video = any(
                f.suffix.lower() in video_exts
                for sub in entry.iterdir()
                if sub.is_dir() and not sub.name.startswith(".")
                for f in sub.iterdir()
                if f.is_file()
            )
        if has_video:
            folders.append(entry)

    return folders


# ---------------------------------------------------------------------------
# Auto-identify series via provider search
# ---------------------------------------------------------------------------

def _clean_folder_name(raw: str) -> str:
    """
    Heuristically clean a folder name into a searchable series title.

    Handles complex anime folder names like:
      [Judas] Romaji Name (English Name) (Season 01) [1080p][HEVC x265 10bit][Multi-Sub]

    Strips common patterns like:
      - Sub-group tags at the start: [SubGroup] ...
      - Season tags: (Season 01)
      - Codec/resolution brackets: [1080p], [HEVC x265 10bit], [Multi-Sub]
      - Language/source tags: [JPN], [ENG], [www]
      - Junk bracket tags: [1920x1080], [AAC], [FLAC]
      - Year in parentheses: (2002)
      - English/alternate title in parentheses: Name (English Title)
      - Underscores / dots used as spaces

    This mirrors the logic in renamer.renamer._clean_folder_name but is
    kept independent to avoid circular imports.
    """
    import re

    name = raw.replace("_", " ").replace(".", " ")

    # Strip leading [SubGroup] tag
    name = re.sub(r"^\[[^\]]+\]\s*", "", name)

    # Strip season tags: (Season 01), (Season 1)
    name = re.sub(r"\s*\(\s*Season\s+\d+\s*\)", "", name, flags=re.IGNORECASE)

    # Strip codec/resolution/quality brackets
    _codec_re = re.compile(
        r"\s*\[(?:"
        r"[^\]]*?(?:1080p|720p|480p|2160p|4K|HEVC|x264|x265|AV1|10bit|8bit|Hi10|"
        r"WEB|BD|DVD|Multi-Sub|Dual-Audio|Batch|Complete)"
        r")[^\]]*\]",
        re.IGNORECASE,
    )
    prev = None
    while prev != name:
        prev = name
        name = _codec_re.sub("", name)

    # Strip language/source tags: [JPN], [ENG], [www], [v2]
    _lang_re = re.compile(
        r"\s*\[(?:"
        r"[A-Z]{2,4}"          # 2-4 letter uppercase codes: JPN, ENG, CHS, CHT, RAW
        r"|www"                # www source tag
        r"|v\d+"               # version tags: v2, v3
        r")\s*\]",
        re.IGNORECASE,
    )
    prev = None
    while prev != name:
        prev = name
        name = _lang_re.sub("", name)

    # Strip junk bracket tags: [1920x1080], [AAC], [FLAC], [10bit], [v2]
    _junk_re = re.compile(
        r"\s*\[(?:"
        r"\d{3,4}x\d{3,4}"    # resolution: 1920x1080
        r"|[A-Z]{2,5}"        # short uppercase: JPN, ENG, WWW, AAC, FLAC
        r"|\d+bit"            # 10bit, 8bit
        r"|v\d+"              # v2
        r"|(?:Hi)?10"         # Hi10, 10
        r")\s*\]",
        re.IGNORECASE,
    )
    prev = None
    while prev != name:
        prev = name
        name = _junk_re.sub("", name)

    # Strip remaining trailing bracket tags
    prev = None
    while prev != name:
        prev = name
        name = re.sub(r"\s*\[[^\]]+\]\s*$", "", name)

    # Strip year in parentheses
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)

    name = name.strip()

    # If there's a parenthesised English/alternate title at the end,
    # keep only the main (romaji) part for the primary search
    m = re.search(r"\s*\(([^)]+)\)\s*$", name)
    if m:
        alt = m.group(1).strip()
        main = name[:m.start()].strip()
        if alt and main and not re.match(r"^\d{4}$", alt):
            name = main

    return name


def auto_identify_series(
    folder: Path,
    base_cfg: Config,
    db_cache: LibraryDBCache,
) -> DiscoveredSeries:
    """
    Use the central SQLite DB, local folder cache, or provider network search
    to resolve the series name and IDs for *folder*.
    """
    folder_name = folder.name
    search_name = _clean_folder_name(folder_name)

    # 1. Check central SQLite database cache first
    db_hit = db_cache.get(folder_name)
    if db_hit:
        log.info("Database cache hit for '%s': %s", folder_name, db_hit)
        provider = (
            Provider.from_str(db_hit["provider"])
            if isinstance(db_hit.get("provider"), str)
            else base_cfg.provider
        )
        return DiscoveredSeries(
            folder=folder,
            folder_name=folder_name,
            resolved_name=db_hit.get("resolved_name") or search_name,
            tmdb_id=db_hit.get("tmdb_id"),
            anilist_id=db_hit.get("anilist_id"),
            kitsu_id=db_hit.get("kitsu_id"),
            provider=provider,
        )

    # 2. Check local per-folder series cache fallback
    cache = SeriesCache(folder)
    cached = cache.load()
    if cached:
        provider = (
            Provider.from_str(cached["provider"])
            if isinstance(cached.get("provider"), str)
            else (cached.get("provider") or base_cfg.provider)
        )
        log.info("Per-folder cache hit for '%s': %s", folder_name, cached)
        resolved = DiscoveredSeries(
            folder=folder,
            folder_name=folder_name,
            resolved_name=cached.get("series_name") or search_name,
            tmdb_id=cached.get("tmdb_id") or cached.get("tmdb_series_id"),
            anilist_id=cached.get("anilist_id"),
            kitsu_id=cached.get("kitsu_id"),
            provider=provider,
        )
        # Seed central database with this per-folder cache
        db_cache.save(
            folder_name=folder_name,
            resolved_name=resolved.resolved_name,
            provider=resolved.provider,
            tmdb_id=resolved.tmdb_id,
            anilist_id=resolved.anilist_id,
            kitsu_id=resolved.kitsu_id,
        )
        return resolved

    # 3. Network search (cache miss)
    # Try multiple search queries in order: cleaned name, English alt title, filename extraction
    search_queries = [search_name]

    # Extract English/alternate title from folder name
    _alt_re = re.compile(r"\s*\(([^)]+)\)\s*$")
    raw_cleaned = folder_name.replace("_", " ").replace(".", " ")
    raw_cleaned = re.sub(r"^\[[^\]]+\]\s*", "", raw_cleaned)
    raw_cleaned = re.sub(r"\s*\(\s*Season\s+\d+\s*\)", "", raw_cleaned, flags=re.IGNORECASE)
    m = _alt_re.search(raw_cleaned.strip())
    if m:
        alt = m.group(1).strip()
        if alt and not re.match(r"^\d{4}$", alt) and alt != search_name:
            search_queries.append(alt)

    registry = get_registry()
    result = None
    for query in search_queries:
        try:
            result = registry.search(base_cfg.provider, query, base_cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Search failed for '%s': %s — using folder name", query, exc)
            result = None
        if result:
            break

    if result:
        log.info(
            "Auto-identified '%s' -> '%s' (provider=%s id=%s)",
            search_name, result.series_name, result.provider, result.series_id,
        )
        tmdb_id = result.tmdb_id or (
            result.series_id if base_cfg.provider == Provider.TMDB else None
        )
        anilist_id = result.anilist_id
        kitsu_id = result.kitsu_id or (
            result.series_id if base_cfg.provider == Provider.Kitsu else None
        )
        resolved_name = result.series_name or search_name

        # Save to both central SQLite DB and per-folder cache
        db_cache.save(
            folder_name=folder_name,
            resolved_name=resolved_name,
            provider=base_cfg.provider,
            tmdb_id=tmdb_id,
            anilist_id=anilist_id,
            kitsu_id=kitsu_id,
        )

        try:
            cache.save(
                series_name=resolved_name,
                provider=base_cfg.provider,
                tmdb_series_id=tmdb_id,
                anilist_id=anilist_id,
                kitsu_id=kitsu_id,
            )
            log.debug("Scan result cached for '%s'", folder_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not cache scan result for '%s': %s", folder_name, exc)

        return DiscoveredSeries(
            folder=folder,
            folder_name=folder_name,
            resolved_name=resolved_name,
            tmdb_id=tmdb_id,
            anilist_id=anilist_id,
            kitsu_id=kitsu_id,
            provider=base_cfg.provider,
        )

    # Cache failure to folder name so we don't spam requests next time
    db_cache.save(
        folder_name=folder_name,
        resolved_name=search_name,
        provider=base_cfg.provider,
    )

    log.warning("Could not auto-identify '%s' — using folder name.", search_name)
    return DiscoveredSeries(
        folder=folder,
        folder_name=folder_name,
        resolved_name=search_name,
        provider=base_cfg.provider,
    )


def interactive_search_series(
    folder: Path,
    base_cfg: Config,
    db_cache: LibraryDBCache,
) -> DiscoveredSeries | None:
    """
    Interactive fallback: let the user manually search for a series when
    auto-identification fails.

    Presents options:
      1. Search TMDB with a custom query
      2. Use the cleaned folder name (LocalFetcher fallback)
      3. Skip this series

    Returns a DiscoveredSeries if resolved, or None if the user skips.
    """
    folder_name = folder.name
    search_name = _clean_folder_name(folder_name)

    while True:
        options = [
            (f"Search TMDB with custom query (current: '{search_name}')", "search"),
            (f"Use folder name as-is: '{search_name}' (local mode)", "local"),
            ("Skip this series", "skip"),
        ]

        result = Picker(
            options,
            title=f"MANUAL SEARCH FOR: {folder_name}",
            default_index=0,
        ).run()

        if result is None or result[1] == "skip":
            return None

        action = result[1]

        if action == "local":
            # Use folder name with LocalFetcher
            db_cache.save(
                folder_name=folder_name,
                resolved_name=search_name,
                provider=base_cfg.provider,
            )
            return DiscoveredSeries(
                folder=folder,
                folder_name=folder_name,
                resolved_name=search_name,
                provider=base_cfg.provider,
            )

        if action == "search":
            # Let user type a custom search query
            custom_query = input(
                f"  Enter search query [{search_name}]: "
            ).strip()
            if not custom_query:
                custom_query = search_name

            # Search TMDB
            registry = get_registry()
            if base_cfg.provider == Provider.TMDB and base_cfg.tmdb_api_key:
                results = registry.search_multi(
                    Provider.TMDB, custom_query, base_cfg, limit=10,
                )

                if not results:
                    print(f"  No TMDB results for '{custom_query}'. Try another query.")
                    continue

                if len(results) == 1:
                    r = results[0]
                    resolved_name = r.get("romaji") or r.get("name") or search_name
                    tmdb_id = r.get("id")
                    print(f"  Found: {resolved_name} (TMDB ID: {tmdb_id})")
                else:
                    # Show picker with multiple results
                    pick_options: list[tuple[str, dict]] = []
                    for r in results:
                        name_val = r.get("name", "")
                        romaji = r.get("romaji", "")
                        orig = r.get("original_name", "")
                        year = r.get("first_air_date", "")[:4]
                        country = ", ".join(r.get("origin_country", []))
                        overview = r.get("overview", "")
                        parts = []
                        if romaji and romaji != name_val:
                            parts.append(romaji)
                        if name_val:
                            parts.append(name_val)
                        if orig and orig != name_val and orig != romaji:
                            parts.append(f"({orig})")
                        label = " / ".join(parts)
                        if year:
                            label += f"  [{year}]"
                        if country:
                            label += f"  [{country}]"
                        if overview:
                            label += f"  — {overview[:80]}{'…' if len(overview) > 80 else ''}"
                        pick_options.append((label, r))

                    pick_result = Picker(
                        pick_options,
                        title=f"SEARCH RESULTS FOR: {custom_query}",
                        default_index=0,
                    ).run()

                    if pick_result is None:
                        print("  Search cancelled. Try another query.")
                        continue

                    r = pick_result[1]
                    resolved_name = r.get("romaji") or r.get("name") or search_name
                    tmdb_id = r.get("id")
                    print(f"  Selected: {resolved_name} (TMDB ID: {tmdb_id})")

                # Save to caches
                series = DiscoveredSeries(
                    folder=folder,
                    folder_name=folder_name,
                    resolved_name=resolved_name,
                    tmdb_id=tmdb_id,
                    provider=base_cfg.provider,
                )

                db_cache.save(
                    folder_name=folder_name,
                    resolved_name=resolved_name,
                    provider=base_cfg.provider,
                    tmdb_id=tmdb_id,
                )

                try:
                    cache = SeriesCache(folder)
                    cache.save(
                        series_name=resolved_name,
                        provider=base_cfg.provider,
                        tmdb_series_id=tmdb_id,
                    )
                except Exception as exc:
                    log.warning("Could not cache result for '%s': %s", folder_name, exc)

                return series
            else:
                print("  TMDB API key not configured. Cannot search.")
                continue


# ---------------------------------------------------------------------------
# Build a series-specific Config
# ---------------------------------------------------------------------------

def _make_series_config(series: DiscoveredSeries, base_cfg: Config) -> Config:
    """Create a Config scoped to a single discovered series folder."""
    return dataclasses.replace(
        base_cfg,
        series_name=series.resolved_name,
        media_dir=series.folder,
        tmdb_series_id=series.tmdb_id,
        anilist_id=series.anilist_id,
        kitsu_id=series.kitsu_id,
        provider=series.provider,
        # Reset episode-group so it doesn't bleed across series
        episode_group_id=None,
        # Preserve base_download_path for folder rename security check
        base_download_path=base_cfg.base_download_path,
    )


# ---------------------------------------------------------------------------
# Main entry point called from the CLI menu
# ---------------------------------------------------------------------------

def run_multi_series_menu(base_cfg: Config) -> None:
    """
    Interactive flow:
      1. Scan MEDIA_DIR for series subfolders.
      2. Auto-identify each series (with a progress indicator).
      3. Present a MultiPicker so the user picks which series to process.
      4. Ask Dry Run / Live Rename.
      5. Process each selected series in sequence.

    Only the selected folders are processed — no other folders are touched.

    If media_dir itself contains video files (the user navigated into a
    specific anime folder), it treats that folder as the single series
    to process instead of scanning for subfolders.
    """
    media_dir = base_cfg.media_dir

    # Check if media_dir itself contains video files — treat it as the
    # series folder directly rather than scanning for subfolders
    video_exts = set(base_cfg.video_extensions)
    has_vid_directly = any(
        f.suffix.lower() in video_exts
        for f in media_dir.iterdir()
        if f.is_file()
    ) if media_dir.is_dir() else False

    if has_vid_directly:
        # The current folder IS the series folder — process it directly
        print("\n  Current folder contains video files — treating as a single series.")
        print(f"  Folder: {media_dir}")

        db_cache = LibraryDBCache(media_dir.parent)
        series = auto_identify_series(media_dir, base_cfg, db_cache)

        status = f"  {series.resolved_name}"
        if series.tmdb_id:
            status += f"  [TMDB:{series.tmdb_id}]"
        elif series.anilist_id:
            status += f"  [AL:{series.anilist_id}]"
        elif series.kitsu_id:
            status += f"  [Kitsu:{series.kitsu_id}]"
        else:
            status += "  [ID unknown — will search interactively]"
        print(status)

        discovered = [series]
    else:
        print(f"\n  Scanning for series in: {media_dir}")
        folders = scan_series_folders(media_dir, cfg=base_cfg)

        if not folders:
            print("  No anime series folders found.")
            print("  Try navigating to a specific anime folder first (option 5 in the menu).")
            return

        print(f"  Found {len(folders)} folder(s). Auto-identifying titles …\n")

        # Auto-identify with live progress output
        db_cache = LibraryDBCache(media_dir)
        discovered: list[DiscoveredSeries] = []
        for i, folder in enumerate(folders, 1):
            print(f"  [{i}/{len(folders)}] {folder.name} … ", end="", flush=True)
            try:
                series = auto_identify_series(folder, base_cfg, db_cache)
            except Exception as exc:  # noqa: BLE001
                log.warning("Unexpected error identifying '%s': %s", folder.name, exc)
                series = DiscoveredSeries(
                    folder=folder,
                    folder_name=folder.name,
                    resolved_name=_clean_folder_name(folder.name),
                    provider=base_cfg.provider,
                )
            discovered.append(series)
            status = f"✓ {series.resolved_name}"
            if series.tmdb_id:
                status += f"  [TMDB:{series.tmdb_id}]"
            elif series.anilist_id:
                status += f"  [AL:{series.anilist_id}]"
            elif series.kitsu_id:
                status += f"  [Kitsu:{series.kitsu_id}]"
            else:
                status += "  [ID unknown — will search interactively]"
            print(status)

    print()

    # Build picker labels
    options: list[tuple[str, DiscoveredSeries]] = []
    for s in discovered:
        id_tag = ""
        if s.tmdb_id:
            id_tag = f"  TMDB:{s.tmdb_id}"
        elif s.anilist_id:
            id_tag = f"  AL:{s.anilist_id}"
        elif s.kitsu_id:
            id_tag = f"  Kitsu:{s.kitsu_id}"
        label = f"{s.resolved_name}{id_tag}  [{s.folder.name}]"
        options.append((label, s))

    choices = MultiPicker(
        options,
        title="SELECT SERIES TO PROCESS",
    ).run()

    if choices is None:
        print("  Cancelled.")
        return

    if not choices:
        print("  No series selected.")
        return

    selected = [s for _, s in choices]
    print(f"\n  Selected {len(selected)} series.")

    # Ask Dry Run vs Live
    mode_result = Picker(
        [
            ("Dry Run  (preview only)", "dry"),
            ("Live Rename + Organise", "live"),
        ],
        title="RENAME MODE",
        default_index=0,
    ).run()

    if mode_result is None:
        print("  Cancelled.")
        return

    dry_run = mode_result[1] == "dry"

    if not dry_run:
        print("\n  WARNING: This will rename and move files on disk for ALL selected series.")
        if input("  Continue? (y/N): ").strip().lower() != "y":
            print("  Cancelled.")
            return

    # Process each series
    total_done = 0
    total_errors = 0
    for i, series in enumerate(selected, 1):
        print(f"\n{'─' * 56}")
        print(f"  [{i}/{len(selected)}] {series.resolved_name}")
        print(f"  Folder: {series.folder}")
        print(f"{'─' * 56}")

        if not series.tmdb_id and not series.anilist_id and not series.kitsu_id:
            # Auto-identification failed — offer interactive search
            print(f"\n  Could not auto-identify: {series.resolved_name}")
            print(f"  Folder: {series.folder.name}")
            resolved = interactive_search_series(
                series.folder, base_cfg, db_cache,
            )
            if resolved is None:
                print("  Skipping this series.")
                total_errors += 1
                continue
            # Replace with the resolved series
            series = resolved

        try:
            cfg = _make_series_config(series, base_cfg)
            renamer = AnimeRenamer(cfg)
            results = renamer.run(dry_run=dry_run)
            done = sum(1 for r in results if r.success)
            errors = sum(1 for r in results if r.error)
            total_done += done
            total_errors += errors

            # Set folder icon after renaming (live mode only)
            if not dry_run and not has_folder_icon(series.folder):
                try:
                    if set_folder_icon(series.folder, cfg):
                        print("  Folder icon set.")
                except Exception as icon_exc:
                    log.debug("Icon setting failed (non-fatal): %s", icon_exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Error processing '%s': %s", series.resolved_name, exc)
            print(f"  ! Failed: {exc}")
            total_errors += 1

    # Summary
    mode_label = "Would rename" if dry_run else "Renamed"
    print(f"\n{'═' * 56}")
    print(f"  BATCH COMPLETE — {len(selected)} series")
    print(f"  {mode_label}: {total_done} files  |  Errors: {total_errors}")
    print(f"{'═' * 56}\n")


# ---------------------------------------------------------------------------
# Rename Folders Only (no file renaming)
# ---------------------------------------------------------------------------

def run_rename_folders_menu(base_cfg: Config) -> None:
    """
    Batch-rename anime folder names to their proper TMDB/AniList/Kitsu titles.

    This ONLY renames the folder itself — no files inside are touched.
    Useful for cleaning up folder names like::

        [Judas] Botsuraku Yotei... (I'm a Noble...) (Season 01) [1080p]

    into the proper series title::

        Botsuraku Yotei no Kizoku Dakedo, Hima Datta kara Mahou o Kiwamete Mita

    Flow:
      1. Scan MEDIA_DIR (or BASE_DOWNLOAD_PATH) for anime subfolders.
      2. Auto-identify each series using TMDB / the active provider.
      3. Compute the new folder name from the resolved series title.
      4. Present a MultiPicker showing: old_name -> new_name.
      5. Rename the selected folders (with security check against BASE_DOWNLOAD_PATH).
    """
    import os

    media_dir = base_cfg.media_dir

    # Use BASE_DOWNLOAD_PATH as the scan root if it's set and media_dir
    # isn't already a subfolder with video files.
    scan_dir = media_dir
    base_download_path = base_cfg.base_download_path
    if base_download_path is None:
        raw = os.getenv("BASE_DOWNLOAD_PATH", "").strip()
        if raw:
            base_download_path = Path(raw).resolve()
    if base_download_path and base_download_path.is_dir():
        try:
            media_dir.relative_to(base_download_path)
        except ValueError:
            scan_dir = base_download_path
        else:
            scan_dir = media_dir
    # If media_dir itself contains video files, scan its parent instead
    video_exts = set(base_cfg.video_extensions)
    has_vid_directly = any(
        f.suffix.lower() in video_exts
        for f in scan_dir.iterdir()
        if f.is_file()
    )
    if has_vid_directly:
        # media_dir is itself an anime folder — scan its parent
        parent = scan_dir.parent
        if base_download_path:
            try:
                parent.relative_to(base_download_path)
                scan_dir = parent
            except ValueError:
                pass  # stay at media_dir
        else:
            scan_dir = parent

    print(f"\n  Scanning for series folders in: {scan_dir}")
    folders = scan_series_folders(scan_dir, cfg=base_cfg)

    if not folders:
        print("  No anime series folders found.")
        return

    print(f"  Found {len(folders)} folder(s). Auto-identifying titles …\n")

    db_cache = LibraryDBCache(scan_dir)
    discovered: list[DiscoveredSeries] = []

    for i, folder in enumerate(folders, 1):
        print(f"  [{i}/{len(folders)}] {folder.name} … ", end="", flush=True)
        try:
            series = auto_identify_series(folder, base_cfg, db_cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("Error identifying '%s': %s", folder.name, exc)
            series = DiscoveredSeries(
                folder=folder,
                folder_name=folder.name,
                resolved_name=_clean_folder_name(folder.name),
                provider=base_cfg.provider,
            )
        discovered.append(series)

        new_name = sanitize_name(series.resolved_name)
        changed = " -> " + new_name if new_name != folder.name else " (already OK)"
        id_tag = ""
        if series.tmdb_id:
            id_tag = f"  [TMDB:{series.tmdb_id}]"
        elif series.anilist_id:
            id_tag = f"  [AL:{series.anilist_id}]"
        elif series.kitsu_id:
            id_tag = f"  [Kitsu:{series.kitsu_id}]"
        else:
            id_tag = "  [ID unknown — search manually]"
        print(f"✓{changed}{id_tag}")

    print()

    # Build picker labels showing old -> new name
    options: list[tuple[str, DiscoveredSeries]] = []
    for s in discovered:
        new_name = sanitize_name(s.resolved_name)
        old_name = s.folder.name
        if new_name == old_name:
            label = f"{old_name}  (already OK)"
        else:
            label = f"{old_name}  ->  {new_name}"
        id_tag = ""
        if s.tmdb_id:
            id_tag = f"  TMDB:{s.tmdb_id}"
        elif s.anilist_id:
            id_tag = f"  AL:{s.anilist_id}"
        elif s.kitsu_id:
            id_tag = f"  Kitsu:{s.kitsu_id}"
        label += id_tag
        options.append((label, s))

    # Pre-select only folders that would actually change
    preselected = [
        i for i, (_, s) in enumerate(options)
        if sanitize_name(s.resolved_name) != s.folder.name
    ]

    choices = MultiPicker(
        options,
        title="SELECT FOLDERS TO RENAME",
        preselected=preselected,
    ).run()

    if choices is None:
        print("  Cancelled.")
        return

    if not choices:
        print("  No folders selected.")
        return

    selected = [s for _, s in choices]

    # Ask Dry Run vs Live
    mode_result = Picker(
        [
            ("Dry Run  (preview only)", "dry"),
            ("Live Rename folders", "live"),
        ],
        title="FOLDER RENAME MODE",
        default_index=0,
    ).run()

    if mode_result is None:
        print("  Cancelled.")
        return

    dry_run = mode_result[1] == "dry"

    if not dry_run:
        print(f"\n  WARNING: This will rename {len(selected)} folder(s) on disk.")
        print("  Files inside are NOT affected — only the folder name changes.")
        if input("  Continue? (y/N): ").strip().lower() != "y":
            print("  Cancelled.")
            return

    # Determine base_download_path for security checks
    if base_download_path is None:
        raw = os.getenv("BASE_DOWNLOAD_PATH", "").strip()
        if raw:
            base_download_path = Path(raw).resolve()

    # Process each selected folder
    total_renamed = 0
    total_skipped = 0
    total_errors = 0

    for i, series in enumerate(selected, 1):
        folder = series.folder.resolve()

        # If no provider ID was found, offer interactive search
        if not series.tmdb_id and not series.anilist_id and not series.kitsu_id:
            print(f"\n  [{i}/{len(selected)}] No provider ID for: {folder.name}")
            resolved = interactive_search_series(
                folder, base_cfg, db_cache,
            )
            if resolved is None:
                print("  Skipping this folder.")
                total_skipped += 1
                continue
            series = resolved

        new_name = sanitize_name(series.resolved_name)

        if new_name == folder.name:
            print(f"  [{i}/{len(selected)}] {folder.name}  — already OK, skipped.")
            total_skipped += 1
            continue

        new_path = folder.parent / new_name

        # Security check
        if base_download_path:
            try:
                folder.relative_to(base_download_path)
            except ValueError:
                log.warning(
                    "Folder rename skipped: %s is not within BASE_DOWNLOAD_PATH (%s)",
                    folder, base_download_path,
                )
                print(f"  [{i}/{len(selected)}] {folder.name}  — SKIPPED (outside BASE_DOWNLOAD_PATH)")
                total_skipped += 1
                continue

        # Check target doesn't already exist
        if new_path.exists() and new_path != folder:
            log.warning("Target folder already exists: %s", new_path)
            print(f"  [{i}/{len(selected)}] {folder.name}  — SKIPPED (target exists: {new_name})")
            total_skipped += 1
            continue

        if dry_run:
            print(f"  [{i}/{len(selected)}] {folder.name}  ->  {new_name}  (dry run)")
            total_renamed += 1
            continue

        try:
            old_resolved = folder
            folder.rename(new_path)
            log.info("FOLDER RENAMED  |  %s  ->  %s", old_resolved.name, new_name)
            print(f"  [{i}/{len(selected)}] {old_resolved.name}  ->  {new_name}")

            # Update per-folder cache to reflect new path
            new_cache = SeriesCache(new_path)
            new_cache.save(
                series_name=series.resolved_name,
                provider=series.provider,
                tmdb_series_id=series.tmdb_id,
                anilist_id=series.anilist_id,
                kitsu_id=series.kitsu_id,
            )

            # Update DB cache with new resolved name
            db_cache.save(
                folder_name=old_resolved.name,
                resolved_name=series.resolved_name,
                provider=series.provider,
                tmdb_id=series.tmdb_id,
                anilist_id=series.anilist_id,
                kitsu_id=series.kitsu_id,
            )
            # Also save under the new folder name so future scans find it
            db_cache.save(
                folder_name=new_name,
                resolved_name=series.resolved_name,
                provider=series.provider,
                tmdb_id=series.tmdb_id,
                anilist_id=series.anilist_id,
                kitsu_id=series.kitsu_id,
            )

            # Update cfg.media_dir if it matched the old folder path
            if base_cfg.media_dir.resolve() == old_resolved:
                base_cfg.media_dir = new_path

            total_renamed += 1
        except OSError as exc:
            log.error("Failed to rename folder '%s': %s", folder, exc)
            print(f"  [{i}/{len(selected)}] {folder.name}  — FAILED: {exc}")
            total_errors += 1

    # Summary
    mode_label = "Would rename" if dry_run else "Renamed"
    print(f"\n{'═' * 56}")
    print("  FOLDER RENAME COMPLETE")
    print(f"  {mode_label}: {total_renamed}  |  Skipped: {total_skipped}  |  Errors: {total_errors}")
    print(f"{'═' * 56}\n")
