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
from renamer.cache import SeriesCache
from renamer.icons import (
    has_folder_icon,
    remove_folder_icon,
    set_folder_icon,
    set_folder_icon_batch,
)

log = get_logger()


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
    )


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
        _save_cache(cfg)


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
        print(
            f"  Using: {selected.name} (absolute numbering auto-enabled)"
        )
    else:
        print(f"  Using: {selected.name}")
    _save_cache(cfg)


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
        print(
            "  Switched to per-season mode.\n"
            "  Each season starts at E01."
        )


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
    from pathlib import Path

    while True:
        # Read directly from environment/env files or fallback to Config
        qbit_url = os.getenv("QBIT_URL", "http://localhost:8080")
        qbit_user = os.getenv("QBIT_USERNAME", "")
        qbit_pass = os.getenv("QBIT_PASSWORD", "")
        base_path = os.getenv("BASE_DOWNLOAD_PATH", "/mnt/D/Torrent")

        options = [
            (f"qBittorrent URL      : {qbit_url}", "url"),
            (f"qBittorrent Username : {qbit_user or '(none)'}", "username"),
            (f"qBittorrent Password : {'*' * len(qbit_pass) if qbit_pass else '(none)'}", "password"),
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
        try:
            conf = json.loads(conf_path.read_text(encoding="utf-8"))
        except Exception:
            pass
            
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
            (f"Configure Series Overrides (Custom Groups, Names, etc.) -->", "series_overrides"),
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
                [("Per-Season (E01 each season)", "per_season"), ("Continuing (cumulative episode numbers)", "continuing")],
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
        print(f"\n  This folder already has a custom icon.")
        overwrite = input("  Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("  Cancelled.")
            return

    print(f"\n  Fetching poster for: {cfg.series_name}")
    print(f"  Provider: {cfg.provider.value}")

    if set_folder_icon(folder, cfg):
        print(f"  Folder icon set successfully!")
        print(f"  Icon file: {folder}/.folder_icon.png")
        print(f"  Config:    {folder}/.directory")
    else:
        print(f"  No poster found for '{cfg.series_name}'.")
        print(f"  Make sure the series is identified (TMDB/AniList/Kitsu ID set).")


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

    folders = scan_series_folders(media_dir)
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
    folders = scan_series_folders(media_dir)

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

    print(f"\n  Done.")


def _make_series_config_for_icon(
    series: "DiscoveredSeries",
    base_cfg: Config,
) -> Config:
    """Create a Config scoped to a series for icon fetching purposes.

    Reuses the same logic as multi_series._make_series_config to keep DRY.
    """
    from renamer.cli.multi_series import _make_series_config
    return _make_series_config(series, base_cfg)
