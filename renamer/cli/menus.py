"""
renamer.cli.menus
=================
Interactive selection menus and configuration prompts for Jellyfin Anime Renamer.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from renamer.cache import SeriesCache
from renamer.config import (
    START_MODE_CONTINUING,
    START_MODE_PER_SEASON,
    TITLE_LANG_ENGLISH,
    TITLE_LANG_ROMAJI,
    Config,
    Provider,
    get_logger,
)
from renamer.icons import (
    has_folder_icon,
    remove_folder_icon,
    set_folder_icon,
)
from renamer.picker import BackSignal, Picker, back_input
from renamer.providers.base import EpisodeGroupInfo
from renamer.romaniser import get_romaniser

if TYPE_CHECKING:
    from renamer.cli.multi_series import DiscoveredSeries
    from renamer.renamer import AnimeRenamer

log = get_logger(__name__)


def _save_cache(cfg: Config) -> None:
    """Save the updated configuration to the folder's series cache immediately."""
    SeriesCache(cfg.media_dir).save(
        series_name=cfg.series_name,
        provider=cfg.provider,
        tmdb_series_id=cfg.tmdb_series_id,
        anilist_id=cfg.anilist_id,
        kitsu_id=cfg.kitsu_id,
        episode_group_id=cfg.episode_group_id,
        episode_start_mode=cfg.episode_start_mode,
        episode_title_lang=cfg.episode_title_lang,
        season_arc_names=cfg.season_arc_names,
    )


def show_config(cfg: Config) -> None:
    from renamer import __version__
    from renamer.security import mask_api_key

    org = "Yes" if cfg.organize_into_folders else "No"
    abs_num = "Yes" if cfg.absolute_numbering else "No"
    use_hash = "Yes" if cfg.use_hash else "No"
    scan_rec = "Yes" if cfg.scan_recursive else "No"
    ep_mode = "Continuing" if cfg.episode_start_mode == START_MODE_CONTINUING else "Per-season"
    eg = cfg.episode_group_id or "(none)"
    title_lang = cfg.episode_title_lang.capitalize()
    arc_names = (
        ", ".join(f"S{s}: {n}" for s, n in sorted(cfg.season_arc_names.items()))
        if cfg.season_arc_names
        else "(none)"
    )
    # Mask sensitive values
    tmdb_key_display = mask_api_key(cfg.tmdb_api_key) if cfg.tmdb_api_key else "(not set)"
    anidb_pass_display = mask_api_key(cfg.anidb_password) if cfg.anidb_password else "(not set)"
    anidb_api_display = mask_api_key(cfg.anidb_api_key) if cfg.anidb_api_key else "(not set)"
    print(f"""
  Version         : v{__version__}
  Series          : {cfg.series_name}
  Provider        : {cfg.provider.value}
  TMDB ID         : {cfg.tmdb_series_id}
  TMDB API Key    : {tmdb_key_display}
  AniList ID      : {cfg.anilist_id}
  Kitsu ID        : {cfg.kitsu_id}
  AniDB User      : {cfg.anidb_username or "(not set)"}
  AniDB Pass      : {anidb_pass_display}
  AniDB API Key   : {anidb_api_display}
  AniDB Offline   : {"Yes" if cfg.anidb_offline else "No"}
  Episode Group   : {eg}
  Title language  : {title_lang}
  Season arcs     : {arc_names}
  Media dir       : {cfg.media_dir}
  Base DL path    : {cfg.base_download_path or "(not set — uses env BASE_DOWNLOAD_PATH)"}
  Current dir     : {Path.cwd()}
  Template        : {cfg.name_template}
  Special template: {cfg.special_template}
  Organise folders: {org}
  Absolute nums   : {abs_num}
  Episode start   : {ep_mode}
  Use hash lookup : {use_hash}
  Scan recursive  : {scan_rec}
  Scan depth      : {cfg.scan_depth}
  Season folder   : {cfg.season_folder_template.format(season=1)}  (example)
  Specials folder : {cfg.specials_folder_name}
  Extensions      : {", ".join(cfg.video_extensions)}
  Subtitle exts   : {", ".join(cfg.subtitle_extensions)}
  History         : {cfg.history_file}
  Log file        : renamer.log""")


def switch_provider(cfg: Config) -> None:
    """Interactive provider switching."""
    providers = [
        (f"TMDB  {'(current)' if cfg.provider == Provider.TMDB else ''}", "tmdb"),
        (f"AniList  {'(current)' if cfg.provider == Provider.AniList else ''}", "anilist"),
        (f"Kitsu  {'(current)' if cfg.provider == Provider.Kitsu else ''}", "kitsu"),
        (f"AniDB (hash-based)  {'(current)' if cfg.provider == Provider.AniDB else ''}", "anidb"),
    ]
    result = Picker(
        providers,
        title="Switch provider",
        default_index=[Provider.TMDB, Provider.AniList, Provider.Kitsu, Provider.AniDB].index(
            cfg.provider
        )
        if cfg.provider in [Provider.TMDB, Provider.AniList, Provider.Kitsu, Provider.AniDB]
        else 0,
    ).run()
    if result:
        cfg.provider = Provider.from_str(result[1])
        print(f"  Switched to: {cfg.provider.value}")
        _save_cache(cfg)


def set_manual_info(cfg: Config) -> None:
    """Interactive title/ID entry."""
    print(f"\n  Current title: {cfg.series_name}")
    try:
        new_title = back_input("  New title (Enter to keep, Ctrl+Q to go back): ")
    except BackSignal:
        print("  Back.")
        return
    if new_title:
        cfg.series_name = new_title
        print(f"  Title set to: {cfg.series_name}")

    print(f"\n  Current TMDB ID: {cfg.tmdb_series_id}")
    try:
        new_tmdb = back_input("  New TMDB ID (Enter to keep, Ctrl+Q to go back): ")
    except BackSignal:
        print("  Back.")
        return
    if new_tmdb:
        try:
            cfg.tmdb_series_id = int(new_tmdb)
            print(f"  TMDB ID set to: {cfg.tmdb_series_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current AniList ID: {cfg.anilist_id}")
    try:
        new_al = back_input("  New AniList ID (Enter to keep, Ctrl+Q to go back): ")
    except BackSignal:
        print("  Back.")
        return
    if new_al:
        try:
            cfg.anilist_id = int(new_al)
            print(f"  AniList ID set to: {cfg.anilist_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Kitsu ID: {cfg.kitsu_id}")
    try:
        new_kitsu = back_input("  New Kitsu ID (Enter to keep, Ctrl+Q to go back): ")
    except BackSignal:
        print("  Back.")
        return
    if new_kitsu:
        try:
            cfg.kitsu_id = int(new_kitsu)
            print(f"  Kitsu ID set to: {cfg.kitsu_id}")
        except ValueError:
            print("  Invalid integer — not changed.")

    print(f"\n  Current Episode Group: {cfg.episode_group_id or '(none)'}")
    try:
        new_eg = back_input(
            "  New Episode Group ID (Enter to keep, '0' to clear, Ctrl+Q to go back): "
        )
    except BackSignal:
        print("  Back.")
        return
    if new_eg == "0":
        cfg.episode_group_id = None
        print("  Episode Group cleared.")
    elif new_eg:
        cfg.episode_group_id = new_eg
        print(f"  Episode Group set to: {cfg.episode_group_id}")
    _save_cache(cfg)


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
        print("\n  TMDB series ID is not set. Searching TMDB …")
        renamer._auto_search_series()
        if not cfg.tmdb_series_id:
            print("  Could not find the series on TMDB.")
            print("  Try setting the title or ID manually (option 2 in the menu).")
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
            _save_cache(cfg)
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
        _save_cache(cfg)
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
        # Auto-populate arc names from the selected episode group
        _auto_import_arc_names_from_group(cfg, renamer, selected.id)
    _save_cache(cfg)


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
        _save_cache(cfg)
        return

    cfg.episode_group_id = selected.id
    if selected.type == 2:
        cfg.absolute_numbering = True
        print(f"  Using: {selected.name} (absolute numbering auto-enabled)")
    else:
        print(f"  Using: {selected.name}")
        # Auto-populate arc names from the selected episode group
        _auto_import_arc_names_from_group(cfg, renamer, selected.id)
    _save_cache(cfg)


