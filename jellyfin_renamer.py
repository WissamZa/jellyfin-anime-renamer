#!/usr/bin/env -S uv run
"""
jellyfin_renamer.py — Interactive CLI for Jellyfin Anime Renamer v5.

Features:
  - Multi-provider support (TMDB, AniList, Kitsu)
  - TMDB Episode Groups (custom season/episode ordering)
  - FZF-like interactive picker (arrow keys + number input)
  - Auto-search for series IDs
  - Auto-detect episode groups
  - Absolute episode numbering
  - Smart romaji title resolution
  - Season folder organisation
  - Undo-safe renames
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from renamer import __version__
from renamer.config import Config, Provider, get_logger
from renamer.renamer import AnimeRenamer
from renamer.cache import SeriesCache
from renamer.picker import Picker
from renamer.providers.base import EpisodeGroupInfo
from renamer.providers.registry import get_registry

log = get_logger()

BANNER = f"""
+======================================================+
|       JELLYFIN ANIME RENAMER  v{__version__}              |
|  TMDB + AniList + Kitsu | Episode Groups | Undo-safe  |
+======================================================+"""


def _menu_options() -> list[tuple[str, str]]:
    """Return the main menu options as (label, value) tuples."""
    return [
        ("Dry Run  (preview only)", "1"),
        ("Live Rename + Organise", "2"),
        ("Undo previous renames", "3"),
        ("Show config", "4"),
        ("Switch provider", "5"),
        ("Clear series cache", "6"),
        ("Set title / provider ID manually", "7"),
        ("Select episode ordering (group / default)", "8"),
        ("Exit", "9"),
    ]


def show_config(cfg: Config) -> None:
    org = "Yes" if cfg.ORGANIZE_INTO_FOLDERS else "No"
    abs_num = "Yes" if cfg.ABSOLUTE_NUMBERING else "No"
    eg = cfg.EPISODE_GROUP_ID or "(none)"
    print(f"""
  Series          : {cfg.SERIES_NAME}
  Provider        : {cfg.provider.value}
  TMDB ID         : {cfg.TMDB_SERIES_ID}
  AniList ID      : {cfg.ANILIST_ID}
  Kitsu ID        : {cfg.KITSU_ID}
  Episode Group   : {eg}
  Media dir       : {cfg.MEDIA_DIR}
  Template        : {cfg.NAME_TEMPLATE}
  Special template: {cfg.SPECIAL_TEMPLATE}
  Organise folders: {org}
  Absolute nums   : {abs_num}
  Season folder   : {cfg.SEASON_FOLDER_TEMPLATE.format(season=1)}  (example)
  Specials folder : {cfg.SPECIALS_FOLDER_NAME}
  Extensions      : {', '.join(cfg.VIDEO_EXTENSIONS)}
  History         : {cfg.history_file}
  Log file        : renamer.log""")


def switch_provider(cfg: Config) -> None:
    """Interactive provider switching."""
    providers = [
        (f"TMDB  {'(current)' if cfg.provider == Provider.TMDB else ''}", "tmdb"),
        (f"AniList  {'(current)' if cfg.provider == Provider.AniList else ''}", "anilist"),
        (f"Kitsu  {'(current)' if cfg.provider == Provider.Kitsu else ''}", "kitsu"),
    ]
    result = Picker(
        providers,
        title="Switch provider",
        default_index=[Provider.TMDB, Provider.AniList, Provider.Kitsu].index(cfg.provider),
    ).run()
    if result:
        cfg.provider = Provider.from_str(result[1])
        print(f"  Switched to: {cfg.provider.value}")


def set_manual_info(cfg: Config) -> None:
    """Interactive title/ID entry."""
    print(f"\n  Current title: {cfg.SERIES_NAME}")
    new_title = input("  New title (Enter to keep): ").strip()
    if new_title:
        cfg.SERIES_NAME = new_title
        print(f"  Title set to: {cfg.SERIES_NAME}")

    print(f"\n  Current TMDB ID: {cfg.TMDB_SERIES_ID}")
    new_tmdb = input("  New TMDB ID (Enter to keep): ").strip()
    if new_tmdb:
        try:
            cfg.TMDB_SERIES_ID = int(new_tmdb)
            print(f"  TMDB ID set to: {cfg.TMDB_SERIES_ID}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current AniList ID: {cfg.ANILIST_ID}")
    new_al = input("  New AniList ID (Enter to keep): ").strip()
    if new_al:
        try:
            cfg.ANILIST_ID = int(new_al)
            print(f"  AniList ID set to: {cfg.ANILIST_ID}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Kitsu ID: {cfg.KITSU_ID}")
    new_kitsu = input("  New Kitsu ID (Enter to keep): ").strip()
    if new_kitsu:
        try:
            cfg.KITSU_ID = int(new_kitsu)
            print(f"  Kitsu ID set to: {cfg.KITSU_ID}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Episode Group: {cfg.EPISODE_GROUP_ID or '(none)'}")
    new_eg = input("  New Episode Group ID (Enter to keep, '0' to clear): ").strip()
    if new_eg == "0":
        cfg.EPISODE_GROUP_ID = None
        print("  Episode Group cleared.")
    elif new_eg:
        cfg.EPISODE_GROUP_ID = new_eg
        print(f"  Episode Group set to: {cfg.EPISODE_GROUP_ID}")


def select_episode_group(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Interactive TMDB Episode Group selection using the Picker.

    Always shows "Default TMDB Ordering" as the first option so the
    user can switch back from a previously selected group.
    When an "Absolute Order" group is selected, absolute numbering
    is auto-enabled.
    """
    if cfg.provider != Provider.TMDB:
        print("\n  Episode groups are only available with the TMDB provider.")
        print(f"  Current provider: {cfg.provider.value}")
        return

    if not cfg.TMDB_SERIES_ID:
        print("\n  TMDB series ID is not set. Use option 7 to set it, or")
        print("  run a dry-run first (auto-search will find it).")
        return

    current_eg = cfg.EPISODE_GROUP_ID
    if current_eg:
        print(f"\n  Current episode group: {current_eg}")
    else:
        print("\n  Current: Default TMDB Ordering")

    print("  Fetching available episode groups from TMDB …")
    groups = renamer.list_episode_groups()

    # Build picker options — always include "Default TMDB Ordering"
    default_label = "Default TMDB Ordering"
    if not current_eg:
        default_label += "  (current)"
    options = [(default_label, "default")]

    if not groups:
        print("  No episode groups found for this series on TMDB.")
        # Still allow picking Default
        result = Picker(
            options,
            title="Select Episode Ordering",
            default_index=0,
        ).run()
        if result is None:
            print("  Cancelled.")
            return
        _, selected = result
        if selected == "default":
            cfg.EPISODE_GROUP_ID = None
            cfg.ABSOLUTE_NUMBERING = False
            print("  Using default TMDB ordering.")
        return

    for g in groups:
        desc = f" — {g.description}" if g.description else ""
        marker = "  (current)" if current_eg and str(g.id) == str(current_eg) else ""
        label = f"{g.name}  ({g.type_label}, {g.episode_count} eps, {g.group_count} seasons){desc}{marker}"
        options.append((label, g))

    # Pre-select current group or default
    default_idx = 0
    if current_eg:
        for i, (_, val) in enumerate(options):
            if isinstance(val, EpisodeGroupInfo) and str(val.id) == str(current_eg):
                default_idx = i
                break

    result = Picker(
        options,
        title="Select Episode Ordering",
        default_index=default_idx,
    ).run()

    if result is None:
        print("  Cancelled.")
        return

    _, selected = result
    if selected == "default":
        cfg.EPISODE_GROUP_ID = None
        cfg.ABSOLUTE_NUMBERING = False
        print("  Switched to default TMDB ordering.")
        return

    cfg.EPISODE_GROUP_ID = selected.id

    # Auto-enable absolute numbering for Absolute Order groups
    if selected.type == 2:  # Absolute Order
        cfg.ABSOLUTE_NUMBERING = True
        print(
            f"\n  Selected: {selected.name} ({selected.type_label})\n"
            f"  Auto-enabled absolute numbering.\n"
            f"  Format: Series - S{{season}}E{{absolute}} - Title.ext"
        )
    else:
        print(
            f"\n  Selected: {selected.name} ({selected.type_label})\n"
            f"  Episode Group ID: {selected.id}"
        )


