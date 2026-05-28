"""
renamer.cli.main
================
The main execution loops, CLI argument parsing, and menu routing.
"""

import argparse
import sys
from pathlib import Path

from renamer import __version__
from renamer.config import Config, Provider, get_logger
from renamer.renamer import AnimeRenamer
from renamer.cache import SeriesCache
from renamer.picker import Picker

# CLI submodules
from renamer.cli import menus
from renamer.cli.scanner import scan_library_to_db
from renamer.cli.multi_series import run_multi_series_menu

log = get_logger()

BANNER = f"""
+======================================================+
|       JELLYFIN ANIME RENAMER  v{__version__}              |
|  TMDB + AniList + Kitsu | Episode Groups | Undo-safe  |
+======================================================+"""


def _main_menu_options() -> list[tuple[str, str]]:
    """Return the main menu options."""
    return [
        ("[Run] Dry Run  (preview only)", "dry_run"),
        ("[Run] Live Rename + Organise", "live_run"),
        ("[Scan] Scan folder — pick & rename multiple series  -->", "multi_series"),
        ("[Configure] Provider & Series Identity  -->", "submenu_identity"),
        ("[Utilities] Library Tools  -->", "submenu_utilities"),
        ("[Info] Show current configuration", "show_config"),
        ("Exit", "exit"),
    ]


def _identity_menu_options() -> list[tuple[str, str]]:
    """Return the provider and series configuration options."""
    return [
        ("Switch provider", "switch_provider"),
        ("Set title / provider ID manually", "set_manual_info"),
        ("Select series title (TMDB alt titles + AniList)", "select_title"),
        ("Select episode ordering (group / default)", "select_episode_group"),
        ("Set episode numbering mode (per-season / continuing)", "select_episode_mode"),
        ("Configure global qBittorrent & hook defaults", "config_qbit_hook"),
        ("<-- Back to main menu", "back"),
    ]


def _utilities_menu_options() -> list[tuple[str, str]]:
    """Return the library utility options."""
    return [
        ("Undo previous renames", "undo"),
        ("Clear series cache", "clear_cache"),
        ("Scan library to SQLite database", "scan_db"),
        ("Set folder icon for current series", "set_icon"),
        ("Set folder icons for ALL series (batch)", "batch_set_icons"),
        ("Remove folder icons (batch)", "batch_remove_icons"),
        ("<-- Back to main menu", "back"),
    ]


def build_config_from_args(args: argparse.Namespace) -> Config:
    """Build Config from CLI arguments, merging with environment variables."""
    overrides = {}
    if args.title is not None:
        overrides["series_name"] = args.title
    if args.tmdb_id is not None:
        overrides["tmdb_series_id"] = args.tmdb_id
    if args.anilist_id is not None:
        overrides["anilist_id"] = args.anilist_id
    if args.kitsu_id is not None:
        overrides["kitsu_id"] = args.kitsu_id
    if args.provider is not None:
        overrides["provider"] = Provider.from_str(args.provider)
    if args.absolute:
        overrides["absolute_numbering"] = args.absolute
    if args.episode_group is not None:
        overrides["episode_group_id"] = args.episode_group

    # Use Config.from_env so it loads all keys and values from .env
    # and merges them with overrides.
    return Config.from_env(**overrides)


def run_identity_submenu(cfg: Config, renamer: AnimeRenamer) -> None:
    """Run the provider and series identity settings sub-menu."""
    while True:
        result = Picker(
            _identity_menu_options(),
            title="PROVIDER & SERIES IDENTITY",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "switch_provider":
            menus.switch_provider(cfg)
        elif action == "set_manual_info":
            menus.set_manual_info(cfg)
        elif action == "select_title":
            menus.select_series_title(cfg, renamer)
        elif action == "select_episode_group":
            menus.select_episode_group(cfg, renamer)
        elif action == "select_episode_mode":
            menus.select_episode_start_mode(cfg)
        elif action == "config_qbit_hook":
            menus.configure_qbit_hook(cfg)



def run_utilities_submenu(cfg: Config, renamer: AnimeRenamer) -> None:
    """Run the library utilities sub-menu."""
    while True:
        result = Picker(
            _utilities_menu_options(),
            title="LIBRARY UTILITIES",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "undo":
            renamer.undo()
        elif action == "clear_cache":
            SeriesCache(cfg.media_dir).clear()
            print("  Series cache cleared.")
        elif action == "scan_db":
            scan_library_to_db(cfg)
        elif action == "set_icon":
            menus.set_icon_current_folder(cfg)
        elif action == "batch_set_icons":
            menus.batch_set_icons(cfg)
        elif action == "batch_remove_icons":
            menus.batch_remove_icons(cfg)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Jellyfin Anime Renamer v5 — rename & organise anime for Jellyfin",
    )
    parser.add_argument("--title", help='Override series title (e.g. "Re:Zero")')
    parser.add_argument("--tmdb-id", type=int, help="Set TMDB series ID directly (skip search)")
    parser.add_argument("--anilist-id", type=int, help="Set AniList ID directly")
    parser.add_argument("--kitsu-id", type=int, help="Set Kitsu ID directly")
    parser.add_argument(
        "--provider",
        choices=["tmdb", "anilist", "kitsu"],
        help="Metadata provider (default: tmdb)",
    )
    parser.add_argument(
        "--absolute",
        action="store_true",
        help="Use absolute episode numbering",
    )
    parser.add_argument(
        "--episode-group",
        help="TMDB episode group ID for custom season/episode ordering",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no files changed)",
    )
    parser.add_argument(
        "--set-icon",
        action="store_true",
        help="Download and set folder icon for the current series",
    )
    parser.add_argument(
        "--batch-icons",
        action="store_true",
        help="Set folder icons for ALL series subfolders under MEDIA_DIR",
    )
    args = parser.parse_args()

    cfg = build_config_from_args(args)
    renamer = AnimeRenamer(cfg)

    # Non-interactive mode
    if args.dry_run:
        print(BANNER)
        renamer.run(dry_run=True)
        return

    # Non-interactive: set folder icon for current series
    if args.set_icon:
        print(BANNER)
        menus.set_icon_current_folder(cfg)
        return

    # Non-interactive: batch-set icons for all subfolders
    if args.batch_icons:
        print(BANNER)
        menus.batch_set_icons(cfg)
        return

    print(BANNER)

    while True:
        result = Picker(
            _main_menu_options(),
            title="JELLYFIN ANIME RENAMER",
            default_index=0,
        ).run()

        if result is None or result[1] == "exit":
            print("\n  Smooth sailing!\n")
            sys.exit(0)

        choice = result[1]

        if choice == "dry_run":
            menus.auto_prompt_episode_groups(cfg, renamer)
            renamer.run(dry_run=True)
        elif choice == "live_run":
            print("\n  WARNING: This will rename and move files on disk.")
            if input("  Continue? (y/N): ").strip().lower() == "y":
                menus.auto_prompt_episode_groups(cfg, renamer)
                renamer.run(dry_run=False)
            else:
                print("  Cancelled.")
        elif choice == "multi_series":
            run_multi_series_menu(cfg)
        elif choice == "submenu_identity":
            run_identity_submenu(cfg, renamer)
        elif choice == "submenu_utilities":
            run_utilities_submenu(cfg, renamer)
        elif choice == "show_config":
            menus.show_config(cfg)