def select_series_title(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Interactive series title selection.

    If the TMDB series ID is not set, first runs an auto-search to find
    and set it.  Then fetches all available titles from:
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

    # If TMDB ID is not set, search for it first
    if not cfg.tmdb_series_id:
        print("\n  TMDB series ID is not set. Searching TMDB …")
        renamer._auto_search_series()
        if not cfg.tmdb_series_id:
            print("  Could not find the series on TMDB.")
            print("  Try setting the title or ID manually (option 2 in the menu).")
            return
        print(f"  Found: {cfg.series_name} (TMDB ID: {cfg.tmdb_series_id})")
        _save_cache(cfg)

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
    for key, label in [
        ("romaji", "AniList Romaji"),
        ("english", "AniList English"),
        ("native", "AniList Native"),
    ]:
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
    _save_cache(cfg)


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
    _save_cache(cfg)
    if selected == START_MODE_CONTINUING:
        print(
            "  Switched to continuing mode.\n"
            "  Episodes will continue from the previous season.\n"
            "  Example: if S1 has 12 eps, S2E01 becomes S02E13."
        )
    else:
        print("  Switched to per-season mode.\n  Each season starts at E01.")


def select_episode_title_lang(cfg: Config) -> None:
    """
    Interactive episode title language selection.

    Romaji  — episode titles are romanised from Japanese (default)
    English — episode titles are in English from TMDB

    This affects the {title} placeholder in the renamed filename.
    """
    current = cfg.episode_title_lang
    options = [
        (
            f"Romaji  — romanised Japanese titles  {'(current)' if current == TITLE_LANG_ROMAJI else ''}",
            TITLE_LANG_ROMAJI,
        ),
        (
            f"English  — English titles from TMDB  {'(current)' if current == TITLE_LANG_ENGLISH else ''}",
            TITLE_LANG_ENGLISH,
        ),
    ]

    default_idx = 0 if current == TITLE_LANG_ROMAJI else 1

    result = Picker(
        options,
        title="Episode Title Language",
        default_index=default_idx,
    ).run()

    if result is None:
        print("  Cancelled.")
        return

    _, selected = result
    if selected == current:
        print("  Language unchanged.")
        return

    cfg.episode_title_lang = selected
    _save_cache(cfg)
    if selected == TITLE_LANG_ENGLISH:
        print(
            "  Switched to English episode titles.\n"
            "  Episode names will use the English TMDB title directly.\n"
            "  Example: 'One Piece - S01E01 - I'm Luffy! The Man Who's Gonna Be King of the Pirates!.mkv'"
        )
    else:
        print(
            "  Switched to Romaji episode titles.\n"
            "  Episode names will be romanised from the Japanese title.\n"
            "  Example: 'One Piece - S01E01 - Ore wa Luffy! Kaizoku Ou ni Ore wa Naru.mkv'"
        )


def configure_season_arc_names(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Interactive season arc name configuration.

    Lets users type custom season names like TMDB Episode Groups do,
    e.g.  Season 1 -> "East Blue (1-61)"
          Season 2 -> "Alabasta (62-135)"

    These names are stored in cfg.season_arc_names as {season_num: "Arc Name"}.

    The user can either:
      - Type arc names manually for each season
      - Import arc names from a TMDB Episode Group (if available)

    The season arc names are used by the season folder template.
    """
    current_arcs = cfg.season_arc_names.copy()

    while True:
        # Show current arc names
        arc_display = ""
        if current_arcs:
            for s, n in sorted(current_arcs.items()):
                arc_display += f"\n    Season {s}: {n}"
        else:
            arc_display = "\n    (none set — using default 'Season XX' folders)"

        options = [
            ("Type arc names for each season manually", "manual"),
            ("Import arc names from TMDB Episode Group", "import_group"),
            ("Clear all arc names (reset to default)", "clear"),
            (f"<-- Back{arc_display}", "back"),
        ]

        result = Picker(
            options,
            title="SEASON ARC NAMES",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]

        if action == "manual":
            _type_arc_names_manually(cfg, current_arcs)

        elif action == "import_group":
            _import_arc_names_from_group(cfg, renamer, current_arcs)

        elif action == "clear":
            current_arcs.clear()
            cfg.season_arc_names = {}
            _save_cache(cfg)
            print("  All arc names cleared. Will use default 'Season XX' folders.")

    # Apply final changes
    cfg.season_arc_names = current_arcs
    _save_cache(cfg)


def _type_arc_names_manually(cfg: Config, current_arcs: dict[int, str]) -> None:
    """
    Let the user type arc names for each season interactively.

    Format example (like TMDB episode groups):
      Season 1: East Blue (1-61)
      Season 2: Alabasta (62-135)
      Season 3: Skypiea (136-195)
    """
    print("\n  Type arc names for each season.")
    print("  Format: 'Arc Name (episode range)' — e.g. 'East Blue (1-61)'")
    print("  Press Enter on a season to keep its current name, or type '0' to clear it.")
    print("  Press Enter on 'Next season number' to stop adding.\n")

    # Show existing names first
    if current_arcs:
        print("  Current arc names:")
        for s, n in sorted(current_arcs.items()):
            print(f"    Season {s}: {n}")
        print()

    # Edit existing and add new
    max_existing = max(current_arcs.keys()) if current_arcs else 0

    # Edit existing arcs
    for s in sorted(current_arcs.keys()):
        current_name = current_arcs[s]
        try:
            new_name = back_input(
                f"  Season {s} [{current_name}] (Ctrl+Q to go back): ", default=current_name
            )
        except BackSignal:
            print("  Back.")
            return
        if new_name == "0":
            del current_arcs[s]
            print(f"  Season {s} arc name cleared.")
        elif new_name and new_name != current_name:
            current_arcs[s] = new_name
            print(f"  Season {s} set to: {new_name}")

    # Add new arcs
    next_season = max_existing + 1
    while True:
        try:
            new_name = back_input(f"  Season {next_season} [Enter to stop, Ctrl+Q to go back]: ")
        except BackSignal:
            print("  Back.")
            return
        if not new_name:
            break
        if new_name == "0":
            next_season += 1
            continue
        current_arcs[next_season] = new_name
        print(f"  Season {next_season} set to: {new_name}")
        next_season += 1

    cfg.season_arc_names = current_arcs
    _save_cache(cfg)
    print(f"\n  Arc names configured for {len(current_arcs)} season(s).")


def _auto_import_arc_names_from_group(
    cfg: Config,
    renamer: AnimeRenamer,
    group_id: str,
) -> None:
    """
    Automatically import arc names from a TMDB Episode Group
    when one is selected.

    This fetches the group's sub-group names and stores them as
    season arc names, so season folders get named like "East Blue (1-61)"
    instead of "Season 01".

    This is called automatically when an episode group is selected —
    no user confirmation needed since the arc names improve the
    Jellyfin experience.
    """
    if cfg.provider != Provider.TMDB:
        return

    try:
        from renamer.providers.tmdb import TMDBFetcher

        if not cfg.tmdb_series_id:
            return
        fetcher = TMDBFetcher(cfg.tmdb_api_key, cfg.tmdb_series_id, cfg)

        data = fetcher._get(
            f"{fetcher.BASE}/tv/episode_group/{group_id}",
            fetcher._params,
            cfg=cfg,
        )
        if not data:
            return

        sub_groups = data.get("groups", [])
        if not sub_groups:
            return

        # Import arc names using the same sequential season numbering
        # as the TMDB fetcher (1, 2, 3, ...)
        regular_season_counter = 0
        imported_arcs: dict[int, str] = {}
        for group in sub_groups:
            gname = group.get("name", "").strip()
            episodes = group.get("episodes", [])

            # Check if it's a specials group
            is_specials = False
            if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
                is_specials = True
            else:
                orig_seasons = {ep.get("season_number", -1) for ep in episodes}
                if orig_seasons == {0}:
                    is_specials = True

            if not is_specials:
                regular_season_counter += 1
                if gname:
                    imported_arcs[regular_season_counter] = gname

        if imported_arcs:
            # Merge with any existing arc names
            for s, name in imported_arcs.items():
                if s not in cfg.season_arc_names:
                    cfg.season_arc_names[s] = name
            print(f"  Auto-imported {len(imported_arcs)} arc name(s) from episode group.")
    except Exception as exc:
        log.debug("Auto-import of arc names failed: %s", exc)
        # Non-critical — don't bother the user


def _import_arc_names_from_group(
    cfg: Config,
    renamer: AnimeRenamer,
    current_arcs: dict[int, str],
) -> None:
    """
    Import season arc names from a TMDB Episode Group.

    Fetches the selected episode group and uses its sub-group names
    as arc names for each season. For example, the One Piece TMDB
    Episode Group "Arcs" has sub-groups like:
      - "East Blue (1-61)"
      - "Alabasta (62-135)"
    These become the season arc names.
    """
    if cfg.provider != Provider.TMDB:
        print("\n  Episode groups are only available with the TMDB provider.")
        print(f"  Current provider: {cfg.provider.value}")
        return

    if not cfg.tmdb_series_id:
        print("\n  TMDB series ID is not set. Searching TMDB …")
        renamer._auto_search_series()
        if not cfg.tmdb_series_id:
            print("  Could not find the series on TMDB.")
            return

    print("  Fetching available episode groups from TMDB …")
    groups = renamer.list_episode_groups()

    if not groups:
        print("  No episode groups found for this series on TMDB.")
        return

    # Build picker options
    options: list[tuple[str, EpisodeGroupInfo]] = []
    for g in groups:
        desc = f" — {g.description}" if g.description else ""
        label = f"{g.name}  ({g.type_label}, {g.episode_count} eps, {g.group_count} seasons){desc}"
        options.append((label, g))

    result = Picker(
        options,
        title="SELECT EPISODE GROUP TO IMPORT ARC NAMES FROM",
        default_index=0,
    ).run()

    if result is None:
        print("  Cancelled.")
        return

    _, selected = result

    # Fetch the group details to get sub-group names
    from renamer.providers.tmdb import TMDBFetcher

    fetcher = TMDBFetcher(cfg.tmdb_api_key, cfg.tmdb_series_id, cfg)

    data = fetcher._get(
        f"{fetcher.BASE}/tv/episode_group/{selected.id}",
        fetcher._params,
        cfg=cfg,
    )
    if not data:
        print("  Could not fetch episode group details.")
        return

    group_name = data.get("name", "Unknown")
    sub_groups = data.get("groups", [])

    if not sub_groups:
        print(f"  No sub-groups found in '{group_name}'.")
        return

    # Build arc names from sub-group names (using sequential season
    # numbering consistent with the TMDB fetcher)
    print(f"\n  Importing arc names from: {group_name}")
    print(f"  Found {len(sub_groups)} sub-groups:\n")

    imported_arcs: dict[int, str] = {}
    regular_season_counter = 0
    for group in sub_groups:
        gname = group.get("name", "").strip()
        episodes = group.get("episodes", [])

        # Check if it's a specials group
        is_specials = False
        if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
            is_specials = True
        else:
            orig_seasons = {ep.get("season_number", -1) for ep in episodes}
            if orig_seasons == {0}:
                is_specials = True

        if is_specials:
            continue  # Skip specials groups

        regular_season_counter += 1
        if not gname:
            gname = f"Season {regular_season_counter}"
        ep_count = len(episodes)
        print(f"    Season {regular_season_counter}: {gname}  ({ep_count} episodes)")
        imported_arcs[regular_season_counter] = gname

    # Ask to confirm
    confirm = Picker(
        [
            ("Apply these arc names", "apply"),
            ("Cancel", "cancel"),
        ],
        title="CONFIRM ARC NAMES IMPORT",
        default_index=0,
    ).run()

    if confirm is None or confirm[1] == "cancel":
        print("  Import cancelled.")
        return

    current_arcs.update(imported_arcs)
    cfg.season_arc_names = current_arcs
    _save_cache(cfg)
    print(f"\n  Imported {len(imported_arcs)} arc name(s) from '{group_name}'.")


# ---------------------------------------------------------------------------
# Select Folder & Edit Series ID
# ---------------------------------------------------------------------------


def edit_series_id_for_folder(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Let the user select a folder and edit/re-search its series ID.

    Options:
      1. Use current MEDIA_DIR
      2. Use current working directory
      3. Enter a custom path
      4. Browse subfolders of MEDIA_DIR

    Once a folder is selected, the user can:
      - See current series ID status
      - Search TMDB/AniList/Kitsu for the correct series
      - Manually set the ID
    """

    # Ask which folder to work on
    dir_options = [
        (f"Current MEDIA_DIR: {cfg.media_dir}", "media_dir"),
        (f"Current working directory: {Path.cwd()}", "cwd"),
        ("Enter a custom path", "custom"),
        ("Browse subfolders of MEDIA_DIR", "browse"),
        ("<-- Back", "back"),
    ]

    result = Picker(
        dir_options,
        title="SELECT FOLDER TO EDIT SERIES ID",
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
        print(f"\n  Selected: {target_dir}")
    elif choice == "custom":
        custom = input("  Enter directory path: ").strip()
        if not custom:
            print("  Cancelled.")
            return
        target_dir = Path(custom).resolve()
        if not target_dir.is_dir():
            print(f"  Directory does not exist: {target_dir}")
            return
    elif choice == "browse":
        # List subfolders of MEDIA_DIR
        video_exts = set(cfg.video_extensions)
        subdirs = sorted(
            d for d in cfg.media_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
        )
        if not subdirs:
            print("  No subfolders found in MEDIA_DIR.")
            return

        browse_options = []
        for d in subdirs:
            # Check for video files
            has_vid = any(f.suffix.lower() in video_exts for f in d.iterdir() if f.is_file())
            # Also check one level deep
            if not has_vid:
                has_vid = any(
                    f.suffix.lower() in video_exts
                    for sub in d.iterdir()
                    if sub.is_dir() and not sub.name.startswith(".")
                    for f in sub.iterdir()
                    if f.is_file()
                )
            marker = "  [video]" if has_vid else ""
            browse_options.append((f"{d.name}{marker}", d))
        browse_options.append(("<-- Back", "back"))

        browse_result = Picker(
            browse_options,
            title="SELECT A SUBFOLDER",
            default_index=0,
        ).run()

        if browse_result is None or browse_result[1] == "back":
            return
        target_dir = browse_result[1]

    if target_dir is None:
        return

    print(f"\n  Selected folder: {target_dir}")

    # Load any existing cache for this folder
    cache = SeriesCache(target_dir)
    cached = cache.load()

    # Temporarily switch cfg to this folder's context

    # Apply the target folder's data
    from renamer.renamer import _clean_folder_name

    cfg.media_dir = target_dir
    cfg.series_name = _clean_folder_name(target_dir.name)
    cfg.tmdb_series_id = None
    cfg.anilist_id = None
    cfg.kitsu_id = None
    cfg.season_arc_names = {}

    if cached:
        if cached.get("series_name"):
            cfg.series_name = cached["series_name"]
        if cached.get("tmdb_series_id") or cached.get("tmdb_id"):
            cfg.tmdb_series_id = cached.get("tmdb_series_id") or cached.get("tmdb_id")
        if cached.get("anilist_id"):
            cfg.anilist_id = cached["anilist_id"]
        if cached.get("kitsu_id"):
            cfg.kitsu_id = cached["kitsu_id"]
        if isinstance(cached.get("provider"), str):
            cfg.provider = Provider.from_str(cached["provider"])
        if cached.get("episode_title_lang"):
            cfg.episode_title_lang = cached["episode_title_lang"]
        if cached.get("season_arc_names"):
            cfg.season_arc_names = {int(k): v for k, v in cached["season_arc_names"].items()}

    # Show current status
    print(f"  Series name: {cfg.series_name}")
    print(f"  TMDB ID:     {cfg.tmdb_series_id or '(not set)'}")
    print(f"  AniList ID:  {cfg.anilist_id or '(not set)'}")
    print(f"  Kitsu ID:    {cfg.kitsu_id or '(not set)'}")

    # Update renamer state
    from renamer.history import RenameHistory

    renamer._cfg = cfg
    renamer._history = RenameHistory(cfg.history_file, media_dir=cfg.media_dir)

    # Offer editing options
    while True:
        # Show current IDs in labels
        edit_labeled = [
            (f"Search provider for the correct series  (current: {cfg.series_name})", "search"),
            (
                f"Set title / provider ID manually  (TMDB:{cfg.tmdb_series_id or '?'} AL:{cfg.anilist_id or '?'})",
                "manual",
            ),
            ("Select series title from TMDB alt titles + AniList", "select_title"),
            ("Reset all IDs for this folder", "reset"),
            ("<-- Done (return to menu)", "done"),
        ]

        edit_result = Picker(
            edit_labeled,
            title=f"EDIT SERIES ID: {target_dir.name}",
            default_index=0,
        ).run()

        if edit_result is None or edit_result[1] == "done":
            break

        action = edit_result[1]
        if action == "search":
            # Reset IDs so auto-search runs fresh
            cfg.tmdb_series_id = None
            cfg.anilist_id = None
            cfg.kitsu_id = None
            renamer._auto_search_series()
            print(f"\n  Updated: {cfg.series_name}")
            print(f"  TMDB ID: {cfg.tmdb_series_id or '(not found)'}")
            _save_cache(cfg)
        elif action == "manual":
            set_manual_info(cfg)
        elif action == "select_title":
            select_series_title(cfg, renamer)
        elif action == "reset":
            cfg.tmdb_series_id = None
            cfg.anilist_id = None
            cfg.kitsu_id = None
            cfg.series_name = _clean_folder_name(target_dir.name)
            cfg.episode_group_id = None
            print(f"  All IDs reset. Series name: {cfg.series_name}")
            _save_cache(cfg)

    # Note: We keep cfg pointing to the selected folder — the user
    # may want to run a rename on it next. If they want to go back
    # to the original, they can use Navigate.


# ---------------------------------------------------------------------------
# Navigate / Change Folder
# ---------------------------------------------------------------------------


def navigate_to_folder(cfg: Config, renamer: AnimeRenamer) -> None:
    """
    Interactive folder navigation — change the working directory (media_dir).

    Options:
      1. Navigate within BASE_DOWNLOAD_PATH (existing behaviour)
      2. Use current working directory (with confirmation)
      3. Enter a custom path

    Security: the user can only navigate to folders that are inside
    BASE_DOWNLOAD_PATH (option 1). Options 2 and 3 require explicit
    confirmation before proceeding.

    Updates both cfg and the renamer's internal state so that subsequent
    operations work on the new folder.
    """
    from pathlib import Path

    # First, ask how the user wants to select a directory
    nav_options = [
        ("Navigate within BASE_DOWNLOAD_PATH", "base_nav"),
        (f"Use current working directory: {Path.cwd()}", "cwd"),
        ("Enter a custom path", "custom"),
        ("<-- Back to main menu", "back"),
    ]

    nav_result = Picker(
        nav_options,
        title="NAVIGATE: SELECT DIRECTORY METHOD",
        default_index=0,
    ).run()

    if nav_result is None or nav_result[1] == "back":
        return

    nav_choice = nav_result[1]

    if nav_choice == "cwd":
        cwd = Path.cwd()
        print(f"\n  Current working directory: {cwd}")
        confirm = input("  Use this as media directory? (Y/n): ").strip().lower()
        if confirm in ("n", "no"):
            print("  Cancelled.")
            return
        _apply_new_media_dir(cfg, renamer, cwd)
        return

    if nav_choice == "custom":
        custom_path = input("  Enter directory path: ").strip()
        if not custom_path:
            print("  Cancelled.")
            return
        custom = Path(custom_path).resolve()
        if not custom.is_dir():
            print(f"  Directory does not exist: {custom}")
            return
        print(f"\n  Selected directory: {custom}")
        confirm = input("  Use this as media directory? (Y/n): ").strip().lower()
        if confirm in ("n", "no"):
            print("  Cancelled.")
            return
        _apply_new_media_dir(cfg, renamer, custom)
        return

    # nav_choice == "base_nav" — existing navigation within BASE_DOWNLOAD_PATH
    base_path = cfg.base_download_path
    if base_path is None:
        import os

        raw = os.getenv("BASE_DOWNLOAD_PATH", "").strip()
        base_path = Path(raw).resolve() if raw else cfg.media_dir.resolve().parent

    if not base_path.exists():
        print(f"\n  BASE_DOWNLOAD_PATH does not exist: {base_path}")
        print("  Set BASE_DOWNLOAD_PATH in your .env file or use the qBit hook config menu.")
        return

    current = cfg.media_dir.resolve()

    # Verify current is within base_path; if not, start at base_path
    try:
        current.relative_to(base_path)
    except ValueError:
        current = base_path

    while True:
        print(f"\n  Current folder: {current}")
        print(f"  Base path:      {base_path}")
        print()

        # List subdirectories of current folder
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            print("  Permission denied — cannot list this directory.")
            current = current.parent
            continue

        subdirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]

        # Build picker options
        options: list[tuple[str, object]] = []

        # Option to go UP (if not already at base_path)
        if current != base_path:
            options.append((f"  .. (go up to {current.parent.name})", current.parent))

        # Option to stay in current folder and use it
        # Only check for videos directly in this folder (not deep rglob)
        # so that the selected folder is used exclusively
        video_exts = set(cfg.video_extensions)
        has_video = (
            any(f.suffix.lower() in video_exts for f in current.iterdir() if f.is_file())
            if any(e.is_file() for e in entries)
            else False
        )

        stay_marker = "  [contains video files]" if has_video else ""
        options.append((f"  Use this folder: {current.name}{stay_marker}", "use_current"))

        # List subdirectories
        for d in subdirs:
            # Check if it has video files directly inside (shallow check only)
            has_vid = any(f.suffix.lower() in video_exts for f in d.iterdir() if f.is_file())
            # Also check one level deep (Season sub-folders)
            if not has_vid:
                has_vid = any(
                    f.suffix.lower() in video_exts
                    for sub in d.iterdir()
                    if sub.is_dir() and not sub.name.startswith(".")
                    for f in sub.iterdir()
                    if f.is_file()
                )
            marker = "  [video]" if has_vid else ""
            options.append((f"  {d.name}{marker}", d))

        # Back to main menu
        options.append(("<-- Back to main menu", "back"))

        result = Picker(
            options,
            title=f"NAVIGATE  (restricted to {base_path})",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            return

        selected = result[1]

        if selected == "use_current":
            # "Use this folder" was selected — update cfg.media_dir
            break

        if isinstance(selected, Path) and selected == current.parent:
            # Go up — already validated that current != base_path
            current = current.parent
            continue

        if isinstance(selected, Path):
            # Navigate into the selected subdirectory
            selected_resolved = selected.resolve()

            # Security check: must be within base_path
            try:
                selected_resolved.relative_to(base_path)
            except ValueError:
                print(f"\n  ACCESS DENIED: {selected_resolved} is outside BASE_DOWNLOAD_PATH.")
                print(f"  You can only navigate within: {base_path}")
                continue

            current = selected_resolved
            continue

    _apply_new_media_dir(cfg, renamer, current)


def _apply_new_media_dir(cfg: Config, renamer: AnimeRenamer, new_dir: Path) -> None:
    """Apply a new media directory to the config and renamer."""
    old_dir = cfg.media_dir
    cfg.media_dir = new_dir
    print("\n  Working directory changed:")
    print(f"    {old_dir}")
    print(f"    -> {new_dir}")

    # Reset series identity for the new folder
    from renamer.renamer import _clean_folder_name

    cfg.series_name = _clean_folder_name(new_dir.name)
    cfg.tmdb_series_id = None
    cfg.anilist_id = None
    cfg.kitsu_id = None
    cfg.episode_group_id = None
    cfg.season_arc_names = {}

    # Update the renamer's internal state to match the new folder
    renamer._cfg = cfg
    from renamer.history import RenameHistory

    renamer._history = RenameHistory(cfg.history_file, media_dir=cfg.media_dir)

    # Try to load cache for the new folder
    cache = SeriesCache(new_dir)
    cached = cache.load()
    if cached:
        if cached.get("tmdb_series_id"):
            cfg.tmdb_series_id = cached["tmdb_series_id"]
        if cached.get("anilist_id"):
            cfg.anilist_id = cached["anilist_id"]
        if cached.get("kitsu_id"):
            cfg.kitsu_id = cached["kitsu_id"]
        if cached.get("series_name"):
            cfg.series_name = cached["series_name"]
        if cached.get("episode_group_id"):
            cfg.episode_group_id = cached["episode_group_id"]
        if cached.get("episode_title_lang"):
            cfg.episode_title_lang = cached["episode_title_lang"]
        if cached.get("season_arc_names"):
            cfg.season_arc_names = {int(k): v for k, v in cached["season_arc_names"].items()}
        if isinstance(cached.get("provider"), str):
            cfg.provider = Provider.from_str(cached["provider"])
        print(f"  Loaded cache: {cfg.series_name} (TMDB ID: {cfg.tmdb_series_id})")
    else:
        print(f"  Series name from folder: {cfg.series_name}")
        print("  No cache found — auto-search will run on next rename.")


def _set_env_value(key: str, value: str) -> None:
    """Updates or appends a key=value in the root .env file while preserving other lines."""
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    lines = []
    found = False

    if env_path.exists():
        try:
            raw_content = env_path.read_text(encoding="utf-8")
            lines = raw_content.splitlines()
        except OSError:
            pass

    for i, line in enumerate(lines):
        # Match lines starting with KEY= (ignoring leading whitespace/comments)
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break

    if not found:
        # Append to the end of the file
        lines.append(f"{key}={value}")

    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write to .env file: %s", exc)


def configure_qbit_hook(cfg: Config) -> None:
    """Submenu to configure global qBittorrent & hook defaults (saved to .env)."""
    import os

    while True:
        # Read directly from environment/env files or fallback to Config
        qbit_url = os.getenv("QBIT_URL", "http://localhost:8080")
        qbit_user = os.getenv("QBIT_USERNAME", "")
        qbit_pass = os.getenv("QBIT_PASSWORD", "")
        base_path = os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent")

        options = [
            (f"qBittorrent URL      : {qbit_url}", "url"),
            (f"qBittorrent Username : {qbit_user or '(none)'}", "username"),
            (
                f"qBittorrent Password : {'*' * len(qbit_pass) if qbit_pass else '(none)'}",
                "password",
            ),
            (f"Base Download Path   : {base_path}", "base_path"),
            ("Configure Hook Renamer Options (Seasons, Names, Ordering) -->", "renamer_options"),
            ("<-- Back to settings menu", "back"),
        ]

        result = Picker(
            options,
            title="QBITTORRENT & HOOK CONNECTION DEFAULTS (GLOBAL)",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "url":
            val = input(f"New qBittorrent URL [{qbit_url}]: ").strip()
            if val:
                _set_env_value("QBIT_URL", val)
                os.environ["QBIT_URL"] = val
                print(f"  Saved QBIT_URL={val}")
        elif action == "username":
            val = input(f"New Username [{qbit_user}]: ").strip()
            if val:
                _set_env_value("QBIT_USERNAME", val)
                os.environ["QBIT_USERNAME"] = val
                print(f"  Saved QBIT_USERNAME={val}")
        elif action == "password":
            val = input("New Password: ").strip()
            if val:
                _set_env_value("QBIT_PASSWORD", val)
                os.environ["QBIT_PASSWORD"] = val
                print("  Saved QBIT_PASSWORD")
        elif action == "base_path":
            val = input(f"New Base Download Path [{base_path}]: ").strip()
            if val:
                _set_env_value("BASE_DOWNLOAD_PATH", val)
                os.environ["BASE_DOWNLOAD_PATH"] = val
                print(f"  Saved BASE_DOWNLOAD_PATH={val}")
        elif action == "renamer_options":
            configure_hook_defaults(cfg)


def configure_hook_defaults(cfg: Config) -> None:
    """Submenu to configure renamer options (seasons, absolute numbering, name overrides) saved in qbit_hook.json."""
    import json
    from pathlib import Path

    conf_path = Path(__file__).resolve().parent.parent.parent / "qbit_hook.json"

    # Load configuration
    conf = {}
    if conf_path.exists():
        with contextlib.suppress(Exception):
            conf = json.loads(conf_path.read_text(encoding="utf-8"))

    if "global" not in conf:
        conf["global"] = {}
    if "series" not in conf:
        conf["series"] = {}

    def save_conf():
        try:
            conf_path.write_text(json.dumps(conf, indent=2, ensure_ascii=False), encoding="utf-8")
            print("  Hook configuration saved to qbit_hook.json.")
        except OSError as exc:
            print(f"  Could not save configuration: {exc}")

    while True:
        glob = conf["global"]
        provider = glob.get("provider", "tmdb")
        start_mode = glob.get("episode_start_mode", "per_season")
        abs_num = glob.get("absolute_numbering", False)

        options = [
            (f"Global Provider           : {provider}", "provider"),
            (f"Global Episode Start Mode : {start_mode}", "start_mode"),
            (f"Global Absolute Numbering : {abs_num}", "abs_num"),
            ("Configure Series Overrides (Custom Groups, Names, etc.) -->", "series_overrides"),
            ("Rebuild Library Index (folder → ID map for hook matching) -->", "rebuild_index"),
            ("<-- Back", "back"),
        ]

        result = Picker(
            options,
            title="HOOK RENAMER OPTIONS (SAVES TO QBIT_HOOK.JSON)",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "provider":
            p_res = Picker(
                [("TMDB", "tmdb"), ("AniList", "anilist"), ("Kitsu", "kitsu")],
                title="Select Hook Default Provider",
                default_index=["tmdb", "anilist", "kitsu"].index(provider),
            ).run()
            if p_res:
                glob["provider"] = p_res[1]
                save_conf()
        elif action == "start_mode":
            m_res = Picker(
                [
                    ("Per-Season (E01 each season)", "per_season"),
                    ("Continuing (cumulative episode numbers)", "continuing"),
                ],
                title="Select Hook Default Start Mode",
                default_index=0 if start_mode == "per_season" else 1,
            ).run()
            if m_res:
                glob["episode_start_mode"] = m_res[1]
                save_conf()
        elif action == "abs_num":
            a_res = Picker(
                [("Disabled", False), ("Enabled", True)],
                title="Select Hook Default Absolute Numbering Mode",
                default_index=1 if abs_num else 0,
            ).run()
            if a_res:
                glob["absolute_numbering"] = a_res[1]
                save_conf()
        elif action == "series_overrides":
            configure_hook_series_overrides(conf, save_conf)
        elif action == "rebuild_index":
            _rebuild_library_index_menu()


def _rebuild_library_index_menu() -> None:
    """
    Scan BASE_DOWNLOAD_PATH for series folders and rebuild library_index.json
    from their .series_cache.json files.

    Called from the "Hook Renamer Options" sub-menu.
    """
    import os
    from pathlib import Path

    from renamer.library_index import LibraryIndex, reset_library_index

    base_path_str = os.getenv("BASE_DOWNLOAD_PATH", "").strip()
    if not base_path_str:
        print("\n  BASE_DOWNLOAD_PATH is not set — cannot rebuild library index.")
        print("  Set it in .env or via the qBit hook settings.")
        input("  Press Enter to continue …")
        return

    base_path = Path(base_path_str)
    if not base_path.is_dir():
        print(f"\n  BASE_DOWNLOAD_PATH does not exist: {base_path}")
        input("  Press Enter to continue …")
        return

    index_path = Path(__file__).resolve().parent.parent.parent / "library_index.json"
    print(f"\n  Rebuilding library index from: {base_path}")
    print(f"  Index file: {index_path}\n")

    idx = LibraryIndex(index_path)
    count = idx.rebuild(base_path)

    # Reset the global singleton so the hook picks up fresh data
    reset_library_index()

    print(f"\n  Done — {count} series folder(s) indexed.")
    if count == 0:
        print("  (No .series_cache.json files found — folders will be added")
        print("   automatically the next time qbit_hook processes each series.)")

    # Show a summary of what was indexed
    if count > 0:
        print("\n  Indexed folders:")
        for path_str, entry in sorted(idx.entries().items()):
            ids = []
            if entry.get("tmdb_id"):
                ids.append(f"TMDB:{entry['tmdb_id']}")
            if entry.get("anilist_id"):
                ids.append(f"AL:{entry['anilist_id']}")
            if entry.get("kitsu_id"):
                ids.append(f"Kitsu:{entry['kitsu_id']}")
            id_str = "  [" + ", ".join(ids) + "]" if ids else "  [no IDs]"
            print(f"    {entry.get('name', Path(path_str).name)}{id_str}")

    input("\n  Press Enter to continue …")


def configure_hook_series_overrides(conf: dict, save_cb) -> None:
    """Manage series overrides (custom names, episode groups, etc.) in the JSON configuration."""
    while True:
        series_overrides = conf["series"]
        options = []
        for s_name, item in series_overrides.items():
            label = f"{s_name} -> {item.get('series_name') or s_name}"
            options.append((label, s_name))

        options.append(("[Add New Series Override]", "add_new"))
        options.append(("<-- Back", "back"))

        result = Picker(
            options,
            title="SERIES OVERRIDES (SAVES TO QBIT_HOOK.JSON)",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "add_new":
            s_name = input("Enter Torrent Series Title (exact name extracted from title): ").strip()
            if s_name:
                if s_name not in series_overrides:
                    series_overrides[s_name] = {}
                configure_single_series_override(s_name, series_overrides[s_name], save_cb)
                if not series_overrides[s_name]:
                    series_overrides.pop(s_name, None)
                save_cb()
        else:
            configure_single_series_override(action, series_overrides[action], save_cb)
            if not series_overrides[action]:
                series_overrides.pop(action, None)
            save_cb()


def configure_single_series_override(torrent_name: str, item: dict, save_cb) -> None:
    """Configure overrides for a single series (saved in qbit_hook.json)."""
    while True:
        official_name = item.get("series_name") or "(none)"
        ep_group = item.get("episode_group_id") or "(none)"
        start_mode = item.get("episode_start_mode") or "Default"
        abs_num = item.get("absolute_numbering")
        abs_num_str = str(abs_num) if abs_num is not None else "Default"

        options = [
            (f"Official / Cleaned Series Name : {official_name}", "name"),
            (f"Episode Group ID              : {ep_group}", "group_id"),
            (f"Episode Start Mode            : {start_mode}", "start_mode"),
            (f"Absolute Numbering            : {abs_num_str}", "abs_num"),
            ("[Delete this Override]", "delete"),
            ("<-- Back", "back"),
        ]

        result = Picker(
            options,
            title=f"OVERRIDES FOR: {torrent_name}",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "name":
            val = input(f"New Official Name [{official_name}]: ").strip()
            if val:
                item["series_name"] = val
                save_cb()
        elif action == "group_id":
            val = input(f"New Episode Group ID [{ep_group}]: ").strip()
            if val:
                item["episode_group_id"] = val
                save_cb()
        elif action == "start_mode":
            m_res = Picker(
                [("Default", None), ("Per-Season", "per_season"), ("Continuing", "continuing")],
                title="Select Episode Start Mode Override",
                default_index=0,
            ).run()
            if m_res:
                if m_res[1] is None:
                    item.pop("episode_start_mode", None)
                else:
                    item["episode_start_mode"] = m_res[1]
                save_cb()
        elif action == "abs_num":
            a_res = Picker(
                [("Default", None), ("Disabled", False), ("Enabled", True)],
                title="Select Absolute Numbering Override",
                default_index=0,
            ).run()
            if a_res:
                if a_res[1] is None:
                    item.pop("absolute_numbering", None)
                else:
                    item["absolute_numbering"] = a_res[1]
                save_cb()
        elif action == "delete":
            print(f"  Are you sure you want to delete override for {torrent_name}?")
            if input("  Continue? (y/N): ").strip().lower() == "y":
                # Clear the reference and tell the callback to remove empty dictionary from conf["series"]
                item.clear()
                break


# ---------------------------------------------------------------------------
# Folder Icon Management
# ---------------------------------------------------------------------------


def set_icon_current_folder(cfg: Config) -> None:
    """
    Set a folder icon for the current series folder (cfg.media_dir).

    Downloads the poster from the active provider and creates a
    ``.directory`` file so Dolphin / Nautilus / Thunar show it.
    """
    folder = cfg.media_dir
    if not folder.is_dir():
        print(f"\n  Media dir does not exist: {folder}")
        return

    if has_folder_icon(folder):
        print("\n  This folder already has a custom icon.")
        overwrite = input("  Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("  Cancelled.")
            return

    print(f"\n  Fetching poster for: {cfg.series_name}")
    print(f"  Provider: {cfg.provider.value}")

    if set_folder_icon(folder, cfg):
        print("  Folder icon set successfully!")
        print(f"  Icon file: {folder}/.folder_icon.png")
        print(f"  Config:    {folder}/.directory")
    else:
        print(f"  No poster found for '{cfg.series_name}'.")
        print("  Make sure the series is identified (TMDB/AniList/Kitsu ID set).")


def remove_icon_current_folder(cfg: Config) -> None:
    """Remove the custom folder icon from the current series folder."""
    folder = cfg.media_dir
    if not has_folder_icon(folder):
        print(f"\n  No custom icon found for: {folder}")
        return

    if remove_folder_icon(folder):
        print(f"\n  Folder icon removed from: {folder}")
    else:
        print(f"\n  Failed to remove icon from: {folder}")


def batch_set_icons(cfg: Config) -> None:
    """
    Set folder icons for ALL anime subfolders under cfg.media_dir.

    Scans for immediate subfolders, auto-identifies each series,
    and downloads posters.  Uses the same identification logic as
    the multi-series scanner.
    """
    from renamer.cli.multi_series import (
        DiscoveredSeries,
        LibraryDBCache,
        auto_identify_series,
        scan_series_folders,
    )
    from renamer.picker import MultiPicker

    media_dir = cfg.media_dir
    print(f"\n  Scanning for series in: {media_dir}")

    folders = scan_series_folders(media_dir, cfg=cfg)
    if not folders:
        print("  No anime series folders found.")
        return

    print(f"  Found {len(folders)} folder(s). Auto-identifying titles …\n")

    db_cache = LibraryDBCache(media_dir)
    discovered: list[DiscoveredSeries] = []

    for i, folder in enumerate(folders, 1):
        print(f"  [{i}/{len(folders)}] {folder.name} … ", end="", flush=True)
        try:
            series = auto_identify_series(folder, cfg, db_cache)
        except Exception as exc:
            log.warning("Error identifying '%s': %s", folder.name, exc)
            series = DiscoveredSeries(
                folder=folder,
                folder_name=folder.name,
                resolved_name=folder.name,
                provider=cfg.provider,
            )
        discovered.append(series)

        # Show status
        has_icon = has_folder_icon(folder)
        icon_tag = " [icon OK]" if has_icon else ""
        id_tag = ""
        if series.tmdb_id:
            id_tag = f"  [TMDB:{series.tmdb_id}]"
        elif series.anilist_id:
            id_tag = f"  [AL:{series.anilist_id}]"
        elif series.kitsu_id:
            id_tag = f"  [Kitsu:{series.kitsu_id}]"
        else:
            id_tag = "  [ID unknown]"
        print(f"-> {series.resolved_name}{id_tag}{icon_tag}")

    print()

    # Filter: only show series that don't already have icons
    needs_icon = [s for s in discovered if not has_folder_icon(s.folder)]
    already_has = len(discovered) - len(needs_icon)

    if already_has:
        print(f"  {already_has} folder(s) already have icons (skipped).")

    if not needs_icon:
        print("  All folders already have icons!")
        return

    # Present picker for which series to set icons on
    options: list[tuple[str, DiscoveredSeries]] = []
    for s in needs_icon:
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
        title="SELECT FOLDERS TO SET ICONS",
        preselected=list(range(len(options))),  # pre-select all
    ).run()

    if choices is None:
        print("  Cancelled.")
        return

    if not choices:
        print("  No folders selected.")
        return

    selected = [s for _, s in choices]
    print(f"\n  Setting icons for {len(selected)} folder(s) …\n")

    success, failure = 0, 0
    for i, series in enumerate(selected, 1):
        print(f"  [{i}/{len(selected)}] {series.resolved_name} … ", end="", flush=True)
        try:
            series_cfg = _make_series_config_for_icon(series, cfg)
            if set_folder_icon(series.folder, series_cfg):
                print("OK")
                success += 1
            else:
                print("no poster found")
                failure += 1
        except Exception as exc:
            print(f"FAILED: {exc}")
            log.error("Icon setting failed for '%s': %s", series.resolved_name, exc)
            failure += 1

    print(f"\n  Icons set: {success}  |  Failed: {failure}")


def batch_remove_icons(cfg: Config) -> None:
    """Remove custom folder icons from ALL anime subfolders under cfg.media_dir."""
    from renamer.cli.multi_series import scan_series_folders
    from renamer.picker import MultiPicker

    media_dir = cfg.media_dir
    folders = scan_series_folders(media_dir, cfg=cfg)

    # Only show folders that have icons
    with_icons = [f for f in folders if has_folder_icon(f)]
    if not with_icons:
        print("\n  No folders with custom icons found.")
        return

    options: list[tuple[str, Path]] = []
    for folder in with_icons:
        options.append((folder.name, folder))

    choices = MultiPicker(
        options,
        title="SELECT FOLDERS TO REMOVE ICONS",
        preselected=list(range(len(options))),
    ).run()

    if choices is None:
        print("  Cancelled.")
        return

    selected = [f for _, f in choices]
    print(f"\n  Removing icons from {len(selected)} folder(s) …\n")

    for i, folder in enumerate(selected, 1):
        print(f"  [{i}/{len(selected)}] {folder.name} … ", end="", flush=True)
        if remove_folder_icon(folder):
            print("removed")
        else:
            print("failed")

    print("\n  Done.")


def _make_series_config_for_icon(
    series: DiscoveredSeries,
    base_cfg: Config,
) -> Config:
    """Create a Config scoped to a series for icon fetching purposes.

    Reuses the same logic as multi_series._make_series_config to keep DRY.
    """
    from renamer.cli.multi_series import _make_series_config

    return _make_series_config(series, base_cfg)


# ---------------------------------------------------------------------------
# Backup Database Menu
# ---------------------------------------------------------------------------


def _backup_menu_options() -> list[tuple[str, str]]:
    """Return the backup database submenu options."""
    return [
        ("View all records", "backup_view"),
        ("Search records (title / hash / group)", "backup_search"),
        ("Scan folder to database", "backup_scan"),
        ("Restore original filenames  (using hashes)", "backup_restore"),
        ("Add record manually", "backup_add"),
        ("Edit record", "backup_edit"),
        ("Delete record", "backup_delete"),
        ("Export to JSON", "backup_export"),
        ("Import from JSON", "backup_import"),
        ("Show statistics", "backup_stats"),
        ("<-- Back to main menu", "back"),
    ]


def run_backup_submenu(cfg: Config) -> None:
    """Run the Anime Backup Database sub-menu."""
    from renamer.db import AnimeDatabase

    db = AnimeDatabase()

    while True:
        result = Picker(
            _backup_menu_options(),
            title="ANIME BACKUP DATABASE",
            default_index=0,
        ).run()

        if result is None or result[1] == "back":
            break

        action = result[1]
        if action == "backup_view":
            _backup_view(db)
        elif action == "backup_search":
            _backup_search(db)
        elif action == "backup_scan":
            _backup_scan(db, cfg)
        elif action == "backup_restore":
            _backup_restore(db, cfg)
        elif action == "backup_add":
            _backup_add(db, cfg)
        elif action == "backup_edit":
            _backup_edit(db)
        elif action == "backup_delete":
            _backup_delete(db)
        elif action == "backup_export":
            _backup_export(db)
        elif action == "backup_import":
            _backup_import(db)
        elif action == "backup_stats":
            _backup_stats(db)


def _backup_view(db) -> None:
    """View all records with pagination."""
    from renamer.db import PAGE_SIZE, format_size

    total = db.count()
    if total == 0:
        print("\n  Database is empty. Scan a folder or add records manually.")
        return

    offset = 0
    while True:
        records = db.list_all(limit=PAGE_SIZE, offset=offset)
        page = offset // PAGE_SIZE + 1
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

        print(f"\n  {'=' * 60}")
        print(f"  Anime Backup Records ({total} total, page {page}/{total_pages})")
        print(f"  {'=' * 60}")

        for r in records:
            title = r.get("anime_title_rom") or r.get("anime_title_en") or "?"
            group = r.get("group_name") or ""
            en = r.get("anime_title_en") or ""
            s = r.get("season_num")
            e = r.get("episode_num")
            ep_str = f" S{s:02d}E{e:02d}" if s and e else ""

            size_str = format_size(r.get("size_in_bytes"))
            crc = r.get("crc32") or "-"
            md5 = (r.get("md5") or "-")[:12] + "..." if r.get("md5") else "-"
            sha1 = (r.get("sha1") or "-")[:12] + "..." if r.get("sha1") else "-"
            ed2k = (r.get("ed2k") or "-")[:12] + "..." if r.get("ed2k") else "-"
            tmdb = r.get("tmdb_id") or "-"

            print(f"\n  #{r['id']}  {title}{ep_str}  [{group}]")
            if en and en != title:
                print(f"      English : {en}")
            print(f"      File    : {r.get('file_name', '?')}")
            print(f"      Size    : {size_str}  |  TMDB: {tmdb}")
            print(f"      CRC32   : {crc}  |  MD5: {md5}")
            print(f"      SHA1    : {sha1}")
            print(f"      ED2K    : {ed2k}")
            print(f"      Added   : {r.get('date_added', '?')}")

        # Navigation
        print(f"\n  {'-' * 60}")
        nav_options = []
        if offset + PAGE_SIZE < total:
            nav_options.append(("Next page", "next"))
        if offset > 0:
            nav_options.append(("Previous page", "prev"))
        nav_options.append(("Edit a record", "edit"))
        nav_options.append(("Delete a record", "delete"))
        nav_options.append(("<-- Back", "back"))

        nav = Picker(nav_options, title="NAVIGATION", default_index=0).run()
        if nav is None or nav[1] == "back":
            break
        elif nav[1] == "next":
            offset += PAGE_SIZE
        elif nav[1] == "prev":
            offset = max(0, offset - PAGE_SIZE)
        elif nav[1] == "edit":
            _backup_edit(db)
        elif nav[1] == "delete":
            _backup_delete(db)


def _backup_search(db) -> None:
    """Search records."""
    query = input("\n  Search (title, filename, group, hash): ").strip()
    if not query:
        print("  Cancelled.")
        return

    results = db.search(query)
    if not results:
        print(f"  No records found matching '{query}'.")
        return

    print(f"\n  Found {len(results)} record(s):\n")
    for r in results:
        title = r.get("anime_title_rom") or "?"
        s = r.get("season_num")
        e = r.get("episode_num")
        ep_str = f" S{s:02d}E{e:02d}" if s and e else ""
        print(f"  #{r['id']}  {title}{ep_str}  - {r.get('file_name', '?')}")


def _backup_scan(db, cfg: Config) -> None:
    """Scan a folder and add/update records with confirmation."""
    options = [
        (f"Current MEDIA_DIR: {cfg.media_dir}", "media_dir"),
        (f"Current working directory: {Path.cwd()}", "cwd"),
        ("Enter a custom path", "custom"),
        ("<-- Back", "back"),
    ]
    result = Picker(options, title="SCAN FOLDER TO DATABASE", default_index=0).run()
    if result is None or result[1] == "back":
        return

    choice = result[1]
    target_dir = None
    if choice == "media_dir":
        target_dir = cfg.media_dir
    elif choice == "cwd":
        target_dir = Path.cwd()
    elif choice == "custom":
        custom = input("  Enter directory path: ").strip()
        if custom:
            target_dir = Path(custom).resolve()
            if not target_dir.is_dir():
                print(f"  Directory does not exist: {target_dir}")
                return

    if not target_dir:
        return

    print(f"\n  Scanning: {target_dir}")
    print("  This may take a while for large collections...\n")

    scan_result = db.scan_folder(target_dir, cfg.video_extensions, cfg)

    from renamer.db import format_size

    print(f"\n  {'=' * 60}")
    print("  SCAN RESULTS:")
    print(f"    New files to add       : {scan_result.added}")
    print(f"    Existing (complete)    : {scan_result.skipped}  (skipped)")
    print(f"    Existing (needs update): {scan_result.updated}")
    if scan_result.renamed:
        print(f"    Renamed files detected : {scan_result.renamed}  (will update file_name)")
    print(f"    Errors                 : {scan_result.errors}")
    print(f"  {'=' * 60}")

    if scan_result.added == 0 and scan_result.updated == 0 and scan_result.renamed == 0:
        print("\n  Nothing to add or update.")
        return

    # Preview renamed files
    if scan_result.pending_renames:
        print("\n  -- Renamed Files Preview --")
        for record_id, force_fields, existing in scan_result.pending_renames:
            old_name = existing.get("file_name", "?")
            new_name = force_fields.get("file_name", old_name)
            print(f"\n  Record #{record_id}:")
            if "file_name" in force_fields:
                print(f"    file_name: {old_name}")
                print(f"            -> {new_name}")
            for k, v in force_fields.items():
                if k != "file_name":
                    old_val = existing.get(k, "(empty)")
                    print(f"    {k}: {old_val}")
                    print(f"       -> {v}")

    # Preview new records
    if scan_result.pending_inserts:
        print("\n  -- New Records Preview --")
        for rec in scan_result.pending_inserts:
            title = rec.get("anime_title_rom", "?")
            s = rec.get("season_num")
            e = rec.get("episode_num")
            ep_str = f" S{s:02d}E{e:02d}" if s and e else ""
            size_str = format_size(rec.get("size_in_bytes"))
            print(f"\n  NEW  {title}{ep_str}")
            print(f"       File  : {rec.get('file_name', '?')}")
            print(f"       Size  : {size_str}")
            print(f"       CRC32 : {rec.get('crc32', '-')}")
            if rec.get("anime_title_en"):
                print(f"       English : {rec['anime_title_en']}")

    # Preview updates
    if scan_result.pending_updates:
        print("\n  -- Updates Preview --")
        for record_id, fields, existing in scan_result.pending_updates:
            print(f"\n  Record #{record_id}: {existing.get('file_name', '?')}")
            print("    WILL ADD (not overwrite):")
            for k, v in fields.items():
                print(f"      {k}: (empty) -> {v}")

    # Confirm
    print(f"\n  {'-' * 60}")
    confirm = (
        input(
            f"  Apply {scan_result.added} insert(s), "
            f"{scan_result.updated} update(s), and "
            f"{scan_result.renamed} rename(s)? (Y/n): "
        )
        .strip()
        .lower()
    )

    if confirm in ("n", "no"):
        print("  Cancelled - no changes made.")
        return

    # Apply
    inserted, updated = db.apply_scan(scan_result)
    print(f"\n  + {inserted} record(s) inserted")
    print(f"  + {updated} record(s) updated")
    print("  Done!")


def _backup_restore(db, cfg: Config) -> None:
    """Rename files on disk back to their original names using hash-based DB lookup."""
    options = [
        (f"Current MEDIA_DIR: {cfg.media_dir}", "media_dir"),
        (f"Current working directory: {Path.cwd()}", "cwd"),
        ("Enter a custom path", "custom"),
        ("<-- Back", "back"),
    ]
    result = Picker(
        options, title="RESTORE ORIGINAL FILENAMES (USING HASHES)", default_index=0
    ).run()
    if result is None or result[1] == "back":
        return

    choice = result[1]
    target_dir = None
    if choice == "media_dir":
        target_dir = cfg.media_dir
    elif choice == "cwd":
        target_dir = Path.cwd()
    elif choice == "custom":
        custom = input("  Enter directory path: ").strip()
        if custom:
            target_dir = Path(custom).resolve()
            if not target_dir.is_dir():
                print(f"  Directory does not exist: {target_dir}")
                return

    if not target_dir:
        return

    # Ask execution mode
    mode_result = Picker(
        [
            ("Dry Run  (preview only — no files renamed)", "dry"),
            ("Live Rename  (restore files to their original names)", "live"),
            ("Cancel", "cancel"),
        ],
        title="RESTORE EXECUTION MODE",
        default_index=0,
    ).run()

    if mode_result is None or mode_result[1] == "cancel":
        print("  Cancelled.")
        return

    dry_run = mode_result[1] == "dry"

    if not dry_run:
        print("\n  WARNING: This will rename files on disk to match their database names.")
        if input("  Continue? (y/N): ").strip().lower() != "y":
            print("  Cancelled.")
            return

    db.restore_original_names(target_dir, cfg.video_extensions, dry_run=dry_run)


def _backup_add(db, cfg: Config) -> None:
    """Add a record manually."""
    from renamer.db import compute_hashes_fast

    print("\n  === Add Record Manually ===\n")

    anime_title_rom = input("  Romaji title (required): ").strip()
    if not anime_title_rom:
        print("  Cancelled.")
        return

    anime_title_en = input("  English title (Enter to skip): ").strip() or None
    file_name = input("  File name (required): ").strip()
    if not file_name:
        print("  Cancelled.")
        return

    season_input = input("  Season number (Enter to skip): ").strip()
    episode_input = input("  Episode number (Enter to skip): ").strip()
    season_num = int(season_input) if season_input else None
    episode_num = int(episode_input) if episode_input else None

    file_path_input = input("  File path for hashing (Enter to skip): ").strip()
    hashes = {}
    file_size = None
    if file_path_input:
        fp = Path(file_path_input)
        if fp.exists():
            file_size = fp.stat().st_size
            compute = input("  Compute hashes? (Y/n): ").strip().lower()
            if compute not in ("n", "no"):
                hashes = compute_hashes_fast(fp, need_sha1=True, need_ed2k=True)

    group_name = input("  Group name (Enter to skip): ").strip() or None
    tmdb_input = input("  TMDB ID (Enter to skip): ").strip()
    tmdb_id = int(tmdb_input) if tmdb_input else None

    record = {
        "anime_title_en": anime_title_en,
        "anime_title_rom": anime_title_rom,
        "file_name": file_name,
        "season_num": season_num,
        "episode_num": episode_num,
        "size_in_bytes": file_size,
        "group_name": group_name,
        "tmdb_id": tmdb_id,
        **hashes,
    }

    print("\n  -- Record Preview --")
    for k, v in record.items():
        if v is not None:
            print(f"    {k}: {v}")

    confirm = input("\n  Add this record? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        return

    rid = db.add_record(**record)
    print(f"  + Added as record #{rid}")


def _backup_edit(db) -> None:
    """Edit an existing record (force override for explicit user edits)."""
    rid_input = input("\n  Record ID to edit: ").strip()
    if not rid_input:
        print("  Cancelled.")
        return

    try:
        rid = int(rid_input)
    except ValueError:
        print("  Invalid ID.")
        return

    record = db.get_by_id(rid)
    if not record:
        print(f"  Record #{rid} not found.")
        return

    print(f"\n  === Edit Record #{rid} ===\n")

    editable = [
        ("anime_title_en", "English title"),
        ("anime_title_rom", "Romaji title"),
        ("file_name", "File name"),
        ("season_num", "Season"),
        ("episode_num", "Episode"),
        ("group_name", "Group name"),
        ("tmdb_id", "TMDB ID"),
        ("crc32", "CRC32"),
        ("md5", "MD5"),
        ("sha1", "SHA1"),
        ("ed2k", "ED2K"),
    ]

    updates = {}
    for field, label in editable:
        current = record.get(field)
        prompt = f"  {label} [{current or '(empty)'}]: "
        new_val = input(prompt).strip()
        if new_val:
            if field in ("season_num", "episode_num", "tmdb_id", "size_in_bytes"):
                try:
                    new_val = int(new_val)
                except ValueError:
                    print("    Invalid number - not changed.")
                    continue
            updates[field] = new_val

    if not updates:
        print("  No changes.")
        return

    print("\n  -- Changes --")
    for k, v in updates.items():
        old = record.get(k)
        print(f"    {k}: {old} -> {v}")

    confirm = input("\n  Save changes? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        return

    # Use force override — explicit user edits should overwrite
    db.update_record_force(rid, **updates)
    print(f"  + Record #{rid} updated.")


def _backup_delete(db) -> None:
    """Delete a record with confirmation."""
    rid_input = input("\n  Record ID to delete: ").strip()
    if not rid_input:
        print("  Cancelled.")
        return

    try:
        rid = int(rid_input)
    except ValueError:
        print("  Invalid ID.")
        return

    record = db.get_by_id(rid)
    if not record:
        print(f"  Record #{rid} not found.")
        return

    title = record.get("anime_title_rom") or "?"
    fname = record.get("file_name") or "?"
    s = record.get("season_num")
    e = record.get("episode_num")
    ep_str = f" S{s:02d}E{e:02d}" if s and e else ""

    print("\n  WARNING: You are about to delete:")
    print(f"    #{rid}  {title}{ep_str}")
    print(f"    File: {fname}")

    confirm = input("  Confirm DELETE? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Cancelled.")
        return

    if db.delete_record(rid):
        print(f"  + Record #{rid} deleted.")
    else:
        print(f"  Failed to delete record #{rid}.")


def _backup_export(db) -> None:
    """Export records to JSON."""
    total = db.count()
    if total == 0:
        print("\n  Database is empty - nothing to export.")
        return

    options = [
        (f"All records ({total})", "all"),
        ("Filtered by anime title", "by_title"),
        ("Filtered by group name", "by_group"),
        ("<-- Back", "back"),
    ]
    result = Picker(options, title="EXPORT TO JSON", default_index=0).run()
    if result is None or result[1] == "back":
        return

    choice = result[1]
    records = None

    if choice == "by_title":
        title = input("  Anime title: ").strip()
        if not title:
            print("  Cancelled.")
            return
        records = db.get_by_anime_title(title)
        print(f"  Found {len(records)} record(s) for '{title}'.")
    elif choice == "by_group":
        group = input("  Group name: ").strip()
        if not group:
            print("  Cancelled.")
            return
        records = db.search(group)
        records = [r for r in records if r.get("group_name") == group]
        print(f"  Found {len(records)} record(s) for group '{group}'.")

    output = input("  Export path [./anime_backup_export.json]: ").strip()
    if not output:
        output = "./anime_backup_export.json"
    output_path = Path(output).resolve()

    db.export_to_json(output_path, records=records)
    print(f"  + Exported to {output_path}")


def _backup_import(db) -> None:
    """Import records from JSON."""
    import json as _json

    path_input = input("\n  JSON file path: ").strip()
    if not path_input:
        print("  Cancelled.")
        return

    json_path = Path(path_input).resolve()
    if not json_path.exists():
        print(f"  File not found: {json_path}")
        return

    # Preview
    data = _json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    count = len(data)

    print(f"\n  Found {count} record(s) in {json_path.name}")

    confirm = input(f"  Import {count} record(s)? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        return

    added, updated, skipped = db.import_from_json(json_path)
    print(f"\n  + {added} added, {updated} updated, {skipped} skipped")


def _backup_stats(db) -> None:
    """Show database statistics."""
    from renamer.db import format_size

    stats = db.get_statistics()
    total = stats["total"]

    if total == 0:
        print("\n  Database is empty.")
        return

    total_size = format_size(stats["total_size"])

    print(f"\n  {'=' * 50}")
    print("  Database Statistics")
    print(f"  {'=' * 50}")
    print(f"  Total records        : {total}")
    print(f"  Unique series        : {stats['unique_series']}")
    print(f"  Unique groups        : {stats['unique_groups']}")
    print(f"  Total size           : {total_size}")
    print()
    print("  Hash Coverage:")
    print(
        f"    CRC32  : {stats['crc32_count']}/{total}  ({100 * stats['crc32_count'] // max(total, 1)}%)"
    )
    print(
        f"    MD5    : {stats['md5_count']}/{total}  ({100 * stats['md5_count'] // max(total, 1)}%)"
    )
    print(
        f"    SHA1   : {stats['sha1_count']}/{total}  ({100 * stats['sha1_count'] // max(total, 1)}%)"
    )
    print(
        f"    ED2K   : {stats['ed2k_count']}/{total}  ({100 * stats['ed2k_count'] // max(total, 1)}%)"
    )
    print(
        f"    TMDB ID: {stats['tmdb_count']}/{total}  ({100 * stats['tmdb_count'] // max(total, 1)}%)"
    )

    if stats["md5_count"] < total:
        missing_md5 = total - stats["md5_count"]
        print(f"\n  ! {missing_md5} records missing MD5 - run 'Scan folder' to fill")

    if stats["top_groups"]:
        print("\n  Top Groups:")
        for name, cnt in stats["top_groups"]:
            print(f"    {name:30s} : {cnt} files")
    print(f"  {'=' * 50}")
