#!/usr/bin/env -S uv run
"""
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️  JELLYFIN ANIME RENAMER  🏴‍☠️              ║
║    TMDB + AniList  |  Season folders  |  Undo-safe   ║
╚══════════════════════════════════════════════════════╝

Interactive CLI for manual runs.
Automated use: see qbit_hook.py
"""

import sys
from pathlib import Path

from renamer_core import AnimeRenamer, Config, get_logger

log = get_logger()
CFG = Config()

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
  History         : {CFG.history_file}
  Log file        : renamer.log""")


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