def auto_prompt_episode_groups(
    cfg: Config, renamer: AnimeRenamer,
) -> None:
    """
    Called before running the renamer.  If using TMDB with a series
    ID, auto-fetch available episode groups and present the picker.

    Always shows "Default TMDB Ordering" as the first option.
    If an episode group is already selected, pre-selects it so the
    user can confirm or switch.
    """
    if cfg.provider != Provider.TMDB:
        return
    if not cfg.TMDB_SERIES_ID:
        return  # No series to look up

    current_eg = cfg.EPISODE_GROUP_ID

    groups = renamer.list_episode_groups()
    if not groups and not current_eg:
        return  # No groups available and no group set — silently skip
    if not groups:
        # No groups found but user has a group set — just use it
        return

    # Build picker options — always include "Default TMDB Ordering"
    default_label = "Default TMDB Ordering"
    if not current_eg:
        default_label += "  (current)"
    options = [(default_label, "default")]

    for g in groups:
        marker = "  (current)" if current_eg and str(g.id) == str(current_eg) else ""
        label = f"{g.name}  ({g.type_label}, {g.episode_count} eps){marker}"
        options.append((label, g))

    # Pre-select current group or default
    default_idx = 0
    if current_eg:
        for i, (_, val) in enumerate(options):
            if isinstance(val, EpisodeGroupInfo) and str(val.id) == str(current_eg):
                default_idx = i
                break

    print(f"\n  TMDB has {len(groups)} episode group(s) available.")
    result = Picker(
        options,
        title="Select Episode Ordering",
        default_index=default_idx,
    ).run()

    if result is None:
        return  # Cancelled — keep current setting

    _, selected = result
    if selected == "default":
        cfg.EPISODE_GROUP_ID = None
        cfg.ABSOLUTE_NUMBERING = False
        print("  Using default TMDB ordering.")
        return

    cfg.EPISODE_GROUP_ID = selected.id
    if selected.type == 2:
        cfg.ABSOLUTE_NUMBERING = True
        print(
            f"  Using: {selected.name} (absolute numbering auto-enabled)"
        )
    else:
        print(f"  Using: {selected.name}")


