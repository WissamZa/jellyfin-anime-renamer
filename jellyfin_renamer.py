#!/usr/bin/env -S uv run
"""
Jellyfin Anime Renamer v4.0.0 — Interactive CLI

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

  # Use Kitsu (best for complex season splits like Re:Zero)
  uv run --project ~/renamer jellyfin_renamer.py --path . --provider kitsu

  # Override series title manually (skip auto-detection from folder name)
  uv run --project ~/renamer jellyfin_renamer.py --path . --title "Re:Zero"

  # Provide a provider ID directly (skip search entirely)
  uv run --project ~/renamer jellyfin_renamer.py --path . --provider anilist --anilist-id 113328

  # Combine title + provider ID for full manual control
  uv run --project ~/renamer jellyfin_renamer.py --path . --title "Re:Zero" --provider kitsu --kitsu-id 13093

  # Inside the project folder — uv finds pyproject.toml automatically
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto
"""

import argparse
import os
import sys
from pathlib import Path

# ── Bootstrap: anchor everything to the script's own directory ──────────────
_SCRIPT_DIR = Path(__file__).resolve().parent

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

os.environ.setdefault("UV_PROJECT", str(_SCRIPT_DIR))

_env_file = _SCRIPT_DIR / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

from renamer import AnimeRenamer, Config, Provider, ProviderRegistry, SeriesCache, get_logger  # noqa: E402

log = get_logger()

BANNER = r"""
+----------------------------------------------------------+
|        JELLYFIN ANIME RENAMER  v4.0                      |
|    Plugin Providers  |  Season folders  |  Undo-safe     |
+----------------------------------------------------------+"""

MENU = """
  +----------------------------------------------+
  |  1. Dry Run  (preview only)                  |
  |  2. Live Rename + Organise                   |
  |  3. Undo previous renames                    |
  |  4. Show config                              |
  |  5. Switch provider                          |
  |  6. Clear series cache (re-search)           |
  |  7. Set title / provider ID manually         |
  |  8. Exit                                     |
  +----------------------------------------------+"""


def build_config(
    path: Path | None,
    provider: Provider | None = None,
    series_name: str | None = None,
    tmdb_id: int | None = None,
    anilist_id: int | None = None,
    kitsu_id: int | None = None,
) -> Config:
    if path:
        path = path.resolve()
        if not path.exists():
            print(f"\n  Path does not exist: {path}\n")
            sys.exit(1)
        if not path.is_dir():
            print(f"\n  Path is not a directory: {path}\n")
            sys.exit(1)
        return Config(
            media_dir=path,
            provider=provider,
            series_name=series_name,
            tmdb_series_id=tmdb_id,
            anilist_id=anilist_id,
            kitsu_id=kitsu_id,
        )
    return Config(
        provider=provider,
        series_name=series_name,
        tmdb_series_id=tmdb_id,
        anilist_id=anilist_id,
        kitsu_id=kitsu_id,
    )


