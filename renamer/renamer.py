"""
renamer.renamer
===============
The main renaming engine — ``AnimeRenamer``.

This module is intentionally free of I/O beyond file operations and logging.
All interactive prompts live in ``jellyfin_renamer.py``.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from renamer.providers.anidb import AniDBFetcher
    from renamer.providers.anidb_cache import AniDBFileInfo

from renamer.cache import SeriesCache
from renamer.config import (
    START_MODE_CONTINUING,
    Config,
    Provider,
    get_logger,
)
from renamer.history import RenameHistory
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.providers.base import EpisodeGroupInfo, EpisodeInfo, RenameResult
from renamer.providers.registry import get_registry
from renamer.subtitles import process_subtitles_for_video

log = get_logger(__name__)

# Characters illegal in Windows filenames and/or confusing for Jellyfin
_ILLEGAL_CHARS = re.compile(r'[\\/*?:"<>|;]')
# Unicode whitespace variants that confuse Jellyfin
_UNICODE_SPACE = re.compile(r"[\u00a0\u2000-\u200b\u2028\u2029\u3000]")
# Unicode dashes → plain hyphen
_UNICODE_DASHES = str.maketrans(
    {
        "\u2014": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u2012": "-",  # figure dash
        "\u2015": "-",  # horizontal bar
        "\u2026": "...",  # ellipsis
    }
)
# Codec/resolution tags inside brackets
_CODEC_BRACKET = re.compile(
    r"\[(?:[^\]]*?(?:1080p|720p|480p|HEVC|x264|x265|AV1|WEB|BD|DVD|"
    r"\d+bit|[A-Z]{2,5}-(?:RIP|release)))\]",
    re.IGNORECASE,
)
_CODEC_LOOSE = re.compile(r"\b(?:1080p|720p|480p|HEVC|x264|x265|AV1)\b", re.IGNORECASE)

# Pattern to clean folder names into searchable titles
_FOLDER_GROUP_TAG = re.compile(r"^\[[^\]]+\]\s*")
_FOLDER_YEAR_TAG = re.compile(r"\s*\(\d{4}\)\s*$")
_FOLDER_BRACKET_TAG = re.compile(r"\s*\[[^\]]+\]\s*$")
# Season tag: (Season 01), (Season 1), (S01), etc.
_FOLDER_SEASON_TAG = re.compile(
    r"\s*\(\s*Season\s+\d+\s*\)",
    re.IGNORECASE,
)
# English/alternate title in parentheses at the end of the Japanese/romaji name
# Matches: "Name (English Name Here)" but NOT "Name (2002)" or "Name (Season 01)"
_FOLDER_ALT_TITLE_PAREN = re.compile(r"\s*\(([^)]+)\)\s*$")
# Codec/resolution tags inside brackets anywhere in the name
_FOLDER_CODEC_BRACKET = re.compile(
    r"\s*\[(?:"
    r"[^\]]*?(?:1080p|720p|480p|2160p|4K|HEVC|x264|x265|AV1|10bit|8bit|Hi10|"
    r"WEB|BD|DVD|Multi-Sub|Dual-Audio|Batch|Complete)"
    r")[^\]]*\]",
    re.IGNORECASE,
)
# Language / source tags inside brackets: [JPN], [ENG], [www], [v2], etc.
# These are typically short ALL-CAPS or lowercase tags that are NOT the title.
_FOLDER_LANG_BRACKET = re.compile(
    r"\s*\[(?:"
    r"[A-Z]{2,4}"  # 2-4 letter uppercase codes: JPN, ENG, CHS, CHT, RAW
    r"|www"  # www source tag
    r"|v\d+"  # version tags: v2, v3
    r")\s*\]",
    re.IGNORECASE,
)
# Catch-all for remaining trailing bracket tags that are clearly NOT part of
# the title — short tags, resolution tags, codec fragments.
_FOLDER_JUNK_BRACKET = re.compile(
    r"\s*\[(?:"
    r"\d{3,4}x\d{3,4}"  # resolution: 1920x1080
    r"|[A-Z]{2,5}"  # short uppercase: JPN, ENG, WWW, AAC, FLAC
    r"|\d+bit"  # 10bit, 8bit
    r"|v\d+"  # v2
    r"|(?:Hi)?10"  # Hi10, 10
    r")\s*\]",
    re.IGNORECASE,
)


class AnimeRenamer:
    """
    Main renaming engine.

    Used by ``jellyfin_renamer.py`` (interactive CLI) and ``qbit_hook.py``
    (automated post-download hook).

    Parameters
    ----------
    cfg:
        Runtime configuration.
    rename_via_qbit:
        Optional callable that moves files through the qBittorrent API so
        that qBit keeps seeding state intact after a rename.
        Signature: ``(old_path: Path, new_path: Path, new_name: str) -> None``
    """

    def __init__(
        self,
        cfg: Config,
        rename_via_qbit: Callable[[Path, Path, str], None] | None = None,
        rename_folder_via_qbit: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._history = RenameHistory(cfg.history_file, media_dir=cfg.media_dir)
        self._ep_parser = EpisodeNumberParser()
        self._sp_parser = SpecialParser()
        self._rename_via_qbit = rename_via_qbit
        self._rename_folder_via_qbit = rename_folder_via_qbit

    # ── Public API ────────────────────────────────────────────

    def run(self, dry_run: bool = True) -> list[RenameResult]:
        """
        Scan ``cfg.media_dir``, resolve episode metadata, and rename files.

        Returns a list of :class:`RenameResult` objects (one per video file).
        When *dry_run* is ``True`` no files are modified.
        """
        # ── Safety Check: prevent running directly on root download folder ──
        if (
            self._cfg.base_download_path
            and self._cfg.media_dir.resolve() == self._cfg.base_download_path.resolve()
        ):
            log.error(
                "CRITICAL: MEDIA_DIR is set to your base torrent download path: %s. "
                "Running the renamer recursively on the root folder will merge all subfolders! Aborting for safety.",
                self._cfg.media_dir,
            )
            return []

        # ── Security: check sensitive file permissions ──
        try:
            from renamer.security import check_sensitive_files

            warnings = check_sensitive_files(Path(__file__).resolve().parent.parent)
            if warnings:
                for w in warnings:
                    log.warning("Security: %s", w)
        except ImportError:
            pass

        self._load_cache()

        # ── Resolve series name from folder BEFORE anything else ──
        # This ensures a clean title is available for LocalFetcher fallback
        # even when no provider API key or series ID is configured.
        if not self._cfg.series_name:
            self._cfg.series_name = _clean_folder_name(self._cfg.media_dir.resolve().name)
        else:
            # Even if series_name was set (e.g. from env), clean it if it
            # looks like a raw folder name with sub-group tags etc.
            cleaned = _clean_folder_name(self._cfg.series_name)
            if cleaned != self._cfg.series_name:
                log.info(
                    "Cleaned series name: %r -> %r",
                    self._cfg.series_name,
                    cleaned,
                )
                self._cfg.series_name = cleaned

        # ── Validate (non-fatal for missing API key — LocalFetcher fallback) ──
        errors = self._cfg.validate()
        fatal_errors = [e for e in errors if not e.startswith("TMDB_API_KEY")]
        if fatal_errors:
            for e in fatal_errors:
                log.error("Config error: %s", e)
            return []
        if errors:
            # Only API-key warnings — can proceed with LocalFetcher
            for e in errors:
                log.warning("Config warning: %s (will use local mode)", e)

        # ── Try to resolve via provider APIs ──
        self._auto_search_series()

        registry = get_registry()
        fetcher = None
        try:
            fetcher = registry.create_fetcher(self._cfg.provider, self._cfg)
        except LookupError as exc:
            log.error("Provider error: %s", exc)
            log.error(
                "Fix: set PROVIDER=tmdb (or anilist / kitsu) in your .env file, "
                "or use option 5 in the menu to switch provider."
            )
            return []
        except ValueError as exc:
            log.warning("Provider lookup failed: %s", exc)
            log.warning(
                "Falling back to local mode — using folder name %r "
                "as series title with episode numbers from filenames.",
                self._cfg.series_name,
            )
            from renamer.providers.local import LocalFetcher

            fetcher = LocalFetcher(self._cfg)

        if fetcher is None:
            log.error("No provider available — aborting.")
            return []

        episode_map = fetcher.fetch()
        specials_map = fetcher.fetch_specials()

        if not episode_map:
            log.error("Could not build episode map — aborting.")
            return []

        # ── Extract original season/episode map and arc names from TMDB episode groups ──
        # These allow matching files named with original TMDB numbering (e.g. S01E1100)
        # when using an episode group that reorganizes seasons.
        original_se_map: dict[tuple[int, int], EpisodeInfo] = {}
        if hasattr(fetcher, "_original_se_map") and fetcher._original_se_map:
            original_se_map = fetcher._original_se_map
            log.info(
                "Episode group: built original season/episode map with %d entries "
                "for fallback file matching.",
                len(original_se_map),
            )

        # Auto-populate season arc names from TMDB episode group names
        if hasattr(fetcher, "_group_arc_names") and fetcher._group_arc_names:
            if not self._cfg.season_arc_names:
                self._cfg.season_arc_names = fetcher._group_arc_names.copy()
                log.info(
                    "Auto-populated %d season arc name(s) from episode group.",
                    len(self._cfg.season_arc_names),
                )
            else:
                # Merge: fill in any missing arc names from the group
                for s, name in fetcher._group_arc_names.items():
                    if s not in self._cfg.season_arc_names:
                        self._cfg.season_arc_names[s] = name
                log.info(
                    "Merged arc names: %d total (%d from episode group).",
                    len(self._cfg.season_arc_names),
                    len(fetcher._group_arc_names),
                )

        self._save_cache()
        self._cross_reference_ids()

        return self._process_files(episode_map, specials_map, dry_run, original_se_map)

    def undo(self) -> None:
        """Revert the most recent rename session interactively."""
        history = self._history.load()
        if not history:
            log.info("No rename history found — nothing to undo.")
            return

        log.info("Found %d entries in rename history.", len(history))
        confirm = input("  Revert all to originals? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

        # Separate folder renames from file renames.
        # Folder renames must be processed LAST (after files are restored).
        file_entries: dict[str, str] = {}
        folder_entries: dict[str, str] = {}
        for new_path_str, orig_path_str in history.items():
            new_path = Path(new_path_str)
            # If the original path is a directory (not a file with extension),
            # treat it as a folder rename.
            if not new_path.suffix and not Path(orig_path_str).suffix:
                folder_entries[new_path_str] = orig_path_str
            else:
                file_entries[new_path_str] = orig_path_str

        restored, skipped_count, restored_keys = 0, 0, []

        # 1. Restore files first
        for new_path_str, orig_path_str in file_entries.items():
            src = Path(new_path_str)
            dest = Path(orig_path_str)
            if src.exists():
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dest)
                    log.info("Restored: %s -> %s", src, dest)
                    restored_keys.append(new_path_str)
                    restored += 1
                except OSError as exc:
                    log.error("Could not revert %r: %s", src, exc)
            else:
                log.warning("Not found (skipped): %s", src)
                skipped_count += 1

        # 2. Restore folders last (reverse order)
        for new_path_str, orig_path_str in reversed(list(folder_entries.items())):
            src = Path(new_path_str)
            dest = Path(orig_path_str)
            if src.exists() and src.is_dir():
                try:
                    src.rename(dest)
                    log.info("Restored folder: %s -> %s", src, dest)
                    restored_keys.append(new_path_str)
                    restored += 1
                except OSError as exc:
                    log.error("Could not revert folder %r: %s", src, exc)
            elif not src.exists():
                log.warning("Folder not found (skipped): %s", src)
                skipped_count += 1

        self._history.clear_entries(restored_keys)
        log.info("Restored %d item(s). Skipped %d.", restored, skipped_count)

    def list_episode_groups(self) -> list[EpisodeGroupInfo]:
        """List available TMDB episode groups (TMDB provider only)."""
        if self._cfg.provider != Provider.TMDB:
            log.warning("Episode groups are TMDB-only. Current: %s", self._cfg.provider.value)
            return []
        if not self._cfg.tmdb_api_key or not self._cfg.tmdb_series_id:
            log.error("TMDB API key and series ID are required for episode groups.")
            return []
        from renamer.providers.tmdb import TMDBFetcher

        return TMDBFetcher(
            self._cfg.tmdb_api_key, self._cfg.tmdb_series_id, self._cfg
        ).fetch_episode_groups()

    def list_alternative_titles(self) -> list[dict]:
        """List TMDB alternative titles (TMDB provider only)."""
        if self._cfg.provider != Provider.TMDB:
            log.warning("Alternative titles are TMDB-only. Current: %s", self._cfg.provider.value)
            return []
        if not self._cfg.tmdb_api_key or not self._cfg.tmdb_series_id:
            log.error("TMDB API key and series ID are required.")
            return []
        from renamer.providers.tmdb import TMDBFetcher

        return TMDBFetcher(
            self._cfg.tmdb_api_key, self._cfg.tmdb_series_id, self._cfg
        ).fetch_alternative_titles()

    # ── Cache helpers ─────────────────────────────────────────

    # ── Hash-based lookup (AniDB ED2K) ──────────────────────────

    _anidb_fetcher: AniDBFetcher | None = None

    def _lookup_by_hash(self, path: Path) -> AniDBFileInfo | None:
        """
        Try to identify a file by its ED2K hash via AniDB.

        This is used as a fallback when filename parsing fails.
        Only active when cfg.use_hash is True or provider is AniDB.
        """
        try:
            from renamer.providers.anidb import AniDBFetcher
        except ImportError:
            log.debug("AniDB provider not available — skipping hash lookup")
            return None

        if self._anidb_fetcher is None:
            self._anidb_fetcher = AniDBFetcher(self._cfg)

        try:
            return self._anidb_fetcher.lookup_single_file(path)
        except Exception as exc:
            log.warning("Hash lookup failed for %s: %s", path.name, exc)
            return None

    def _load_cache(self) -> None:
        cache = SeriesCache(self._cfg.media_dir)
        data = cache.load()
        if not data:
            return
        log.info("Loaded series cache: %s", data.get("series_name"))
        cfg = self._cfg
        if not cfg.tmdb_series_id and data.get("tmdb_series_id"):
            cfg.tmdb_series_id = data["tmdb_series_id"]
        if not cfg.anilist_id and data.get("anilist_id"):
            cfg.anilist_id = data["anilist_id"]
        if not cfg.kitsu_id and data.get("kitsu_id"):
            cfg.kitsu_id = data["kitsu_id"]
        if not cfg.series_name and data.get("series_name"):
            cfg.series_name = data["series_name"]
        # Cache may store provider as a Provider enum or a plain string
        raw_provider = data.get("provider")
        if isinstance(raw_provider, Provider):
            cfg.provider = raw_provider
        elif isinstance(raw_provider, str) and raw_provider:
            cfg.provider = Provider.from_str(raw_provider)
        # Never let provider become None
        if cfg.provider is None:  # type: ignore[comparison-overlap]
            cfg.provider = Provider.TMDB  # type: ignore[assignment]
        if not cfg.episode_group_id and data.get("episode_group_id"):
            cfg.episode_group_id = data["episode_group_id"]
        if data.get("episode_start_mode"):
            cfg.episode_start_mode = data["episode_start_mode"]
        if data.get("episode_title_lang"):
            cfg.episode_title_lang = data["episode_title_lang"]
        if data.get("season_arc_names"):
            cfg.season_arc_names = {int(k): v for k, v in data["season_arc_names"].items()}

    def _save_cache(self) -> None:
        cfg = self._cfg
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

    # ── Auto-search ───────────────────────────────────────────

    def _auto_search_series(self) -> None:
        """
        Search for a series ID if the active provider needs one but
        none is set.

        Flow:
          1. Search with the cleaned folder name (romaji part)
          2. If exactly one result, use it automatically
          3. If multiple results, show an interactive picker so the
             user can choose the correct series
          4. If no results, retry with the English/alternate title
             from the folder name (if available)
          5. If still no results, try extracting a name from the
             files inside the folder
          6. If all attempts fail, fall back to LocalFetcher

        This ensures that any process needing a TMDB ID searches for
        it first — no need to run a dry run just to get the ID.
        """
        name = self._cfg.series_name
        if not name:
            return

        registry = get_registry()
        cfg = self._cfg

        if cfg.provider == Provider.TMDB and not cfg.tmdb_series_id:
            if not cfg.tmdb_api_key:
                log.info(
                    "No TMDB API key — skipping auto-search. "
                    "Will use folder name %r as series title.",
                    name,
                )
                return

            # Search attempts: list of (query, label) to try in order
            search_queries = [(name, "cleaned folder name")]

            # Try English/alternate title from folder name as fallback
            raw_folder = cfg.media_dir.resolve().name
            alt_title = _extract_alt_title_from_folder(raw_folder)
            if alt_title and alt_title != name:
                search_queries.append((alt_title, "English/alternate title"))

            # Try extracting name from files inside the folder
            file_name = _extract_name_from_files(cfg.media_dir)
            if file_name and file_name != name and file_name != alt_title:
                search_queries.append((file_name, "filename extraction"))

            for query, label in search_queries:
                log.info("Auto-searching TMDB with %s: %r …", label, query)
                results = registry.search_multi(Provider.TMDB, query, cfg, limit=10)

                if not results:
                    log.info("No TMDB results for %s: %r", label, query)
                    continue

                if len(results) == 1:
                    # Single result — use it automatically
                    r = results[0]
                    cfg.tmdb_series_id = r["id"]
                    cfg.series_name = r.get("romaji") or r.get("name") or name
                    log.info(
                        "Auto-found: %r (TMDB ID %d)",
                        cfg.series_name,
                        cfg.tmdb_series_id,
                    )
                    return

                # Multiple results — show interactive picker
                selected = self._pick_series_from_results(results, query)
                if selected:
                    cfg.tmdb_series_id = selected["id"]
                    cfg.series_name = selected.get("romaji") or selected.get("name") or name
                    log.info(
                        "Selected: %r (TMDB ID %d)",
                        cfg.series_name,
                        cfg.tmdb_series_id,
                    )
                    return

                # User cancelled picker — try next query
                log.info("Picker cancelled for query %r — trying next.", query)
                continue

            # All queries exhausted — offer interactive search
            log.warning(
                "TMDB auto-search found no match for %r "
                "(also tried alternate title and filename extraction).",
                name,
            )
            self._interactive_search_fallback()

        elif cfg.provider == Provider.AniList and not cfg.anilist_id:
            log.info("Auto-searching AniList for %r …", name)
            try:
                results = registry.search_multi(Provider.AniList, name, cfg, limit=10)
                if not results:
                    log.info("No AniList results for %r", name)
                elif len(results) == 1:
                    r = results[0]
                    cfg.anilist_id = r["id"]
                    cfg.series_name = r.get("romaji") or r.get("name") or name
                    log.info("Auto-found: %r (AniList ID %d)", cfg.series_name, cfg.anilist_id)
                else:
                    # Multiple results — reuse the provider-agnostic picker
                    selected = self._pick_series_from_results(results, name)
                    if selected:
                        cfg.anilist_id = selected["id"]
                        cfg.series_name = selected.get("romaji") or selected.get("name") or name
                        log.info(
                            "Selected: %r (AniList ID %d)",
                            cfg.series_name,
                            cfg.anilist_id,
                        )
            except Exception as exc:
                log.warning("AniList auto-search failed: %s", exc)

        elif cfg.provider == Provider.Kitsu and not cfg.kitsu_id:
            log.info("Auto-searching Kitsu for %r …", name)
            try:
                from renamer.providers.kitsu import KitsuFetcher

                result = KitsuFetcher(None, cfg).find_series(name)
                if result:
                    cfg.kitsu_id, romaji = result
                    cfg.series_name = romaji
                    log.info("Auto-found: %r (Kitsu ID %d)", cfg.series_name, cfg.kitsu_id)
            except Exception as exc:
                log.warning("Kitsu auto-search failed: %s", exc)

    @staticmethod
    def _pick_series_from_results(
        results: list[dict],
        query: str,
    ) -> dict | None:
        """
        Present an interactive Picker for the user to choose from
        multiple TMDB search results.

        Returns the selected result dict, or ``None`` if cancelled.
        """
        from renamer.picker import Picker

        options: list[tuple[str, dict]] = []
        for r in results:
            name = r.get("name", "")
            romaji = r.get("romaji", "")
            orig = r.get("original_name", "")
            year = r.get("first_air_date", "")[:4]
            overview = r.get("overview", "")
            country = ", ".join(r.get("origin_country", []))

            # Build a descriptive label
            parts: list[str] = []
            if romaji and romaji != name:
                parts.append(romaji)
            if name:
                parts.append(name)
            if orig and orig != name and orig != romaji:
                parts.append(f"({orig})")
            label = " / ".join(parts)

            if year:
                label += f"  [{year}]"
            if country:
                label += f"  [{country}]"
            if overview:
                label += f"  — {overview[:80]}{'…' if len(overview) > 80 else ''}"

            options.append((label, r))

        if not options:
            return None

        result = Picker(
            options,
            title=f"MULTIPLE MATCHES FOR: {query}",
            default_index=0,
        ).run()

        if result is None:
            return None

        return result[1]

    def _cross_reference_ids(self) -> None:
        """Best-effort: fill in a missing Kitsu ID from AniList data."""
        cfg = self._cfg
        if cfg.anilist_id and not cfg.kitsu_id:
            try:
                from renamer.providers.kitsu import KitsuFetcher

                result = KitsuFetcher(None, cfg).find_series(cfg.series_name)
                if result:
                    cfg.kitsu_id = result[0]
                    log.info("Cross-ref: found Kitsu ID %d", cfg.kitsu_id)
            except Exception:
                pass

    def _interactive_search_fallback(self) -> None:
        """
        When all auto-search queries fail, present an interactive
        menu so the user can manually search TMDB, use local mode,
        or cancel.

        This is called from ``_auto_search_series`` when no TMDB
        match is found for any query.
        """
        from renamer.picker import Picker

        cfg = self._cfg
        name = cfg.series_name

        while True:
            options = [
                (f"Search TMDB with custom query (current: '{name}')", "search"),
                (f"Use as-is: '{name}' (local mode — no TMDB episode data)", "local"),
                ("Cancel (abort rename)", "cancel"),
            ]

            result = Picker(
                options,
                title=f"AUTO-SEARCH FAILED FOR: {name}",
                default_index=0,
            ).run()

            if result is None or result[1] == "cancel":
                return

            action = result[1]

            if action == "local":
                # Keep using the cleaned folder name, will fall back
                # to LocalFetcher in run()
                log.info("Using local mode with folder name %r.", name)
                return

            if action == "search":
                custom_query = input(f"  Enter search query [{name}]: ").strip()
                if not custom_query:
                    custom_query = name

                registry = get_registry()
                results = registry.search_multi(Provider.TMDB, custom_query, cfg, limit=10)

                if not results:
                    print(f"  No TMDB results for '{custom_query}'. Try another query.")
                    continue

                if len(results) == 1:
                    r = results[0]
                    cfg.tmdb_series_id = r["id"]
                    cfg.series_name = r.get("romaji") or r.get("name") or name
                    log.info(
                        "Found: %r (TMDB ID %d)",
                        cfg.series_name,
                        cfg.tmdb_series_id,
                    )
                    return

                # Multiple results — show picker
                selected = self._pick_series_from_results(results, custom_query)
                if selected:
                    cfg.tmdb_series_id = selected["id"]
                    cfg.series_name = selected.get("romaji") or selected.get("name") or name
                    log.info(
                        "Selected: %r (TMDB ID %d)",
                        cfg.series_name,
                        cfg.tmdb_series_id,
                    )
                    return

                # User cancelled picker
                print("  Search cancelled. Try another query or use local mode.")
                continue

    # ── File processing ───────────────────────────────────────

    def _process_files(
        self,
        episode_map: dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        dry_run: bool,
        original_se_map: dict[tuple[int, int], EpisodeInfo] | None = None,
    ) -> list[RenameResult]:
        cfg = self._cfg
        files = sorted(p for p in cfg.media_dir.rglob("*") if p.is_file())
        results: list[RenameResult] = []
        session_history: dict[str, str] = {}
        # Track claimed destination paths to prevent overwriting
        claimed_dests: set[Path] = set()

        mode = "DRY RUN — no files changed" if dry_run else "LIVE"
        log.info("Scanning: %s | %s", cfg.media_dir, mode)

        season_ep_map = {(ep.season, ep.episode): ep for ep in episode_map.values()}
        season_offsets = (
            self._compute_season_offsets(episode_map)
            if cfg.episode_start_mode == START_MODE_CONTINUING
            else {}
        )
        next_auto_special = max(specials_map.keys(), default=0) + 1

        for path in files:
            if path.suffix.lower() not in cfg.video_extensions:
                continue

            result, next_auto_special = self._classify_and_handle(
                path=path,
                episode_map=episode_map,
                specials_map=specials_map,
                season_ep_map=season_ep_map,
                season_offsets=season_offsets,
                next_auto_special=next_auto_special,
                dry_run=dry_run,
                session_history=session_history,
                claimed_dests=claimed_dests,
                original_se_map=original_se_map or {},
            )
            results.append(result)

        done = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if r.error)

        # Save file-rename history BEFORE the folder rename,
        # because the history file path depends on cfg.media_dir
        # which will change after the folder is renamed.
        if not dry_run and session_history:
            self._history.save(session_history)

        # ── Rename the containing folder to match the series name ──
        folder_renamed = False
        if done > 0 or skipped > 0:
            folder_renamed = self._rename_folder(dry_run, session_history)

        # If folder was renamed, also save the folder-rename to the
        # history file at the NEW location (so undo works correctly)
        if folder_renamed and not dry_run:
            new_history = RenameHistory(cfg.history_file, media_dir=cfg.media_dir)
            new_history.save(session_history)

        tag = "Would process" if dry_run else "Processed"
        log.info("%s: %d | Skipped: %d | Errors: %d", tag, done, skipped, failed)
        if folder_renamed:
            log.info("Folder renamed to: %s", cfg.media_dir.name)
        if not dry_run and session_history:
            log.info("Undo log: %s", cfg.history_file)

        return results

    def _classify_and_handle(
        self,
        path: Path,
        episode_map: dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        season_ep_map: dict[tuple[int, int], EpisodeInfo],
        season_offsets: dict[int, int],
        next_auto_special: int,
        dry_run: bool,
        session_history: dict[str, str],
        claimed_dests: set[Path],
        original_se_map: dict[tuple[int, int], EpisodeInfo] | None = None,
    ) -> tuple[RenameResult, int]:
        """
        Classify *path* and return a (RenameResult, next_auto_special) pair.

        Extracted from ``_process_files`` to keep nesting depth manageable.
        """

        # ── Special detection ─────────────────────────────────
        sp_num = self._sp_parser.parse(path.name)
        if sp_num is not None:
            if sp_num == 0:
                sp_num = next_auto_special
                next_auto_special += 1
            info = specials_map.get(sp_num)

            # Title similarity validation/override for specials
            suffix = _extract_title_suffix(path.name)
            if suffix:
                matches = _find_all_title_matches(suffix, specials_map)
                title_info = self._select_best_candidate(
                    matches, path, season_offsets, claimed_dests, sp_num
                )
                if title_info:
                    if info and info != title_info:
                        log.info(
                            "Special %d title mismatch (numeric matched: %r, title matched: %r) — overriding with title match",
                            sp_num,
                            info.title,
                            title_info.title,
                        )
                    info = title_info

            if not info:
                info = EpisodeInfo(
                    absolute=sp_num,
                    season=0,
                    episode=sp_num,
                    title=_clean_special_title(path.stem),
                    source="filename",
                    is_special=True,
                )
            return (
                self._handle_file(
                    path, info, dry_run, season_offsets, session_history, claimed_dests
                ),
                next_auto_special,
            )

        # ── SxxExx detection ─────────────────────────────────
        season_num, ep_num = self._ep_parser.parse_season_episode(path.name)
        if season_num is not None and ep_num is not None:
            if season_num == 0:
                # S00Exx — look up in specials map
                info = specials_map.get(ep_num)

                # Title similarity validation/override for specials
                suffix = _extract_title_suffix(path.name)
                if suffix:
                    matches = _find_all_title_matches(suffix, specials_map)
                    title_info = self._select_best_candidate(
                        matches, path, season_offsets, claimed_dests, ep_num
                    )
                    if title_info:
                        if info and info != title_info:
                            log.info(
                                "S00E%02d title mismatch (numeric matched: %r, title matched: %r) — overriding with title match",
                                ep_num,
                                info.title,
                                title_info.title,
                            )
                        info = title_info

                if not info:
                    info = EpisodeInfo(
                        absolute=ep_num,
                        season=0,
                        episode=ep_num,
                        title=_clean_special_title(path.stem),
                        source="filename",
                        is_special=True,
                    )
                return (
                    self._handle_file(
                        path, info, dry_run, season_offsets, session_history, claimed_dests
                    ),
                    next_auto_special,
                )

            info = season_ep_map.get((season_num, ep_num))
            if not info:
                # ── Fallback: try matching by original TMDB season/episode ──
                # When using a TMDB episode group, the group reorganizes episodes
                # into new seasons. Files may still be named with the original
                # TMDB numbering (e.g. S01E1100 for One Piece), so we try to
                # match against the original season/episode mapping.
                if original_se_map:
                    info = original_se_map.get((season_num, ep_num))
                    if info:
                        log.info(
                            "S%02dE%02d matched via original TMDB numbering -> S%02dE%02d (%s)",
                            season_num,
                            ep_num,
                            info.season,
                            info.episode,
                            info.title[:50] if info.title else "",
                        )
                if not info:
                    # ── Fallback: try the episode number as an absolute number ──
                    # Some files use S01EXXXX where XXXX is the absolute episode
                    # number in the series, not a per-season episode number.
                    info = episode_map.get(ep_num)
                    if info:
                        log.info(
                            "S%02dE%02d matched via absolute episode number -> S%02dE%02d (%s)",
                            season_num,
                            ep_num,
                            info.season,
                            info.episode,
                            info.title[:50] if info.title else "",
                        )

            # Title similarity validation/override for SxxExx
            suffix = _extract_title_suffix(path.name)
            if suffix:
                matches = _find_all_title_matches(suffix, episode_map)
                title_info = self._select_best_candidate(
                    matches, path, season_offsets, claimed_dests, ep_num
                )
                if not title_info:
                    matches = _find_all_title_matches(suffix, specials_map)
                    title_info = self._select_best_candidate(
                        matches, path, season_offsets, claimed_dests, ep_num
                    )

                if title_info:
                    if info and info != title_info:
                        log.info(
                            "S%02dE%02d title mismatch (numeric matched: %r, title matched: %r) — overriding with title match",
                            season_num,
                            ep_num,
                            info.title,
                            title_info.title,
                        )
                    info = title_info

            if not info:
                # Last resort: try matching suffix by title without episode number
                matches = _find_all_title_matches(suffix or path.stem, episode_map)
                title_info = self._select_best_candidate(
                    matches, path, season_offsets, claimed_dests, ep_num
                )
                if not title_info:
                    matches = _find_all_title_matches(suffix or path.stem, specials_map)
                    title_info = self._select_best_candidate(
                        matches, path, season_offsets, claimed_dests, ep_num
                    )
                if title_info:
                    info = title_info

            if not info:
                max_season = max((s for s, _ in season_ep_map), default=0)
                hint = (
                    f"  (TMDB only has {max_season} season(s) "
                    "— try an Episode Group for more seasons)"
                    if season_num > max_season
                    else ""
                )
                log.warning("S%02dE%02d not in episode map — skipped.%s", season_num, ep_num, hint)
                return (
                    RenameResult(
                        path.name,
                        "",
                        EpisodeInfo(0, season_num, ep_num, ""),
                        status=RenameResult.Status.SKIPPED,
                    ),
                    next_auto_special,
                )
            return (
                self._handle_file(
                    path, info, dry_run, season_offsets, session_history, claimed_dests
                ),
                next_auto_special,
            )

        # ── Absolute number fallback ──────────────────────────
        abs_num = self._ep_parser.parse(path.name)
        if abs_num is None:
            # Try title matching before hash fallback if filename has a title suffix
            suffix = _extract_title_suffix(path.name) or path.stem
            if suffix:
                matches = _find_all_title_matches(suffix, episode_map)
                title_info = self._select_best_candidate(
                    matches, path, season_offsets, claimed_dests, None
                )
                if not title_info:
                    matches = _find_all_title_matches(suffix, specials_map)
                    title_info = self._select_best_candidate(
                        matches, path, season_offsets, claimed_dests, None
                    )
                if title_info:
                    return (
                        self._handle_file(
                            path,
                            title_info,
                            dry_run,
                            season_offsets,
                            session_history,
                            claimed_dests,
                        ),
                        next_auto_special,
                    )

            # ── ED2K hash fallback (when --use-hash is enabled) ────
            if self._cfg.use_hash:
                hash_info = self._lookup_by_hash(path)
                if hash_info:
                    # Cross-reference the hash result with the episode map
                    hash_ep = hash_info.episode_number
                    if hash_ep and hash_ep.isdigit():
                        ep_int = int(hash_ep)
                        info = episode_map.get(ep_int)
                        if info:
                            return (
                                self._handle_file(
                                    path,
                                    info,
                                    dry_run,
                                    season_offsets,
                                    session_history,
                                    claimed_dests,
                                ),
                                next_auto_special,
                            )
                        # No match in the main episode map — use hash data directly
                        title = (
                            hash_info.episode_title_en
                            or hash_info.episode_title_romaji
                            or f"Episode {ep_int}"
                        )
                        info = EpisodeInfo(
                            absolute=ep_int,
                            season=1,
                            episode=ep_int,
                            title=title,
                            source="anidb_hash",
                        )
                        return (
                            self._handle_file(
                                path, info, dry_run, season_offsets, session_history, claimed_dests
                            ),
                            next_auto_special,
                        )
            log.warning("Skipped (unrecognised): %s", path.name)
            return (
                RenameResult(
                    path.name, "", EpisodeInfo(0, 0, 0, ""), status=RenameResult.Status.SKIPPED
                ),
                next_auto_special,
            )

        info = episode_map.get(abs_num)

        # Title similarity validation/override for absolute numbers
        suffix = _extract_title_suffix(path.name)
        if suffix:
            matches = _find_all_title_matches(suffix, episode_map)
            title_info = self._select_best_candidate(
                matches, path, season_offsets, claimed_dests, abs_num
            )
            if not title_info:
                matches = _find_all_title_matches(suffix, specials_map)
                title_info = self._select_best_candidate(
                    matches, path, season_offsets, claimed_dests, abs_num
                )

            if title_info:
                if info and info != title_info:
                    log.info(
                        "Absolute %d title mismatch (numeric matched: %r, title matched: %r) — overriding with title match",
                        abs_num,
                        info.title,
                        title_info.title,
                    )
                info = title_info

        if not info:
            # ── ED2K hash fallback for episode not in map ────────
            if self._cfg.use_hash:
                hash_info = self._lookup_by_hash(path)
                if hash_info and hash_info.episode_number and hash_info.episode_number.isdigit():
                    ep_int = int(hash_info.episode_number)
                    hash_info_from_map = episode_map.get(ep_int)
                    if hash_info_from_map:
                        return (
                            self._handle_file(
                                path,
                                hash_info_from_map,
                                dry_run,
                                season_offsets,
                                session_history,
                                claimed_dests,
                            ),
                            next_auto_special,
                        )
            log.warning("Ep %d not in episode map — skipped.", abs_num)
            return (
                RenameResult(
                    path.name,
                    "",
                    EpisodeInfo(abs_num, 0, 0, ""),
                    status=RenameResult.Status.SKIPPED,
                ),
                next_auto_special,
            )

        return (
            self._handle_file(path, info, dry_run, season_offsets, session_history, claimed_dests),
            next_auto_special,
        )

    def _handle_file(
        self,
        path: Path,
        info: EpisodeInfo,
        dry_run: bool,
        season_offsets: dict[int, int],
        session_history: dict[str, str],
        claimed_dests: set[Path],
    ) -> RenameResult:
        """Compute the new name, log it, and (in live mode) rename the file."""
        cfg = self._cfg
        new_name = self._format_name(info, path.suffix, season_offsets)

        if cfg.organize_into_folders:
            if not dry_run:
                dest_folder = self._season_folder(info.season)
            else:
                if info.season == 0:
                    folder_name = cfg.specials_folder_name
                elif cfg.season_arc_names and info.season in cfg.season_arc_names:
                    folder_name = cfg.season_arc_names[info.season]
                else:
                    folder_name = cfg.season_folder_template.format(season=info.season)
                dest_folder = cfg.media_dir / folder_name
            dest_path = dest_folder / new_name
        else:
            dest_folder = cfg.media_dir
            dest_path = cfg.media_dir / new_name

        # Resolve to absolute for consistent comparison and history
        dest_resolved = dest_path.resolve()
        src_resolved = path.resolve()

        # Already named correctly
        if src_resolved == dest_resolved:
            # Even if the video is already named correctly, still check for
            # subtitles that might need to be moved alongside it
            new_stem = dest_path.stem
            self._process_subtitles(path, new_stem, dest_folder, dry_run, session_history)
            return RenameResult(path.name, new_name, info, status=RenameResult.Status.SKIPPED)

        # ── Duplicate destination check ──────────────────────
        # If another file in this session already claimed this destination,
        # or if a different existing file is already at that path, skip.
        if dest_resolved in claimed_dests:
            log.warning(
                "Duplicate target: %s already claimed — skipping %s",
                dest_resolved,
                src_resolved,
            )
            return RenameResult(
                path.name,
                new_name,
                info,
                status=RenameResult.Status.SKIPPED,
                error=f"Duplicate target name: {new_name}",
            )
        # Also check if a different pre-existing file is at the destination
        if dest_resolved.exists() and dest_resolved != src_resolved:
            log.warning(
                "Target already exists: %s — skipping %s",
                dest_resolved,
                src_resolved,
            )
            return RenameResult(
                path.name,
                new_name,
                info,
                status=RenameResult.Status.SKIPPED,
                error=f"Target already exists: {dest_resolved}",
            )

        # Claim this destination so no other file can take it
        claimed_dests.add(dest_resolved)

        self._log_rename(info, path, dest_path, season_offsets)

        result = RenameResult(
            path.name,
            new_name,
            info,
            dest_dir=str(dest_path.parent.relative_to(cfg.media_dir)),
        )

        if not dry_run:
            try:
                if cfg.organize_into_folders:
                    dest_path.parent.mkdir(exist_ok=True)

                if self._rename_via_qbit:
                    self._rename_via_qbit(path, dest_path, new_name)
                else:
                    path.rename(dest_path)

                result.status = RenameResult.Status.SUCCESS
                # Store full absolute paths so undo always works
                session_history[str(dest_resolved)] = str(src_resolved)

                # Rename matching subtitles alongside the video
                new_stem = dest_path.stem
                self._process_subtitles(path, new_stem, dest_folder, dry_run, session_history)

            except Exception as exc:
                result.error = str(exc)
                result.status = RenameResult.Status.ERROR
                log.error("Failed to rename %r: %s", path, exc)
        else:
            result.status = RenameResult.Status.SUCCESS
            # In dry-run, still report what subtitles would be renamed
            new_stem = dest_path.stem
            self._process_subtitles(path, new_stem, dest_folder, dry_run, session_history)

        return result

    def _process_subtitles(
        self,
        original_video_path: Path,
        new_video_stem: str,
        dest_dir: Path,
        dry_run: bool,
        session_history: dict[str, str],
    ) -> None:
        """
        Find and rename subtitle files matching a renamed video.

        This is a thin wrapper around :func:`process_subtitles_for_video`
        that also records subtitle renames in the undo history log and
        adapts the qBit rename callback to the two-argument signature
        expected by the subtitles module.
        """
        cfg = self._cfg

        # Build a rename function compatible with subtitles.rename_subtitle
        # (which expects old_path, new_path) from the qBit callback
        # (which expects old_path, new_path, new_name).
        sub_rename_fn: Callable[[Path, Path], None] | None = None
        if self._rename_via_qbit and not dry_run:
            _rename = self._rename_via_qbit

            def sub_rename_fn_impl(old_path: Path, new_path: Path) -> None:
                _rename(old_path, new_path, new_path.name)

            sub_rename_fn = sub_rename_fn_impl

        renamed = process_subtitles_for_video(
            original_video_path=original_video_path,
            new_video_stem=new_video_stem,
            dest_dir=dest_dir,
            cfg=cfg,
            dry_run=dry_run,
            rename_fn=sub_rename_fn,
        )

        # Record subtitle renames in the undo history (full absolute paths)
        if not dry_run:
            for new_sub_path in renamed:
                with contextlib.suppress(ValueError):
                    session_history[str(new_sub_path.resolve())] = str(
                        original_video_path.resolve()
                    )

    def _rename_folder(
        self,
        dry_run: bool,
        session_history: dict[str, str],
    ) -> bool:
        """
        Rename the containing folder (cfg.media_dir) to match the series name.

        This is called after all file renames are complete.  The folder is
        renamed to the sanitized series name so that Jellyfin can identify it
        correctly.

        Security: only renames if the folder is within BASE_DOWNLOAD_PATH
        (or the parent of media_dir).  The folder is only renamed if the
        new name differs from the current name.

        Returns True if the folder was (or would be) renamed, False otherwise.
        """
        cfg = self._cfg
        current_dir = cfg.media_dir.resolve()
        new_folder_name = sanitize_name(cfg.series_name)

        if not new_folder_name:
            log.debug("Folder rename skipped: series name is empty.")
            return False

        # Already named correctly
        if current_dir.name == new_folder_name:
            return False

        # Security check: prevent renaming the root BASE_DOWNLOAD_PATH itself.
        # When the user is inside BASE_DOWNLOAD_PATH, we allow folder renames.
        # When the user has explicitly chosen a directory outside BASE_DOWNLOAD_PATH
        # (e.g. via current-dir or custom path navigation), we still allow renames
        # since the user intentionally chose that location.
        parent = current_dir.parent
        base_download_path = cfg.base_download_path or _get_base_download_path()
        if base_download_path:
            try:
                current_dir.relative_to(base_download_path)
                # Inside BASE_DOWNLOAD_PATH — safe to rename
            except ValueError:
                # Outside BASE_DOWNLOAD_PATH — still allow rename since the
                # user explicitly chose this directory (via navigate/cwd/custom).
                # Only block if the folder IS the base path itself.
                if current_dir.resolve() == Path(str(base_download_path)).resolve():
                    log.warning(
                        "Folder rename skipped: refusing to rename BASE_DOWNLOAD_PATH itself (%s)",
                        current_dir,
                    )
                    return False
                # Log a note but proceed — the user chose this location
                log.info(
                    "Folder is outside BASE_DOWNLOAD_PATH — renaming anyway (user-selected directory)",
                )

        new_path = parent / new_folder_name

        # Check if the target folder name already exists
        if new_path.exists() and new_path != current_dir:
            log.warning(
                "Folder rename skipped: target folder already exists: %s",
                new_path,
            )
            return False

        if dry_run:
            log.info(
                "FOLDER  |  %s  ->  %s  (dry run)",
                current_dir.name,
                new_folder_name,
            )
            return True

        try:
            old_resolved = current_dir

            # If we have a qBit folder-rename callback, use it instead
            # of os.rename() so that qBit keeps tracking the files.
            if self._rename_folder_via_qbit:
                self._rename_folder_via_qbit(old_resolved, new_path)
            else:
                current_dir.rename(new_path)

            # Update cfg.media_dir to point to the new path
            cfg.media_dir = new_path
            # Record in undo history so the folder can be renamed back
            session_history[str(new_path.resolve())] = str(old_resolved)
            log.info(
                "FOLDER  |  %s  ->  %s",
                old_resolved.name,
                new_folder_name,
            )
            return True
        except OSError as exc:
            log.error("Failed to rename folder: %s", exc)
            return False

    def _get_dest_path(self, info: EpisodeInfo, new_name: str) -> Path:
        cfg = self._cfg
        if cfg.organize_into_folders:
            if info.season == 0:
                folder_name = cfg.specials_folder_name
            elif cfg.season_arc_names and info.season in cfg.season_arc_names:
                folder_name = cfg.season_arc_names[info.season]
            else:
                folder_name = cfg.season_folder_template.format(season=info.season)
            dest_folder = cfg.media_dir / folder_name
        else:
            dest_folder = cfg.media_dir
        return (dest_folder / new_name).resolve()

    def _select_best_candidate(
        self,
        matches: list[tuple[EpisodeInfo, float]],
        path: Path,
        season_offsets: dict[int, int],
        claimed_dests: set[Path],
        parsed_ep_num: int | None = None,
    ) -> EpisodeInfo | None:
        if not matches:
            return None

        if len(matches) == 1:
            return matches[0][0]

        candidates = []
        for ep, ratio in matches:
            dist = abs(ep.absolute - parsed_ep_num) if parsed_ep_num is not None else 0
            candidates.append((ep, ratio, dist))

        # Sort by: ratio descending, distance ascending
        candidates.sort(key=lambda x: (x[1], -x[2]), reverse=True)

        for ep, _, _ in candidates:
            new_name = self._format_name(ep, path.suffix, season_offsets)
            dest_path = self._get_dest_path(ep, new_name)
            if dest_path not in claimed_dests:
                return ep

        return candidates[0][0]

    def _log_rename(
        self,
        info: EpisodeInfo,
        path: Path,
        dest_path: Path,
        season_offsets: dict[int, int],
    ) -> None:
        cfg = self._cfg
        if info.is_special:
            tag = "SP"
        elif (
            season_offsets
            and cfg.episode_start_mode == START_MODE_CONTINUING
            and info.season in season_offsets
        ):
            cont_ep = info.episode + season_offsets[info.season]
            tag = f"S{info.season:02d}E{cont_ep:02d} (continuing)"
        else:
            tag = f"S{info.season:02d}E{info.episode:02d}"

        # Log FULL paths on both sides so undo can always work reliably.
        # The source path is the original location (before rename).
        # The dest path is the new location (after rename).
        log.info(
            "%s  |  %s  ->  %s",
            tag,
            path.resolve(),
            dest_path.resolve(),
        )

    # ── Naming helpers ────────────────────────────────────────

    def _format_name(
        self,
        info: EpisodeInfo,
        ext: str,
        season_offsets: dict[int, int] | None = None,
    ) -> str:
        series = sanitize_name(self._cfg.series_name)
        clean_title = sanitize_name(info.title)

        if info.is_special:
            return self._cfg.special_template.format(
                series=series,
                episode=info.episode,
                title=clean_title,
                ext=ext,
            )

        ep_display = _compute_display_episode(info, self._cfg, season_offsets or {})
        ep_fmt = _format_episode_number(ep_display)

        formatted = self._cfg.name_template.format(
            series=series,
            season=info.season,
            episode=ep_display,
            title=clean_title,
            ext=ext,
        )
        # Override the default 2-digit episode field when > 99
        if ep_display >= 100:
            formatted = re.sub(r"E\d{2}(?=\s*-)", f"E{ep_fmt}", formatted)

        return formatted

    def _season_folder(self, season: int) -> Path:
        cfg = self._cfg
        if season == 0:
            name = sanitize_name(cfg.specials_folder_name)
        elif cfg.season_arc_names and season in cfg.season_arc_names:
            # Use arc name if configured, e.g. "East Blue (1-61)"
            name = sanitize_name(cfg.season_arc_names[season])
        else:
            name = sanitize_name(cfg.season_folder_template.format(season=season))
        folder = cfg.media_dir / name
        folder.mkdir(exist_ok=True)
        return folder

    # ── Static utilities ──────────────────────────────────────

    @staticmethod
    def _compute_season_offsets(episode_map: dict[int, EpisodeInfo]) -> dict[int, int]:
        """
        Build a dict mapping season number -> episode count offset for
        "continuing" mode.

        Uses base-relative start positions for each season to robustly
        handle specials, custom start offsets, and numbering gaps.
        """
        seasons: dict[int, list[EpisodeInfo]] = {}
        for ep in episode_map.values():
            if ep.season > 0:
                seasons.setdefault(ep.season, []).append(ep)

        if not seasons:
            return {}

        base_abs = min(ep.absolute for eps in seasons.values() for ep in eps)

        offsets: dict[int, int] = {}
        for sn, eps in seasons.items():
            min_abs = min(ep.absolute for ep in eps)
            min_ep = min(ep.episode for ep in eps)
            offsets[sn] = max(0, (min_abs - base_abs) - (min_ep - 1))
        return offsets


# ---------------------------------------------------------------------------
# Module-level helpers (stateless, testable)
# ---------------------------------------------------------------------------


def _normalize_title(text: str) -> str:
    """
    Normalize string titles (lowercase, alphanumeric characters only)
    for safe matching.
    """
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", "", text).lower()
    return text


def _extract_title_suffix(filename: str) -> str:
    """
    Extract the title suffix from a filename by stripping out prefix noise
    (such as the series name and parsed season/episode patterns).
    """
    from renamer.parsers import EpisodeNumberParser

    stem = Path(filename).stem
    for pattern in EpisodeNumberParser.SEASON_EP_PATTERNS:
        m = pattern.search(stem)
        if m:
            suffix = stem[m.end() :].strip()
            suffix = re.sub(r"^[\s\-–_~.]+", "", suffix).strip()
            return suffix

    for _label, pattern in EpisodeNumberParser.PATTERNS:
        m = re.search(pattern, stem, re.IGNORECASE)
        if m:
            suffix = stem[m.end() :].strip()
            suffix = re.sub(r"^[\s\-–_~.]+", "", suffix).strip()
            return suffix

    return ""


def _find_all_title_matches(
    suffix: str, search_map: dict[int, EpisodeInfo]
) -> list[tuple[EpisodeInfo, float]]:
    """
    Find all matching episodes in search_map with ratio >= 0.85,
    sorted by ratio descending.
    """
    import difflib

    norm_suffix = _normalize_title(suffix)
    if not norm_suffix or len(norm_suffix) < 4:
        return []

    matches = []
    seen_ids = set()

    for ep in search_map.values():
        if id(ep) in seen_ids:
            continue
        seen_ids.add(id(ep))

        if not ep.title:
            continue
        norm_ep_title = _normalize_title(ep.title)
        if not norm_ep_title:
            continue

        if norm_suffix == norm_ep_title:
            matches.append((ep, 1.0))
            continue

        len_suffix = len(norm_suffix)
        len_ep = len(norm_ep_title)
        max_possible = (2.0 * min(len_suffix, len_ep)) / (len_suffix + len_ep)
        if max_possible < 0.80 and not (
            (norm_suffix in norm_ep_title or norm_ep_title in norm_suffix)
            and min(len_suffix, len_ep) >= 8
        ):
            continue

        ratio = difflib.SequenceMatcher(None, norm_suffix, norm_ep_title).ratio()

        if ratio < 0.85 and (
            (norm_suffix in norm_ep_title or norm_ep_title in norm_suffix)
            and min(len_suffix, len_ep) >= 8
        ):
            ratio = max(ratio, 0.85)

        if ratio >= 0.85:
            matches.append((ep, ratio))

    # Sort by ratio descending
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches


def _find_best_title_match(
    suffix: str, search_map: dict[int, EpisodeInfo]
) -> tuple[EpisodeInfo | None, float]:
    """
    Find the best title-matching episode in search_map using SequenceMatcher.
    """
    matches = _find_all_title_matches(suffix, search_map)
    if matches:
        return matches[0]
    return None, 0.0


def _get_base_download_path() -> Path | None:
    """
    Return the BASE_DOWNLOAD_PATH from the environment, or None if not set.

    Used by the folder-rename and navigate features to enforce security:
    only folders within this path can be renamed or navigated to.
    """
    import os

    raw = os.getenv("BASE_DOWNLOAD_PATH", "").strip()
    if raw:
        return Path(raw).resolve()
    return None


def sanitize_name(name: str) -> str:
    """
    Sanitise a string for use as a filename or folder name.

    * Normalises Unicode dashes to ``-`` and ``…`` to ``...``
    * Strips characters illegal on Windows and confusing for Jellyfin
    * Collapses runs of spaces and hyphens
    * Strips leading/trailing whitespace, dots, hyphens, and underscores
    """
    cleaned = name.translate(_UNICODE_DASHES)
    cleaned = _ILLEGAL_CHARS.sub("", cleaned)
    cleaned = _UNICODE_SPACE.sub(" ", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip(" .-_")


def _clean_special_title(title: str) -> str:
    """Strip codec/release-group noise from a special-episode filename stem."""
    cleaned = _CODEC_BRACKET.sub("", title)
    cleaned = _CODEC_LOOSE.sub("", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip(" -_")
    return cleaned


def _clean_folder_name(raw: str) -> str:
    """
    Heuristically clean a folder name into a searchable series title.

    Handles complex anime folder names like::

        [Judas] Botsuraku Yotei no Kizoku Dakedo, Hima Datta kara
        Mahou o Kiwamete Mita (I'm a Noble on the Brink of Ruin, So I
        Might as Well Try Mastering Magic) (Season 01) [1080p]
        [HEVC x265 10bit][Multi-Sub]

    Processing order:
      1. Replace underscores/dots with spaces
      2. Strip leading sub-group tags: ``[SubGroup] Anime`` → ``Anime``
      3. Strip season tags: ``(Season 01)`` → removed
      4. Strip codec/resolution brackets: ``[1080p]``, ``[HEVC x265 10bit]``,
         ``[Multi-Sub]``, ``[Dual-Audio]``, etc.
      5. Strip remaining trailing bracket tags
      6. Strip year in parentheses: ``(2024)``
      7. If an English/alternate title remains in parentheses *after* the
         main name, prefer the non-parenthesised (typically romaji) part
         as the primary search term — but also try the parenthesised
         (English) part if the romaji yields no results.

    Returns a clean, searchable title string.
    """
    name = raw.replace("_", " ").replace(".", " ")

    # 1. Strip leading sub-group tag: [Judas] ...
    name = _FOLDER_GROUP_TAG.sub("", name)

    # 2. Strip season tags: (Season 01), (Season 1)
    name = _FOLDER_SEASON_TAG.sub("", name)

    # 3. Strip codec/resolution/quality brackets: [1080p], [HEVC x265 10bit], [Multi-Sub]
    #    Repeatedly apply because there may be multiple bracket tags
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_CODEC_BRACKET.sub("", name)

    # 4. Strip language/source tags: [JPN], [ENG], [www], [v2]
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_LANG_BRACKET.sub("", name)

    # 5. Strip junk bracket tags: [1920x1080], [AAC], [FLAC], [10bit], [v2]
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_JUNK_BRACKET.sub("", name)

    # 6. Strip remaining trailing bracket tags (catch-all for anything left)
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_BRACKET_TAG.sub("", name)

    # 7. Strip year in parentheses: (2024)
    name = _FOLDER_YEAR_TAG.sub("", name)

    name = name.strip()

    # 8. If there's a parenthesised English/alternate title at the end,
    #    keep only the main (romaji/Japanese) part for the primary search.
    #    Example: "Botsuraku... (I'm a Noble on the Brink of Ruin)"
    #    → primary: "Botsuraku..."
    #    The English alternate title is stored separately for fallback search.
    m = _FOLDER_ALT_TITLE_PAREN.search(name)
    if m:
        alt = m.group(1).strip()
        main = name[: m.start()].strip()
        # Only strip the parenthesised part if it looks like an English title
        # (contains mostly ASCII letters and spaces) and isn't a year or season
        if alt and main and not re.match(r"^\d{4}$", alt):
            name = main

    return name


def _extract_alt_title_from_folder(raw: str) -> str | None:
    """
    Extract the English/alternate title from a complex folder name.

    For folder names like::

        [Judas] Romaji Name (English Name) (Season 01) [1080p]

    This returns the ``"English Name"`` part, or ``None`` if no
    parenthesised alternate title is found.

    Used as a fallback search query when the romaji name yields no
    results on TMDB.
    """
    name = raw.replace("_", " ").replace(".", " ")
    name = _FOLDER_GROUP_TAG.sub("", name)
    name = _FOLDER_SEASON_TAG.sub("", name)

    # Strip codec brackets
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_CODEC_BRACKET.sub("", name)

    # Strip language/source tags
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_LANG_BRACKET.sub("", name)

    # Strip junk bracket tags
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_JUNK_BRACKET.sub("", name)

    # Strip remaining trailing bracket tags
    prev = None
    while prev != name:
        prev = name
        name = _FOLDER_BRACKET_TAG.sub("", name)

    name = _FOLDER_YEAR_TAG.sub("", name)
    name = name.strip()

    # Look for parenthesised alternate title
    m = _FOLDER_ALT_TITLE_PAREN.search(name)
    if m:
        alt = m.group(1).strip()
        main = name[: m.start()].strip()
        if alt and main and not re.match(r"^\d{4}$", alt):
            return alt
    return None


def _extract_name_from_files(media_dir: Path) -> str | None:
    """
    Try to extract a series name from the video files in *media_dir*.

    Looks for common anime filename patterns like::

        [SubGroup] Series Name - 01 [1080p].mkv
        [SubGroup] Series Name - E01 [1080p].mkv

    Strips the sub-group tag and episode number, returning the
    common prefix across all video files (if consistent).

    Returns ``None`` if no consistent name can be extracted.
    """
    from renamer.parsers import SpecialParser

    sp_parser = SpecialParser()
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".flv", ".webm"}

    files = sorted(
        p for p in media_dir.rglob("*") if p.is_file() and p.suffix.lower() in video_exts
    )

    if not files:
        return None

    # Strip episode numbers and sub-group tags from each filename stem
    # to find the common "series name" prefix
    _EP_NUM_RE = re.compile(
        r"\s*[-–]\s*(?:E?P?\d+|S\d+E\d+).*$",
        re.IGNORECASE,
    )
    _SUBGROUP_RE = re.compile(r"^\[[^\]]+\]\s*")

    stems: list[str] = []
    for f in files:
        # Skip specials
        if sp_parser.parse(f.name) is not None:
            continue
        stem = f.stem
        # Strip leading [SubGroup] tag
        stem = _SUBGROUP_RE.sub("", stem)
        # Strip episode number suffix
        stem = _EP_NUM_RE.sub("", stem)
        # Strip trailing codec/resolution tags
        stem = _FOLDER_CODEC_BRACKET.sub("", stem)
        stem = _FOLDER_LANG_BRACKET.sub("", stem)
        stem = _FOLDER_JUNK_BRACKET.sub("", stem)
        stem = _FOLDER_BRACKET_TAG.sub("", stem)
        stem = stem.strip(" .-_")
        if stem:
            stems.append(stem)

    if not stems:
        return None

    # Find the longest common prefix (word-level)
    # If all stems start with the same words, that's the series name
    words_list = [s.split() for s in stems]
    if not words_list:
        return None

    # Find common prefix words
    common_words: list[str] = []
    min_len = min(len(w) for w in words_list)
    for i in range(min_len):
        word = words_list[0][i]
        if all(len(w) > i and w[i] == word for w in words_list):
            common_words.append(word)
        else:
            break

    # Need at least 1 word that's reasonably long to be meaningful
    if len(common_words) < 1:
        return None

    result = " ".join(common_words).strip()
    return result if len(result) >= 3 else None


def _compute_display_episode(
    info: EpisodeInfo,
    cfg: Config,
    season_offsets: dict[int, int],
) -> int:
    if cfg.absolute_numbering:
        return info.absolute
    if (
        season_offsets
        and cfg.episode_start_mode == START_MODE_CONTINUING
        and info.season in season_offsets
    ):
        return info.episode + season_offsets[info.season]
    return info.episode


def _format_episode_number(n: int) -> str:
    if n >= 1000:
        return f"{n:04d}"
    if n >= 100:
        return f"{n:03d}"
    return f"{n:02d}"
