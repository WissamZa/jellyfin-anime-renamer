#!/usr/bin/env -S uv run
"""
qbit_delete_hook.py — called by qBittorrent when a torrent is DELETED WITH files.

qBittorrent setup  (Settings → Downloads → "Run external program on torrent deletion"):
    python /path/to/qbit_delete_hook.py "%D" "%N" "%F"

    %D = save path (the directory qBittorrent was saving to)
    %N = torrent name
    %F = content path (the actual file or root folder on disk)

What it does
────────────
After qBittorrent removes the individual files it tracked, this script
walks UP the directory tree — starting from the deepest affected folder —
and removes each directory that is now completely EMPTY.

Crucially, it uses os.rmdir() / Path.rmdir(), which raises an OSError if
the directory still contains ANY files or subdirectories.  This means:

    • An empty Season 01/ folder  → removed  ✔
    • A Season 01/ that still has other episodes from a second torrent → left alone  ✔
    • The shared series folder (e.g. One Piece/) with files remaining  → left alone  ✔
    • BASE_DOWNLOAD_PATH itself                                        → never touched ✔

This prevents the common problem where deleting one torrent from a series
that shares a save-path folder with other torrents would wipe the entire
series directory.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from renamer_core import get_logger

log = get_logger("qbit_delete_hook")

# ─────────────────────────── CONFIG ─────────────────────────────────────────
# We stop walking UP the tree at this path; we will never try to remove it.
BASE_DOWNLOAD_PATH = Path(os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent")).resolve()


# ─────────────────────────── HELPERS ────────────────────────────────────────
def remove_empty_parents(start: Path, stop_at: Path) -> None:
    """
    Walk upward from `start`, removing each directory that is empty.
    Stops as soon as a non-empty directory is encountered or we reach `stop_at`.

    Uses Path.rmdir() which:
      - Succeeds  → directory was empty; moves to parent and tries again.
      - OSError   → directory is NOT empty (other files present); stops cleanly.
    """
    current = start.resolve()
    stop    = stop_at.resolve()

    log.info("Cleanup walk: %s  (stopping at %s)", current, stop)

    while True:
        # Never remove the base download root itself
        if current == stop or not current.is_relative_to(stop):
            log.debug("Reached stop boundary — done: %s", current)
            break

        if not current.exists():
            log.debug("Already gone: %s — moving to parent", current)
            current = current.parent
            continue

        if not current.is_dir():
            log.debug("Not a directory: %s — moving to parent", current)
            current = current.parent
            continue

        try:
            current.rmdir()          # atomic; fails if anything is inside
            log.info("✔ Removed empty directory: %s", current)
            current = current.parent
        except OSError:
            # Directory is not empty — other files/torrents are using it.
            log.info("✘ Directory not empty — leaving intact: %s", current)
            break


# ─────────────────────────── MAIN ───────────────────────────────────────────
def main() -> None:
    if len(sys.argv) < 2:
        log.error(
            "Usage: python qbit_delete_hook.py <save_path> [torrent_name] [content_path]\n"
            "Configure in qBittorrent:  python %s \"%%D\" \"%%N\" \"%%F\"",
            __file__,
        )
        sys.exit(1)

    save_path    = Path(sys.argv[1])
    torrent_name = sys.argv[2] if len(sys.argv) > 2 else "<unknown>"
    content_path = Path(sys.argv[3]) if len(sys.argv) > 3 else save_path

    log.info("=" * 60)
    log.info("Torrent deleted : %s", torrent_name)
    log.info("Save path       : %s", save_path)
    log.info("Content path    : %s", content_path)
    log.info("Base path (stop): %s", BASE_DOWNLOAD_PATH)

    # ── Determine where to start the cleanup walk ────────────────────────────
    # If the content path is a file, start from its parent directory.
    # If it's a directory (folder-torrent), start from that directory itself.
    if content_path.is_file():
        start = content_path.parent
    elif content_path.is_dir():
        # The directory might still exist (qBit deleted the files but left the
        # folder structure).  Walk up from there.
        start = content_path
    else:
        # Content path no longer exists — qBit may have already removed it.
        # Start one level up from the last known path.
        start = content_path.parent

    # ── Safety: never operate outside BASE_DOWNLOAD_PATH ────────────────────
    try:
        start.resolve().relative_to(BASE_DOWNLOAD_PATH)
    except ValueError:
        log.warning(
            "Content path is outside BASE_DOWNLOAD_PATH — skipping cleanup.\n"
            "  content_path      : %s\n"
            "  BASE_DOWNLOAD_PATH: %s",
            start, BASE_DOWNLOAD_PATH,
        )
        return

    remove_empty_parents(start, BASE_DOWNLOAD_PATH)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