def build_config_from_args(args: argparse.Namespace) -> Config:
    """Build Config from CLI arguments, merging with env vars."""
    return Config(
        series_name=args.title or None,
        tmdb_series_id=args.tmdb_id or None,
        anilist_id=args.anilist_id or None,
        kitsu_id=args.kitsu_id or None,
        provider=Provider.from_str(args.provider) if args.provider else None,
        absolute_numbering=args.absolute or None,
        episode_group_id=args.episode_group or None,
    )


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
    args = parser.parse_args()

    cfg = build_config_from_args(args)
    renamer = AnimeRenamer(cfg)

    # Non-interactive mode
    if args.dry_run:
        print(BANNER)
        renamer.run(dry_run=True)
        return

    print(BANNER)

    while True:
        result = Picker(
            _menu_options(),
            title="JELLYFIN ANIME RENAMER",
            default_index=0,
        ).run()

        if result is None:
            print("\n  Smooth sailing!\n")
            sys.exit(0)

        choice = result[1]

        if choice == "1":
            auto_prompt_episode_groups(cfg, renamer)
            renamer.run(dry_run=True)
        elif choice == "2":
            print("\n  WARNING: This will rename and move files on disk.")
            if input("  Continue? (y/N): ").strip().lower() == "y":
                auto_prompt_episode_groups(cfg, renamer)
                renamer.run(dry_run=False)
            else:
                print("  Cancelled.")
        elif choice == "3":
            renamer.undo()
        elif choice == "4":
            show_config(cfg)
        elif choice == "5":
            switch_provider(cfg)
        elif choice == "6":
            SeriesCache(cfg.MEDIA_DIR).clear()
            print("  Series cache cleared.")
        elif choice == "7":
            set_manual_info(cfg)
        elif choice == "8":
            select_episode_group(cfg, renamer)
        elif choice == "9":
            print("\n  Smooth sailing!\n")
            sys.exit(0)


if __name__ == "__main__":
    main()
