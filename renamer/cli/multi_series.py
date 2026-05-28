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
import sqlite3
from typing import TYPE_CHECKING

from renamer.cache import SeriesCache
from renamer.config import Config, Provider, get_logger
from renamer.icons import has_folder_icon, set_folder_icon
from renamer.picker import MultiPicker, Picker
from renamer.providers.registry import get_registry
from renamer.renamer import AnimeRenamer

if TYPE_CHECKING:
    from pathlib import Path

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

def scan_series_folders(media_dir: Path) -> list[Path]:
    """
    Return all immediate subfolders of *media_dir* that look like anime series
    (i.e. contain at least one video file somewhere beneath them).
    """
    VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".flv", ".webm"}
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
        # Must contain at least one video file
        has_video = any(
            f.suffix.lower() in VIDEO_EXTS
            for f in entry.rglob("*")
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

    Strips common patterns like:
      - Year in parentheses: "Naruto (2002)"
      - Resolution / quality tags: "[1080p]", "[BD]"
      - Sub-group tags at the start: "[SubGroup] "
      - Underscores / dots used as spaces
    """
    import re
    # Replace underscores/dots with spaces
    name = raw.replace("_", " ").replace(".", " ")
    # Strip leading [SubGroup] tag
    name = re.sub(r"^\[[^\]]+\]\s*", "", name)
    # Strip trailing (year) or [tag]
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
    name = re.sub(r"\s*\[[^\]]+\]\s*$", "", name)
    return name.strip()


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
    registry = get_registry()
    try:
        result = registry.search(base_cfg.provider, search_name, base_cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("Search failed for '%s': %s — using folder name", search_name, exc)
        result = None

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
    """
    media_dir = base_cfg.media_dir

    print(f"\n  Scanning for series in: {media_dir}")
    folders = scan_series_folders(media_dir)

    if not folders:
        print("  No anime series folders found.")
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
            status += "  [ID unknown — will skip]"
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
            print("  ! No provider ID found — skipping (cannot fetch episode list).")
            total_errors += 1
            continue

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
