#!/usr/bin/env -S uv run
"""
qbit_delete_hook.py — called by qBittorrent when a torrent is DELETED WITH files.

qBittorrent setup  (Settings -> Downloads -> "Run external program on torrent deletion"):
    python /path/to/qbit_delete_hook.py "%D" "%N" "%F"

    %D = save path (the directory qBittorrent was saving to)
    %N = torrent name
    %F = content path (the actual file or root folder on disk)

After qBittorrent removes the individual files it tracked, this script
walks UP the directory tree and removes each directory that is now
completely EMPTY.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from renamer import get_logger  # noqa: E402

log = get_logger("qbit_delete_hook")

BASE_DOWNLOAD_PATH = Path(os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent")).resolve()


def remove_empty_parents(start: Path, stop_at: Path) -> None:
    current = start.resolve()
    stop = stop_at.resolve()

    log.info("Cleanup walk: %s  (stopping at %s)", current, stop)

    while True:
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
            current.rmdir()
            log.info("Removed empty directory: %s", current)
            current = current.parent
        except OSError:
            log.info("Directory not empty — leaving intact: %s", current)
            break


def main() -> None:
    if len(sys.argv) < 2:
        log.error(
            "Usage: python qbit_delete_hook.py <save_path> [torrent_name] [content_path]\n"
            'Configure in qBittorrent:  python %s "%%D" "%%N" "%%F"',
            __file__,
        )
        sys.exit(1)

    save_path = Path(sys.argv[1])
    torrent_name = sys.argv[2] if len(sys.argv) > 2 else "<unknown>"
    content_path = Path(sys.argv[3]) if len(sys.argv) > 3 else save_path

    log.info("=" * 60)
    log.info("Torrent deleted : %s", torrent_name)
    log.info("Save path       : %s", save_path)
    log.info("Content path    : %s", content_path)
    log.info("Base path (stop): %s", BASE_DOWNLOAD_PATH)

    if content_path.is_file():
        start = content_path.parent
    elif content_path.is_dir():
        start = content_path
    else:
        start = content_path.parent

    try:
        start.resolve().relative_to(BASE_DOWNLOAD_PATH)
    except ValueError:
        log.warning(
            "Content path is outside BASE_DOWNLOAD_PATH — skipping cleanup.\n"
            "  content_path      : %s\n"
            "  BASE_DOWNLOAD_PATH: %s",
            start,
            BASE_DOWNLOAD_PATH,
        )
        return

    remove_empty_parents(start, BASE_DOWNLOAD_PATH)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
