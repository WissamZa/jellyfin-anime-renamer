"""
renamer.cli.main
================
The main execution loops, CLI argument parsing, and menu routing.
"""

import argparse
import os
import sys
from pathlib import Path

from renamer import __version__
from renamer.cache import SeriesCache

# CLI submodules
from renamer.cli import menus
from renamer.cli.multi_series import run_multi_series_menu, run_rename_folders_menu
from renamer.cli.scanner import scan_library_to_db
from renamer.config import Config, Provider, get_logger
from renamer.picker import Picker
from renamer.renamer import AnimeRenamer

log = get_logger(__name__)

BANNER = f"""
+======================================================+
|       JELLYFIN ANIME RENAMER  v{__version__}              |
|  TMDB + AniList + Kitsu + AniDB | Hash ID | Undo-safe  |
+======================================================+"""


def _install_completion(shell: str) -> None:
    """Install shell tab-completion for jellyfin_renamer.py."""
    import importlib.util
    if importlib.util.find_spec("argcomplete") is None:
        print("  argcomplete is not installed. Install it with: uv add argcomplete")
        return

    # Get the path to the shell completion script
    script_name = "jellyfin_renamer.py"

    if shell == "bash":
        # Register with argcomplete
        bash_completion_dir = os.path.expanduser("~/.bash_completion.d")
        os.makedirs(bash_completion_dir, exist_ok=True)
        completion_file = os.path.join(bash_completion_dir, "jellyfin_renamer")
        content = f'''# Bash completion for jellyfin_renamer.py
_eval "$(register-python-argcomplete {script_name})"
'''
        with open(completion_file, "w") as f:
            f.write(content)
        print(f"  Bash completion installed to: {completion_file}")
        print(f"  Restart your shell or run: source {completion_file}")

    elif shell == "zsh":
        zsh_completion_dir = os.path.expanduser("~/.zsh/completion")
        os.makedirs(zsh_completion_dir, exist_ok=True)
        completion_file = os.path.join(zsh_completion_dir, "_jellyfin_renamer")
        content = f'''#compdef jellyfin_renamer.py
_eval "$(register-python-argcomplete {script_name})"
'''
        with open(completion_file, "w") as f:
            f.write(content)
        print(f"  Zsh completion installed to: {completion_file}")
        print("  Add to your .zshrc:")
        print(f"    fpath+=({zsh_completion_dir})")
        print("    autoload -U compinit && compinit")

    elif shell == "fish":
        fish_completion_dir = os.path.expanduser("~/.config/fish/completions")
        os.makedirs(fish_completion_dir, exist_ok=True)
        completion_file = os.path.join(fish_completion_dir, "jellyfin_renamer.fish")
        content = f'''# Fish completion for jellyfin_renamer.py
_eval (register-python-argcomplete {script_name})
'''
        with open(completion_file, "w") as f:
            f.write(content)
        print(f"  Fish completion installed to: {completion_file}")
        print("  Restart your shell to activate.")

    print(f"\n  After installation, type: uv run {script_name} --<TAB> to see options!")


def _main_menu_options() -> list[tuple[str, str]]:
    """Return the main menu options."""
    return [
        ("[Run] Dry Run  (preview only)", "dry_run"),
        ("[Run] Live Rename + Organise", "live_run"),
        ("[Subtitles] Rename subtitles to match videos  -->", "rename_subtitles"),
        ("[Organize] Hash-scan & organize mixed anime files  -->", "hash_organize"),
        ("[Scan] Scan folder — pick & rename multiple series  -->", "multi_series"),
        ("[Folders] Rename folder names using TMDB  -->", "rename_folders"),
        ("[Navigate] Change to a subfolder  -->", "navigate"),
        ("[Backup] Anime Database  -->", "submenu_backup"),
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
        ("Set episode title language (Romaji / English)", "select_title_lang"),
        ("Configure season arc names (e.g. East Blue (1-61))  -->", "configure_arc_names"),
        ("Select folder & edit its series ID  -->", "edit_series_id"),
        ("Configure global qBittorrent & hook defaults", "config_qbit_hook"),
        ("<-- Back to main menu", "back"),
    ]


