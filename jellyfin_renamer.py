#!/usr/bin/env -S uv run
"""
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️  JELLYFIN ANIME RENAMER  🏴‍☠️              ║
║    TMDB + AniList  |  Season folders  |  Undo-safe   ║
╚══════════════════════════════════════════════════════╝

Interactive CLI for manual runs.
Automated use: see qbit_hook.py

Usage (from ANY directory)
--------------------------
  # Use MEDIA_DIR from .env
  uv run --project ~/renamer jellyfin_renamer.py

  # Scan a specific folder (overrides MEDIA_DIR)
  uv run --project ~/renamer jellyfin_renamer.py --path /mnt/D/Torrent/Naruto

  # Shorthand: pass current directory
  uv run --project ~/renamer jellyfin_renamer.py --path .

  # Non-interactive dry run
  uv run --project ~/renamer jellyfin_renamer.py --path . --dry-run

  # Non-interactive live rename
  uv run --project ~/renamer jellyfin_renamer.py --path . --run

  # Use AniList as the provider (no TMDB API key needed)
  uv run --project ~/renamer jellyfin_renamer.py --path . --provider anilist

  # Inside the project folder — uv finds pyproject.toml automatically
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto
"""

import argparse
import os
import sys
from pathlib import Path

# ── Bootstrap: anchor everything to the script's own directory ──────────────
# Works regardless of which directory the user is in when they run the script,
# whether uv found the right pyproject.toml, or how the script was invoked.
#
#   1. Add the script directory to sys.path  → `import renamer_core` works
#   2. Set UV_PROJECT                        → uv finds the right venv
#   3. Load .env early                       → Config() works immediately

_SCRIPT_DIR = Path(__file__).resolve().parent

# 1 — sys.path
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# 2 — Tell uv where the project lives (no-op if already correct)
os.environ.setdefault("UV_PROJECT", str(_SCRIPT_DIR))

# 3 — Load .env early so Config() has values before renamer_core loads it
_env_file = _SCRIPT_DIR / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file, override=False)
    except ImportError:
        pass  # dotenv not yet available; renamer_core will load it

from renamer_core import AnimeRenamer, Config, Provider, SeriesCache, get_logger  # noqa: E402