def show_config(cfg: Config) -> None:
    org = "Yes" if cfg.ORGANIZE_INTO_FOLDERS else "No"
    cache = SeriesCache(cfg.MEDIA_DIR)
    cache_status = cache.path.name if cache.path.exists() else "(none — will search on run)"

    provider_label = cfg.PROVIDER.value.upper()
    if cfg.PROVIDER == Provider.TMDB:
        provider_desc = f"TMDB (id={cfg.TMDB_SERIES_ID})" if cfg.TMDB_SERIES_ID else "TMDB (auto-resolve)"
    elif cfg.PROVIDER == Provider.AniList:
        provider_desc = f"AniList (id={cfg.ANILIST_ID})" if cfg.ANILIST_ID else "AniList (auto-resolve)"
    elif cfg.PROVIDER == Provider.Kitsu:
        provider_desc = f"Kitsu (id={cfg.KITSU_ID})" if cfg.KITSU_ID else "Kitsu (auto-resolve)"
    else:
        provider_desc = "Unknown"

    print(f"""
  Series          : {cfg.SERIES_NAME}
  Provider        : {provider_label}
  Provider ID     : {provider_desc}
  TMDB ID         : {cfg.TMDB_SERIES_ID if cfg.TMDB_SERIES_ID is not None else "(auto-resolve via search)"}
  AniList ID      : {cfg.ANILIST_ID if cfg.ANILIST_ID is not None else "(auto-lookup)"}
  Kitsu ID        : {cfg.KITSU_ID if cfg.KITSU_ID is not None else "(auto-lookup)"}
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


def set_manual_info(cfg: Config) -> None:
    """Interactive menu for manually setting series title and provider IDs."""
    print("""
  +--------------------------------------------------------------+
  |  Set Series Title & Provider IDs Manually                     |
  +--------------------------------------------------------------+
  |  Leave blank to keep the current value.                       |
  |  Setting a provider ID skips the search for that provider.    |
  +--------------------------------------------------------------+""")

    # ── Series title ──────────────────────────────────────────
    current_title = cfg.SERIES_NAME
    new_title = input(f"  Series title [{current_title}]: ").strip()
    if new_title:
        cfg.SERIES_NAME = new_title
        print(f"  Title set to: {new_title}")
    else:
        print(f"  Title unchanged: {current_title}")

    # ── Provider IDs ──────────────────────────────────────────
    id_fields = [
        ("TMDB", "TMDB_SERIES_ID", Provider.TMDB),
        ("AniList", "ANILIST_ID", Provider.AniList),
        ("Kitsu", "KITSU_ID", Provider.Kitsu),
    ]

    for label, attr, provider in id_fields:
        current_val = getattr(cfg, attr)
        current_str = str(current_val) if current_val is not None else "(auto)"
        raw = input(f"  {label} ID [{current_str}]: ").strip()

        if not raw:
            continue

        # Allow 'clear' or '0' to reset
        if raw.lower() in ("clear", "reset", "none", "0"):
            setattr(cfg, attr, None)
            print(f"  {label} ID cleared.")
            continue

        try:
            val = int(raw)
            setattr(cfg, attr, val)
            print(f"  {label} ID set to: {val}")
        except ValueError:
            print(f"  Invalid integer — {label} ID unchanged.")

    # ── Optionally switch provider to match the ID you just set ──
    print("\n  Which provider should be active?")
    registered = ProviderRegistry.registered_providers()
    for i, p in enumerate(registered, start=1):
        marker = " (current)" if p == cfg.PROVIDER else ""
        print(f"    {i}. {p.value.upper()}{marker}")
    print("    Enter to keep current.")

    prov_choice = input("  Provider: ").strip()
    if prov_choice:
        try:
            idx = int(prov_choice) - 1
            if 0 <= idx < len(registered):
                cfg.PROVIDER = registered[idx]
                print(f"  Provider switched to: {cfg.PROVIDER.value.upper()}")
        except (ValueError, IndexError):
            print("  Provider unchanged.")

    # ── Update cache if we have an ID ────────────────────────
    cache = SeriesCache(cfg.MEDIA_DIR)
    cache.save(
        series_name=cfg.SERIES_NAME,
        provider=cfg.PROVIDER,
        tmdb_series_id=cfg.TMDB_SERIES_ID,
        anilist_id=cfg.ANILIST_ID,
        kitsu_id=cfg.KITSU_ID,
    )
    print(f"\n  Series cache updated. Ready to dry-run or live rename!")


def switch_provider(cfg: Config) -> None:
    """Interactive provider switching menu."""
    # Build menu dynamically from the registry
    registered = ProviderRegistry.registered_providers()

    provider_descriptions = {
        Provider.TMDB: "The Movie Database — Requires API key, best season/episode data",
        Provider.AniList: "Free GraphQL API — No API key needed, human-curated romaji",
        Provider.Kitsu: "Kitsu.io free JSON API — Best anime season splits via sequel chain",
    }

    menu_lines = []
    for i, p in enumerate(registered, start=1):
        desc = provider_descriptions.get(p, p.value)
        marker = " (current)" if p == cfg.PROVIDER else ""
        menu_lines.append(f"  |  {i}. {p.value.upper():<10} — {desc}{marker}")

    menu_text = "\n".join(menu_lines)

    print(f"""
  +--------------------------------------------------------------+
  |  Current provider: {cfg.PROVIDER.value.upper():<41}|
  +--------------------------------------------------------------+
{menu_text}
  +--------------------------------------------------------------+""")

    choice = input(f"  Select provider (1-{len(registered)}, Enter to cancel): ").strip()

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(registered):
            target = registered[idx]
        else:
            print("  Cancelled.")
            return
    except (ValueError, IndexError):
        print("  Cancelled.")
        return

    if cfg.PROVIDER == target:
        print(f"  Already using {target.value.upper()}.")
        return

    cfg.PROVIDER = target
    label = target.value.upper()
    if target == Provider.Kitsu:
        print(f"  Switched to {label} provider (best for anime season splits!).")
    elif target == Provider.AniList:
        print(f"  Switched to {label} provider (no API key required!).")
    else:
        print(f"  Switched to {label} provider.")

    # Clear cache so next run re-searches with the new provider
    cache = SeriesCache(cfg.MEDIA_DIR)
    if cache.path.exists():
        cache.clear()
        print("  Series cache cleared (will re-search on next run).")


def interactive_menu(cfg: Config) -> None:
    renamer = AnimeRenamer(cfg)
    print(BANNER)

    print(f"\n  Working folder: {cfg.MEDIA_DIR}")
    print(f"  Provider: {cfg.PROVIDER.value.upper()}")

    while True:
        print(MENU)
        choice = input("  Option (1-7): ").strip()

        if choice == "1":
            renamer.run(dry_run=True)
        elif choice == "2":
            print("\n  This will rename and move files on disk.")
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
            print(f"\n  Provider: {cfg.PROVIDER.value.upper()}")
        elif choice == "6":
            cache = SeriesCache(cfg.MEDIA_DIR)
            if cache.path.exists():
                cache.clear()
                print("  Series cache cleared. Next run will re-search.")
            else:
                print("  No series cache found for this folder.")
        elif choice == "7":
            set_manual_info(cfg)
        elif choice == "8":
            print("\n  Smooth sailing!\n")
            sys.exit(0)
        else:
            print("  Invalid choice — enter 1 through 8.")


def parse_args() -> argparse.Namespace:
    # Build provider choices dynamically
    provider_choices = [p.value for p in Provider]

    parser = argparse.ArgumentParser(
        prog="jellyfin_renamer",
        description="Rename anime episodes to Jellyfin SxxExx format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
examples:
  # Interactive menu, folder from .env
  uv run jellyfin_renamer.py

  # Interactive menu, specific folder
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto

  # Non-interactive dry run
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --dry-run

  # Non-interactive live rename
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --run

  # Use AniList as the metadata provider (no TMDB API key needed)
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --provider anilist

  # Use Kitsu (best for complex season splits like Re:Zero)
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --provider kitsu

  # Override series title manually
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --title "Re:Zero"

  # Provide a provider ID directly (skip search entirely)
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/ReZero --provider anilist --anilist-id 113328

  # Full manual: title + provider + ID
  uv run jellyfin_renamer.py --path /mnt/D/Torrent/ReZero --title "Re:Zero" --provider kitsu --kitsu-id 13093

providers:
  tmdb     — The Movie Database (default, requires TMDB_API_KEY)
  anilist  — AniList GraphQL API (free, no key needed)
  kitsu    — Kitsu JSON API (free, best anime season splits via sequel chain)
""",
    )
    parser.add_argument(
        "--path", "-p", type=Path, default=None, metavar="DIR",
        help="folder to scan (overrides MEDIA_DIR in .env)",
    )
    parser.add_argument(
        "--provider", type=str, default=None,
        choices=provider_choices, metavar="PROVIDER",
        help=f"metadata provider: {', '.join(provider_choices)}",
    )
    parser.add_argument(
        "--title", "-t", type=str, default=None, metavar="TITLE",
        help="override series title (skip auto-detection from folder name)",
    )
    parser.add_argument(
        "--tmdb-id", type=int, default=None, metavar="ID",
        help="TMDB series ID (skips TMDB search)",
    )
    parser.add_argument(
        "--anilist-id", type=int, default=None, metavar="ID",
        help="AniList series ID (skips AniList search)",
    )
    parser.add_argument(
        "--kitsu-id", type=int, default=None, metavar="ID",
        help="Kitsu series ID (skips Kitsu search)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", "-d", action="store_true",
                      help="preview renames without touching files, then exit")
    mode.add_argument("--run", "-r", action="store_true",
                      help="rename and organise files immediately, then exit (no menu)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    provider = None
    if args.provider:
        provider = Provider.from_str(args.provider)

    cfg = build_config(
        args.path,
        provider=provider,
        series_name=args.title,
        tmdb_id=args.tmdb_id,
        anilist_id=args.anilist_id,
        kitsu_id=args.kitsu_id,
    )

    if args.dry_run:
        print(BANNER)
        print(f"\n  Working folder: {cfg.MEDIA_DIR}")
        print(f"  Provider: {cfg.PROVIDER.value.upper()}")
        print("  DRY RUN — no files will be changed\n")
        AnimeRenamer(cfg).run(dry_run=True)
        return

    if args.run:
        print(BANNER)
        print(f"\n  Working folder: {cfg.MEDIA_DIR}")
        print(f"  Provider: {cfg.PROVIDER.value.upper()}")
        print("  LIVE RUN — files will be renamed\n")
        AnimeRenamer(cfg).run(dry_run=False)
        return

    interactive_menu(cfg)


if __name__ == "__main__":
    main()