def _utilities_menu_options() -> list[tuple[str, str]]:
    """Return the library utility options."""
    return [
        ("Undo previous renames", "undo"),
        ("Clear series cache", "clear_cache"),
        ("Clear AniDB hash cache", "clear_anidb_cache"),
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
    if args.title_lang is not None:
        overrides["episode_title_lang"] = args.title_lang
    if args.use_hash:
        overrides["use_hash"] = True
    if args.recursive:
        overrides["scan_recursive"] = True
    if args.offline:
        overrides["anidb_offline"] = True
    if args.media_dir is not None:
        overrides["media_dir"] = Path(args.media_dir)

    # Use Config.from_env so it loads all keys and values from .env
    # and merges them with overrides.
    return Config.from_env(**overrides)


def _resolve_media_dir(cfg: Config, args: argparse.Namespace | None = None) -> Path:
    """
    Resolve the media directory to use for operations.

    Priority:
      1. --media-dir CLI argument
      2. --current-dir flag (use CWD with confirmation)
      3. MEDIA_DIR from .env / config
      4. Fallback to current working directory
    """
    if args and hasattr(args, "media_dir") and args.media_dir:
        return Path(args.media_dir)

    if args and hasattr(args, "current_dir") and args.current_dir:
        cwd = Path.cwd()
        print("\n  Using current directory as MEDIA_DIR:")
        print(f"    {cwd}")
        confirm = input("  Confirm? (Y/n): ").strip().lower()
        if confirm in ("n", "no"):
            print("  Cancelled.")
            return cfg.media_dir
        cfg.media_dir = cwd
        return cwd

    return cfg.media_dir


def run_hash_organize_menu(cfg: Config) -> None:
    """
    Interactive hash-organize flow.

    Lets the user choose which directory to scan, then runs the
    hash-based multi-series organizer.
    """
    from renamer.hash_organizer import HashOrganizer

    # Ask which directory to scan
    options = [
        (f"Use current MEDIA_DIR: {cfg.media_dir}", "media_dir"),
        ("Use current working directory (where you ran the command)", "cwd"),
        ("Use BASE_DOWNLOAD_PATH" + (f": {cfg.base_download_path}" if cfg.base_download_path else " (not set)"), "base_dl"),
        ("Enter a custom path", "custom"),
        ("<-- Back to main menu", "back"),
    ]

    result = Picker(
        options,
        title="HASH-ORGANIZE: SELECT DIRECTORY",
        default_index=0,
    ).run()

    if result is None or result[1] == "back":
        return

    choice = result[1]
    target_dir: Path | None = None

    if choice == "media_dir":
        target_dir = cfg.media_dir
    elif choice == "cwd":
        target_dir = Path.cwd()
        print(f"\n  Current directory: {target_dir}")
        if input("  Confirm this directory? (Y/n): ").strip().lower() in ("n", "no"):
            print("  Cancelled.")
            return
    elif choice == "base_dl":
        if cfg.base_download_path and cfg.base_download_path.is_dir():
            target_dir = cfg.base_download_path
        else:
            print("  BASE_DOWNLOAD_PATH is not set or doesn't exist.")
            print("  Set it in your .env file or use the qBit hook config menu.")
            return
    elif choice == "custom":
        custom = input("  Enter directory path: ").strip()
        if not custom:
            print("  Cancelled.")
            return
        target_dir = Path(custom).resolve()
        if not target_dir.is_dir():
            print(f"  Directory does not exist: {target_dir}")
            return

    if target_dir is None:
        return

    # Show what we're about to scan
    video_exts = set(cfg.video_extensions)
    video_count = sum(
        1 for f in target_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in video_exts
    )

    print(f"\n  Target directory: {target_dir}")
    print(f"  Video files found: {video_count}")

    if video_count == 0:
        print("  No video files found. Nothing to organize.")
        return

    # Confirm
    if input("  Proceed with hash scanning? (Y/n): ").strip().lower() in ("n", "no"):
        print("  Cancelled.")
        return

    # Check AniDB credentials
    if not cfg.anidb_username or not cfg.anidb_password:
        print("\n  NOTE: AniDB credentials not configured.")
        print("  Hash-organize will use filename-based identification (no API lookups).")
        print("  Set ANIDB_USERNAME and ANIDB_PASSWORD in .env for hash-based identification.")
    else:
        print("\n  AniDB credentials found. Will try hash identification first,")
        print("  then fall back to filename parsing for unidentified files.")

    # Run the organizer
    organizer = HashOrganizer(cfg, media_dir=target_dir)

    # Phase 1: Scan and identify
    print(f"\n  Scanning and hashing files in: {target_dir}")
    print("  This may take a while for large collections …\n")

    organize_result = organizer.scan_and_identify()

    if organize_result.total_files == 0:
        return

    # Phase 2: Preview
    organizer.preview()

    # Phase 3: Ask for execution mode
    mode_result = Picker(
        [
            ("Dry Run  (preview only — no files moved)", "dry"),
            ("Live Organize  (move files into series folders)", "live"),
            ("Back to main menu", "back"),
        ],
        title="HASH-ORGANIZE: EXECUTION MODE",
        default_index=0,
    ).run()

    if mode_result is None or mode_result[1] == "back":
        return

    dry_run = mode_result[1] == "dry"

    if not dry_run:
        print(f"\n  WARNING: This will move {organize_result.identified} identified file(s)")
        print(f"  into series folders under: {target_dir}")
        print(f"  Unidentified files will go to: {target_dir / '_unidentified'}")
        if input("  Continue? (y/N): ").strip().lower() != "y":
            print("  Cancelled.")
            return

    # Phase 4: Execute
    organizer.execute(dry_run=dry_run)


def run_subtitle_rename_menu(cfg: Config) -> None:
    """
    Interactive subtitle rename flow.

    Matches subtitle files to video files by season/episode number (S01E01)
    and renames the subtitles to match the video filenames.

    This is useful when videos have already been renamed with full episode
    titles but subtitles still only have the series name and episode number.

    Example::

        Video:    'Runway de Waratte - S01E01 - Koreha Kun no Monogatari.mkv'
        Subtitle: 'Runway de Waratte - S01E01.ass'
        Result:   'Runway de Waratte - S01E01 - Koreha Kun no Monogatari.ass'
    """
    from renamer.subtitle_matcher import (
        execute_subtitle_renames,
        preview_subtitle_renames,
        scan_subtitle_matches,
    )

    # Ask which directory to scan
    options = [
        (f"Use current MEDIA_DIR: {cfg.media_dir}", "media_dir"),
        ("Use current working directory (where you ran the command)", "cwd"),
        ("Enter a custom path", "custom"),
        ("<-- Back to main menu", "back"),
    ]

    result = Picker(
        options,
        title="SUBTITLE RENAME: SELECT DIRECTORY",
        default_index=0,
    ).run()

    if result is None or result[1] == "back":
        return

    choice = result[1]
    target_dir: Path | None = None

    if choice == "media_dir":
        target_dir = cfg.media_dir
    elif choice == "cwd":
        target_dir = Path.cwd()
        print(f"\n  Current directory: {target_dir}")
        if input("  Confirm this directory? (Y/n): ").strip().lower() in ("n", "no"):
            print("  Cancelled.")
            return
    elif choice == "custom":
        custom = input("  Enter directory path: ").strip()
        if not custom:
            print("  Cancelled.")
            return
        target_dir = Path(custom).resolve()
        if not target_dir.is_dir():
            print(f"  Directory does not exist: {target_dir}")
            return

    if target_dir is None:
        return

    # Ask whether to scan recursively
    scan_recursive = False
    sub_count_shallow = sum(
        1 for f in target_dir.iterdir()
        if f.is_file() and f.suffix.lower() in cfg.subtitle_extensions
    )
    sub_count_deep = sum(
        1 for f in target_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in cfg.subtitle_extensions
    )

    if sub_count_deep > sub_count_shallow:
        print(f"\n  Subtitles in current folder: {sub_count_shallow}")
        print(f"  Subtitles including subfolders: {sub_count_deep}")
        rec_choice = Picker(
            [
                ("Scan current folder only", False),
                (f"Scan recursively (all subfolders) — {sub_count_deep} subtitle(s)", True),
            ],
            title="SCAN DEPTH",
            default_index=0,
        ).run()
        if rec_choice:
            scan_recursive = rec_choice[1]

    # Scan for matches
    print(f"\n  Scanning: {target_dir}")
    scan_result = scan_subtitle_matches(
        media_dir=target_dir,
        video_extensions=cfg.video_extensions,
        subtitle_extensions=cfg.subtitle_extensions,
        scan_recursive=scan_recursive,
    )

    # Preview
    preview_subtitle_renames(scan_result)

    if scan_result.needs_rename_count == 0:
        if scan_result.already_correct_count > 0:
            print("\n  All subtitles are already correctly named. Nothing to do.")
        else:
            print("\n  No matching subtitles found. Nothing to do.")
        return

    # Ask for execution mode
    mode_result = Picker(
        [
            ("Dry Run  (preview only — no files renamed)", "dry"),
            ("Live Rename  (rename subtitle files to match videos)", "live"),
            ("Back to main menu", "back"),
        ],
        title="SUBTITLE RENAME: EXECUTION MODE",
        default_index=0,
    ).run()

    if mode_result is None or mode_result[1] == "back":
        return

    dry_run = mode_result[1] == "dry"

    if not dry_run:
        print(f"\n  WARNING: This will rename {scan_result.needs_rename_count} subtitle file(s).")
        if input("  Continue? (y/N): ").strip().lower() != "y":
            print("  Cancelled.")
            return

    # Execute
    renamed = execute_subtitle_renames(scan_result, dry_run=dry_run)

    mode_label = "DRY RUN" if dry_run else "LIVE"
    print(f"\n  {mode_label}: {len(renamed)} subtitle(s) processed.")

    if not dry_run and renamed:
        print("  Subtitles renamed successfully.")


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
        elif action == "select_title_lang":
            menus.select_episode_title_lang(cfg)
        elif action == "configure_arc_names":
            menus.configure_season_arc_names(cfg, renamer)
        elif action == "edit_series_id":
            menus.edit_series_id_for_folder(cfg, renamer)
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
        elif action == "clear_anidb_cache":
            from renamer.providers.anidb_cache import AniDBCache
            AniDBCache().clear_all()
            print("  AniDB hash cache cleared.")
        elif action == "scan_db":
            scan_library_to_db(cfg)
        elif action == "set_icon":
            menus.set_icon_current_folder(cfg)
        elif action == "batch_set_icons":
            menus.batch_set_icons(cfg)
        elif action == "batch_remove_icons":
            menus.batch_remove_icons(cfg)


def _goodbye_handler(signum, frame) -> None:
    """Handle Ctrl+C gracefully — print goodbye instead of traceback."""
    print("\n  Goodbye!\n")
    sys.exit(0)


def _ask_search_mode(cfg: Config, renamer_obj: AnimeRenamer) -> None:
    """
    Ask the user how to identify the series before running a rename.

    Options:
      1. Search by folder name (existing default behaviour — uses the
         cleaned folder name to search TMDB/AniList/Kitsu)
      2. Search by file names (scans video files in the folder, extracts
         series name from each filename — better when the folder name
         is wrong/unclear but file names are well-tagged)

    The user's choice affects how cfg.series_name is set before the
    renamer's auto-search runs. This is only asked interactively.
    """
    current_name = cfg.series_name or cfg.media_dir.name

    # Check if we can extract a name from the files
    from renamer.hash_organizer import extract_series_from_filename
    video_exts = set(cfg.video_extensions)
    file_names: set[str] = set()
    try:
        for f in sorted(cfg.media_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in video_exts:
                info = extract_series_from_filename(f.name)
                if info and info.series_name:
                    file_names.add(info.series_name)
    except Exception:
        pass

    # If no file names found or they match the folder name, skip the question
    if not file_names:
        return

    # Check if file-extracted names differ from the folder name
    folder_clean = current_name.strip()
    different_names = [n for n in sorted(file_names) if n != folder_clean]

    if not different_names:
        return  # File names agree with folder name — no need to ask

    # Show the user what we found
    print(f"\n  Current series name (from folder): {folder_clean}")
    print(f"  Series name extracted from filenames: {', '.join(different_names)}")

    options = [
        (f"Use folder name: '{folder_clean}'", "folder"),
    ]
    for name in different_names[:3]:
        options.append((f"Use filename: '{name}'", f"file:{name}"))
    if len(different_names) > 1:
        options.append(("Use filename (best guess from most common name)", "file:auto"))
    options.append(("Enter a custom name", "custom"))

    result = Picker(
        options,
        title="HOW SHOULD WE IDENTIFY THIS SERIES?",
        default_index=0,
    ).run()

    if result is None:
        return  # Cancelled — keep current name

    choice = result[1]

    if choice == "folder":
        return  # Keep current folder-based name

    if choice == "file:auto":
        # Pick the most common name from file extractions
        from collections import Counter
        name_counts = Counter()
        for f in sorted(cfg.media_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in video_exts:
                info = extract_series_from_filename(f.name)
                if info and info.series_name:
                    name_counts[info.series_name] += 1
        if name_counts:
            best = name_counts.most_common(1)[0][0]
            cfg.series_name = best
            print(f"  Using filename-based name: {best}")
    elif choice.startswith("file:"):
        name = choice[5:]
        cfg.series_name = name
        print(f"  Using filename-based name: {name}")
    elif choice == "custom":
        custom = input(f"  Enter series name [{folder_clean}]: ").strip()
        if custom:
            cfg.series_name = custom
            print(f"  Using custom name: {custom}")

    # Reset provider IDs so auto-search runs with the new name
    cfg.tmdb_series_id = None
    cfg.anilist_id = None
    cfg.kitsu_id = None


def main() -> None:
    # Install Ctrl+C handler for graceful exit
    import signal
    signal.signal(signal.SIGINT, _goodbye_handler)

    # Try to enable argcomplete for shell tab-completion
    _argcomplete = None
    try:  # noqa: SIM105
        import argcomplete as _argcomplete  # type: ignore[import-untyped]
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="Jellyfin Anime Renamer v2 — rename & organise anime for Jellyfin",
    )
    parser.add_argument("--title", help='Override series title (e.g. "Re:Zero")')
    parser.add_argument("--tmdb-id", type=int, help="Set TMDB series ID directly (skip search)")
    parser.add_argument("--anilist-id", type=int, help="Set AniList ID directly")
    parser.add_argument("--kitsu-id", type=int, help="Set Kitsu ID directly")
    parser.add_argument(
        "--provider",
        choices=["tmdb", "anilist", "kitsu", "anidb"],
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
        "--title-lang",
        choices=["romaji", "english"],
        help="Episode title language: romaji (default) or english",
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
    # ── Hash-based identification ────────────────────────────
    parser.add_argument(
        "--use-hash",
        action="store_true",
        help="Enable ED2K hash lookup via AniDB for unidentified files",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Only use cached data (no API calls to AniDB)",
    )
    # ── Hash-organize mode ────────────────────────────────────
    parser.add_argument(
        "--hash-organize",
        action="store_true",
        help="Hash-scan a mixed anime folder and organize files into series folders",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually move files (used with --hash-organize; default is dry-run)",
    )
    # ── Recursive scanning ──────────────────────────────────
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan subfolders recursively for video files",
    )
    parser.add_argument(
        "--media-dir",
        help="Override MEDIA_DIR path for this run",
    )
    parser.add_argument(
        "--current-dir",
        action="store_true",
        help="Use the current working directory as MEDIA_DIR (with confirmation)",
    )
    # ── Shell completion ─────────────────────────────────────
    parser.add_argument(
        "--install-completion",
        choices=["bash", "zsh", "fish"],
        help="Install shell tab-completion for the specified shell",
    )

    if _argcomplete is not None:
        _argcomplete.autocomplete(parser)

    args = parser.parse_args()

    # Handle --install-completion separately
    if args.install_completion:
        _install_completion(args.install_completion)
        return

    cfg = build_config_from_args(args)

    # Resolve media directory (handles --current-dir)
    if args.current_dir:
        cwd = Path.cwd()
        print("\n  --current-dir: Using current directory as MEDIA_DIR:")
        print(f"    {cwd}")
        confirm = input("  Confirm? (Y/n): ").strip().lower()
        if confirm not in ("n", "no"):
            cfg.media_dir = cwd

    renamer_obj = AnimeRenamer(cfg)

    # Non-interactive: hash-organize mode
    if args.hash_organize:
        print(BANNER)
        from renamer.hash_organizer import HashOrganizer

        # Use --media-dir if specified, else current MEDIA_DIR
        target = cfg.media_dir
        organizer = HashOrganizer(cfg, media_dir=target)

        print(f"\n  Hash-organize mode: scanning {target}")
        print(f"  {'LIVE mode' if args.live else 'DRY RUN mode'}\n")

        organize_result = organizer.scan_and_identify()
        if organize_result.total_files == 0:
            return

        organizer.preview()
        organizer.execute(dry_run=not args.live)
        return

    # Non-interactive mode
    if args.dry_run:
        print(BANNER)
        renamer_obj.run(dry_run=True)
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
            _ask_search_mode(cfg, renamer_obj)
            menus.auto_prompt_episode_groups(cfg, renamer_obj)
            renamer_obj.run(dry_run=True)
        elif choice == "live_run":
            _ask_search_mode(cfg, renamer_obj)
            print("\n  WARNING: This will rename and move files on disk.")
            if input("  Continue? (y/N): ").strip().lower() == "y":
                menus.auto_prompt_episode_groups(cfg, renamer_obj)
                renamer_obj.run(dry_run=False)
            else:
                print("  Cancelled.")
        elif choice == "rename_subtitles":
            run_subtitle_rename_menu(cfg)
        elif choice == "hash_organize":
            run_hash_organize_menu(cfg)
        elif choice == "multi_series":
            run_multi_series_menu(cfg)
        elif choice == "rename_folders":
            run_rename_folders_menu(cfg)
        elif choice == "navigate":
            menus.navigate_to_folder(cfg, renamer_obj)
        elif choice == "submenu_backup":
            menus.run_backup_submenu(cfg)
        elif choice == "submenu_identity":
            run_identity_submenu(cfg, renamer_obj)
        elif choice == "submenu_utilities":
            run_utilities_submenu(cfg, renamer_obj)
        elif choice == "show_config":
            menus.show_config(cfg)