log = get_logger()

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
  │  5. Switch provider                  │
  │  6. Clear series cache (re-search)   │
  │  7. Exit                             │
  └──────────────────────────────────────┘"""


def build_config(path: Path | None, provider: Provider | None = None) -> Config:
    """
    Build a Config, optionally overriding MEDIA_DIR with a CLI path.
    When a path is provided, the series name is derived from the folder
    name unless SERIES_NAME is explicitly set in .env.
    """
    if path:
        path = path.resolve()
        if not path.exists():
            print(f"\n  ❌  Path does not exist: {path}\n")
            sys.exit(1)
        if not path.is_dir():
            print(f"\n  ❌  Path is not a directory: {path}\n")
            sys.exit(1)
        # Pass media_dir — Config will derive SERIES_NAME from the folder name
        # if SERIES_NAME is not set in .env
        return Config(media_dir=path, provider=provider)
    return Config(provider=provider)


def show_config(cfg: Config) -> None:
    org = "Yes" if cfg.ORGANIZE_INTO_FOLDERS else "No"
    cache = SeriesCache(cfg.MEDIA_DIR)
    cache_status = cache.path.name if cache.path.exists() else "(none — will search on run)"

    # Show provider-specific info
    provider_label = cfg.PROVIDER.value.upper()
    if cfg.PROVIDER == Provider.TMDB:
        provider_desc = f"TMDB (id={cfg.TMDB_SERIES_ID})" if cfg.TMDB_SERIES_ID else "TMDB (auto-resolve)"
        anilist_label = str(cfg.ANILIST_ID) if cfg.ANILIST_ID else "(auto-lookup)"
    else:
        provider_desc = f"AniList (id={cfg.ANILIST_ID})" if cfg.ANILIST_ID else "AniList (auto-resolve)"
        anilist_label = str(cfg.ANILIST_ID) if cfg.ANILIST_ID else "(primary)"
        if cfg.TMDB_SERIES_ID:
            anilist_label += f"  [TMDB cross-ref: {cfg.TMDB_SERIES_ID}]"

    print(f"""
  Series          : {cfg.SERIES_NAME}
  Provider        : {provider_label}
  Provider ID     : {provider_desc}
  TMDB ID         : {cfg.TMDB_SERIES_ID if cfg.TMDB_SERIES_ID is not None else "(auto-resolve via search)"}
  AniList ID      : {anilist_label}
  Media dir       : {cfg.MEDIA_DIR}
  Template        : {cfg.NAME_TEMPLATE}
  Special template: {cfg.SPECIAL_TEMPLATE}
  Organise folders: {org}
  Season folder   : {cfg.SEASON_FOLDER_TEMPLATE.format(season=1)}  (example)
  Specials folder : {cfg.SPECIALS_FOLDER_NAME}
  Extensions      : {", ".join(cfg.VIDEO_EXTENSIONS)}
  History         : {cfg.history_file}
  Series cache    : {cache_status}
  Log file        : {_SCRIPT_DIR / "renamer.log"}""")


def switch_provider(cfg: Config) -> None:
    """Interactive provider switching menu."""
    print(f"""
  ┌──────────────────────────────────────────────────────────┐
  │  Current provider: {cfg.PROVIDER.value.upper():<41}│
  ├──────────────────────────────────────────────────────────┤
  │                                                          │
  │  1. TMDB    — The Movie Database                         │
  │               Requires API key, best season/episode data  │
  │                                                          │
  │  2. AniList — Free GraphQL API                            │
  │               No API key needed, human-curated romaji     │
  │                                                          │
  └──────────────────────────────────────────────────────────┘""")

    choice = input("  Select provider (1-2, Enter to cancel): ").strip()

    if choice == "1":
        if cfg.PROVIDER == Provider.TMDB:
            print("  ℹ️  Already using TMDB.")
            return
        cfg.PROVIDER = Provider.TMDB
        print("  ✅  Switched to TMDB provider.")
        # Clear cache so next run re-searches with the new provider
        cache = SeriesCache(cfg.MEDIA_DIR)
        if cache.path.exists():
            cache.clear()
            print("  🗑️  Series cache cleared (will re-search on next run).")

    elif choice == "2":
        if cfg.PROVIDER == Provider.AniList:
            print("  ℹ️  Already using AniList.")
            return
        cfg.PROVIDER = Provider.AniList
        print("  ✅  Switched to AniList provider (no API key required!).")
        # Clear cache so next run re-searches with the new provider
        cache = SeriesCache(cfg.MEDIA_DIR)
        if cache.path.exists():
            cache.clear()
            print("  🗑️  Series cache cleared (will re-search on next run).")

    else:
        print("  Cancelled.")


def interactive_menu(cfg: Config) -> None:
    renamer = AnimeRenamer(cfg)
    print(BANNER)

    # Show which folder and provider we're working with
    print(f"\n  📂  Working folder: {cfg.MEDIA_DIR}")
    print(f"  🔌  Provider: {cfg.PROVIDER.value.upper()}")

    while True:
        print(MENU)
        choice = input("  Option (1-7): ").strip()

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
            show_config(cfg)

        elif choice == "5":
            switch_provider(cfg)
            # Update the menu header info
            print(f"\n  🔌  Provider: {cfg.PROVIDER.value.upper()}")

        elif choice == "6":
            cache = SeriesCache(cfg.MEDIA_DIR)
            if cache.path.exists():
                cache.clear()
                print("  🗑️  Series cache cleared. Next run will re-search.")
            else:
                print("  ℹ️  No series cache found for this folder.")

        elif choice == "7":
            print("\n  Smooth sailing! 🌊\n")
            sys.exit(0)

        else:
            print("  ❌  Invalid choice — enter 1 through 7.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jellyfin_renamer",
        description="Rename anime episodes to Jellyfin SxxExx format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Interactive menu, folder from .env
  uv run jellyfin_renamer.py

  # Interactive menu, specific folder
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto

  # Non-interactive dry run (great for scripting / cron preview)
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --dry-run

  # Non-interactive live rename
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --run

  # Use AniList as the metadata provider (no TMDB API key needed)
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --provider anilist

providers:
  tmdb     — The Movie Database (default, requires TMDB_API_KEY)
  anilist  — AniList GraphQL API (free, no key needed)
""",
    )
    parser.add_argument(
        "--path",
        "-p",
        type=Path,
        default=None,
        metavar="DIR",
        help="folder to scan (overrides MEDIA_DIR in .env)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["tmdb", "anilist"],
        metavar="PROVIDER",
        help="metadata provider: tmdb (default) or anilist (free, no API key)",
    )
    # Mutually exclusive non-interactive modes
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        help="preview renames without touching files, then exit",
    )
    mode.add_argument(
        "--run",
        "-r",
        action="store_true",
        help="rename and organise files immediately, then exit (no menu)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve provider from CLI arg
    provider = None
    if args.provider:
        provider = Provider.from_str(args.provider)

    cfg = build_config(args.path, provider=provider)

    # ── Non-interactive modes (--dry-run / --run) ─────────
    if args.dry_run:
        print(BANNER)
        print(f"\n  📂  Working folder: {cfg.MEDIA_DIR}")
        print(f"  🔌  Provider: {cfg.PROVIDER.value.upper()}")
        print("  ⚠️  DRY RUN — no files will be changed\n")
        AnimeRenamer(cfg).run(dry_run=True)
        return

    if args.run:
        print(BANNER)
        print(f"\n  📂  Working folder: {cfg.MEDIA_DIR}")
        print(f"  🔌  Provider: {cfg.PROVIDER.value.upper()}")
        print("  🚀  LIVE RUN — files will be renamed\n")
        AnimeRenamer(cfg).run(dry_run=False)
        return

    # ── Interactive menu ──────────────────────────────────
    interactive_menu(cfg)


if __name__ == "__main__":
    main()
