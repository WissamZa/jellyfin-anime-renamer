"""
renamer.cli.menus
=================
Interactive selection menus and configuration prompts for Jellyfin Anime Renamer.
"""

from renamer.config import (
    Config,
    Provider,
    START_MODE_CONTINUING,
    START_MODE_PER_SEASON,
    get_logger,
)
from renamer.renamer import AnimeRenamer
from renamer.picker import Picker
from renamer.providers.base import EpisodeGroupInfo
from renamer.romaniser import get_romaniser

log = get_logger()


def show_config(cfg: Config) -> None:
    org = "Yes" if cfg.organize_into_folders else "No"
    abs_num = "Yes" if cfg.absolute_numbering else "No"
    ep_mode = (
        "Continuing" if cfg.episode_start_mode == START_MODE_CONTINUING
        else "Per-season"
    )
    eg = cfg.episode_group_id or "(none)"
    print(f"""
  Series          : {cfg.series_name}
  Provider        : {cfg.provider.value}
  TMDB ID         : {cfg.tmdb_series_id}
  AniList ID      : {cfg.anilist_id}
  Kitsu ID        : {cfg.kitsu_id}
  Episode Group   : {eg}
  Media dir       : {cfg.media_dir}
  Template        : {cfg.name_template}
  Special template: {cfg.special_template}
  Organise folders: {org}
  Absolute nums   : {abs_num}
  Episode start   : {ep_mode}
  Season folder   : {cfg.season_folder_template.format(season=1)}  (example)
  Specials folder : {cfg.specials_folder_name}
  Extensions      : {', '.join(cfg.video_extensions)}
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
    print(f"\n  Current title: {cfg.series_name}")
    new_title = input("  New title (Enter to keep): ").strip()
    if new_title:
        cfg.series_name = new_title
        print(f"  Title set to: {cfg.series_name}")

    print(f"\n  Current TMDB ID: {cfg.tmdb_series_id}")
    new_tmdb = input("  New TMDB ID (Enter to keep): ").strip()
    if new_tmdb:
        try:
            cfg.tmdb_series_id = int(new_tmdb)
            print(f"  TMDB ID set to: {cfg.tmdb_series_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current AniList ID: {cfg.anilist_id}")
    new_al = input("  New AniList ID (Enter to keep): ").strip()
    if new_al:
        try:
            cfg.anilist_id = int(new_al)
            print(f"  AniList ID set to: {cfg.anilist_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Kitsu ID: {cfg.kitsu_id}")
    new_kitsu = input("  New Kitsu ID (Enter to keep): ").strip()
    if new_kitsu:
        try:
            cfg.kitsu_id = int(new_kitsu)
            print(f"  Kitsu ID set to: {cfg.kitsu_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Episode Group: {cfg.episode_group_id or '(none)'}")
    new_eg = input("  New Episode Group ID (Enter to keep, '0' to clear): ").strip()
    if new_eg == "0":
        cfg.episode_group_id = None
        print("  Episode Group cleared.")
    elif new_eg:
        cfg.episode_group_id = new_eg
        print(f"  Episode Group set to: {cfg.episode_group_id}")


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

    if not cfg.tmdb_series_id:
        print("\n  TMDB series ID is not set. Use manual configuration to set it, or")
        print("  run a dry-run first (auto-search will find it).")
        return

    current_eg = cfg.episode_group_id
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
    options: list[tuple[str, str | EpisodeGroupInfo]] = [(default_label, "default")]

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
            cfg.episode_group_id = None
            cfg.absolute_numbering = False
            print("  Using default TMDB ordering.")
        return

    for g in groups:
        desc = f" — {g.description}" if g.description else ""
        marker = "  (current)" if current_eg and g.id == current_eg else ""
        label = f"{g.name}  ({g.type_label}, {g.episode_count} eps, {g.group_count} seasons){desc}{marker}"
        options.append((label, g))

    # Pre-select current group or default
    default_idx = 0
    if current_eg:
        for i, (_, val) in enumerate(options):
            if isinstance(val, EpisodeGroupInfo) and val.id == current_eg:
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
        cfg.episode_group_id = None
        cfg.absolute_numbering = False
        print("  Switched to default TMDB ordering.")
        return

    cfg.episode_group_id = selected.id

    # Auto-enable absolute numbering for Absolute Order groups
    if selected.type == 2:  # Absolute Order
        cfg.absolute_numbering = True
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


def auto_prompt_episode_groups(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Called before running the renamer. If using TMDB with a series
    ID, auto-fetch available episode groups and present the picker.

    Always shows "Default TMDB Ordering" as the first option.
    If an episode group is already selected, pre-selects it so the
    user can confirm or switch.
    """
    if cfg.provider != Provider.TMDB:
        return
    if not cfg.tmdb_series_id:
        return  # No series to look up

    current_eg = cfg.episode_group_id

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
    options: list[tuple[str, str | EpisodeGroupInfo]] = [(default_label, "default")]

    for g in groups:
        marker = "  (current)" if current_eg and g.id == current_eg else ""
        label = f"{g.name}  ({g.type_label}, {g.episode_count} eps){marker}"
        options.append((label, g))

    # Pre-select current group or default
    default_idx = 0
    if current_eg:
        for i, (_, val) in enumerate(options):
            if isinstance(val, EpisodeGroupInfo) and val.id == current_eg:
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
        cfg.episode_group_id = None
        cfg.absolute_numbering = False
        print("  Using default TMDB ordering.")
        return

    cfg.episode_group_id = selected.id
    if selected.type == 2:
        cfg.absolute_numbering = True
        print(
            f"  Using: {selected.name} (absolute numbering auto-enabled)"
        )
    else:
        print(f"  Using: {selected.name}")


def select_series_title(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Interactive series title selection.

    Fetches all available titles from:
      1. TMDB Alternative Titles + Translations + main name
      2. AniList romaji / english / native
      3. pykakasi romanisation of the Japanese TMDB title

    The romaji title (AniList cross-referenced) is the default.
    All titles are presented in the picker so the user can choose.
    """
    if cfg.provider != Provider.TMDB:
        print("\n  Title selection requires the TMDB provider.")
        print(f"  Current provider: {cfg.provider.value}")
        return

    if not cfg.tmdb_series_id:
        print("\n  TMDB series ID is not set. Run a dry-run first.")
        return

    print(f"\n  Current title: {cfg.series_name}")
    print("  Fetching available titles from TMDB + AniList …")

    # 1. Fetch TMDB alternative titles
    tmdb_titles = renamer.list_alternative_titles()

    # 2. Fetch AniList titles for cross-reference
    anilist_titles: dict[str, str] = {}  # {romaji|english|native: title}
    try:
        from renamer.providers.anilist import AniListFetcher
        af = AniListFetcher()
        media = af._gql(af.SERIES_QUERY, {"search": cfg.series_name})
        if media:
            titles = media.get("title", {})
            for key in ("romaji", "english", "native"):
                val = titles.get(key)
                if val:
                    anilist_titles[key] = val
    except Exception as exc:
        log.warning("AniList title lookup failed: %s", exc)

    # 3. Romanise any Japanese titles from TMDB
    romaniser = get_romaniser()
    romaji_from_jp = ""
    for t in tmdb_titles:
        if t.get("type") == "japanese" and romaniser.is_japanese(t["title"]):
            romaji_from_jp = romaniser.to_romaji(t["title"])
            break

    # Determine the best default romaji title
    best_romaji = (
        anilist_titles.get("romaji")
        or romaji_from_jp
        or anilist_titles.get("english")
        or cfg.series_name
    )

    # Build picker options — deduplicated
    seen_titles: set[str] = set()
    options: list[tuple[str, str]] = []

    def _add(title: str, label: str) -> None:
        """Add a title option if not already seen."""
        clean = title.strip()
        if not clean or clean in seen_titles:
            return
        seen_titles.add(clean)
        marker = "  (current)" if clean == cfg.series_name else ""
        options.append((f"{label}: {clean}{marker}", clean))

    # Default romaji first
    _add(best_romaji, "Romaji (recommended)")

    # AniList titles
    for key, label in [("romaji", "AniList Romaji"), ("english", "AniList English"), ("native", "AniList Native")]:
        if key in anilist_titles:
            _add(anilist_titles[key], label)

    # Romanised Japanese from TMDB
    if romaji_from_jp and romaji_from_jp not in seen_titles:
        _add(romaji_from_jp, "TMDB Romaji (pykakasi)")

    # All TMDB alternative titles
    type_labels = {
        "primary": "TMDB English",
        "original": "TMDB Original",
        "japanese": "TMDB Japanese",
        "translation": "TMDB Translation",
        "3": "TMDB Original Type",
        "4": "TMDB Primary Type",
    }
    for t in tmdb_titles:
        tl = type_labels.get(str(t.get("type", "")), "TMDB Alt")
        iso = t.get("iso_3166_1", "")
        if iso:
            tl += f" [{iso}]"
        _add(t["title"], tl)

    # Current title
    _add(cfg.series_name, "Current")

    if not options:
        print("  No alternative titles found.")
        return

    # Pre-select current title or romaji default
    default_idx = 0
    for i, (_, val) in enumerate(options):
        if val == cfg.series_name:
            default_idx = i
            break

    result = Picker(
        options,
        title="Select Series Title",
        default_index=default_idx,
    ).run()

    if result is None:
        print("  Cancelled.")
        return

    _, selected_title = result
    if selected_title == cfg.series_name:
        print("  Title unchanged.")
        return

    cfg.series_name = selected_title
    print(f"  Title set to: {selected_title}")


def select_episode_start_mode(cfg: Config) -> None:
    """
    Interactive episode numbering mode selection.

    Per-season  — each season starts at E01 (e.g. S02E01)
    Continuing  — episodes continue from previous season
                  (e.g. if S1 had 12 eps, S2 starts at E13)
    """
    current = cfg.episode_start_mode
    options = [
        (
            f"Per-season  — each season starts at E01  {'(current)' if current == START_MODE_PER_SEASON else ''}",
            START_MODE_PER_SEASON,
        ),
        (
            f"Continuing  — continue from previous season  {'(current)' if current == START_MODE_CONTINUING else ''}",
            START_MODE_CONTINUING,
        ),
    ]

    default_idx = 0 if current == START_MODE_PER_SEASON else 1

    result = Picker(
        options,
        title="Episode Numbering Mode",
        default_index=default_idx,
    ).run()

    if result is None:
        print("  Cancelled.")
        return

    _, selected = result
    if selected == current:
        print("  Mode unchanged.")
        return

    cfg.episode_start_mode = selected
    if selected == START_MODE_CONTINUING:
        print(
            "  Switched to continuing mode.\n"
            "  Episodes will continue from the previous season.\n"
            "  Example: if S1 has 12 eps, S2E01 becomes S02E13."
        )
    else:
        print(
            "  Switched to per-season mode.\n"
            "  Each season starts at E01."
        )
