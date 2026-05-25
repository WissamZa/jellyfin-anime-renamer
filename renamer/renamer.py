"""
renamer.py — Main renaming engine.

Uses the provider registry to create fetchers and resolve series names,
then processes files according to the episode map.
"""

import re
from typing import Callable, Optional

from renamer.cache import SeriesCache
from renamer.config import Config, Provider, log
from renamer.history import RenameHistory
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.providers.base import EpisodeInfo, RenameResult
from renamer.providers.registry import ProviderRegistry
from renamer.providers.base import SeriesSearchResult


class AnimeRenamer:
    """
    Main renaming engine.

    Used by jellyfin_renamer.py (interactive CLI) and qbit_hook.py
    (automated, called after a torrent completes).

    When called from qbit_hook.py, the `rename_via_qbit` callable is
    provided so that file moves go through the qBittorrent API instead
    of os.rename(), keeping qBit's seeding state intact.
    """

    def __init__(
        self,
        cfg: Config,
        rename_via_qbit: Optional[Callable] = None,
    ):
        self._cfg = cfg
        self._history = RenameHistory(cfg.history_file)
        self._parser = EpisodeNumberParser()
        self._special = SpecialParser()
        self._rename_via_qbit = rename_via_qbit

    # ── public ───────────────────────────────────────────
    def run(self, dry_run: bool = True) -> list[RenameResult]:
        errors = self._cfg.validate()
        if errors:
            for e in errors:
                log.error("Config error: %s", e)
            return []

        cache = SeriesCache(self._cfg.MEDIA_DIR)

        # ── Step 1: Try loading from folder cache ──────────────────
        self._load_cache(cache)

        # ── Step 2: If still unresolved, search using the provider registry ─
        if self._needs_resolve():
            self._resolve_via_registry(cache)

        # ── Step 3: Build the episode map using the provider registry ──────
        try:
            fetcher = ProviderRegistry.create_fetcher(self._cfg)
        except ValueError as e:
            log.error("%s", e)
            return []

        episode_map = fetcher.fetch()
        specials_map = fetcher.fetch_specials()

        if not episode_map:
            log.error("Could not build episode map — aborting.")
            return []

        return self._process_files(episode_map, specials_map, dry_run)

    def undo(self) -> None:
        history = self._history.load()
        if not history:
            log.info("No rename history found — nothing to undo.")
            return

        log.info("Found %d entries in rename history.", len(history))
        confirm = input("  Revert all to originals? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

        restored, skipped_count, restored_keys = 0, 0, []
        for rel_new, orig_name in history.items():
            src = self._cfg.MEDIA_DIR / rel_new
            dest = self._cfg.MEDIA_DIR / orig_name
            if src.exists():
                try:
                    src.rename(dest)
                    log.info("Restored: %s -> %s", rel_new, orig_name)
                    restored_keys.append(rel_new)
                    restored += 1
                except OSError as e:
                    log.error("Could not revert '%s': %s", rel_new, e)
            else:
                log.warning("Not found (skipped): %s", rel_new)
                skipped_count += 1

        self._history.clear_entries(restored_keys)
        log.info("Restored %d file(s). Skipped %d.", restored, skipped_count)

    # ── cache & resolve helpers ────────────────────────────

    def _needs_resolve(self) -> bool:
        """Check if the current provider's ID is still unresolved."""
        provider = self._cfg.PROVIDER
        if provider == Provider.TMDB and self._cfg.TMDB_SERIES_ID is None:
            return True
        if provider == Provider.AniList and self._cfg.ANILIST_ID is None:
            return True
        if provider == Provider.Kitsu and self._cfg.KITSU_ID is None:
            return True
        return False

    def _load_cache(self, cache: SeriesCache) -> None:
        """Load cached series info and apply it to the config."""
        if not self._needs_resolve():
            return

        cached = cache.load()
        if not cached:
            return

        cached_provider = cached.get("provider", Provider.TMDB)
        self._cfg.SERIES_NAME = cached["series_name"]

        if cached.get("tmdb_series_id"):
            self._cfg.TMDB_SERIES_ID = cached["tmdb_series_id"]
        if cached.get("anilist_id"):
            self._cfg.ANILIST_ID = cached["anilist_id"]
        if cached.get("kitsu_id"):
            self._cfg.KITSU_ID = cached["kitsu_id"]

        # If the cached provider differs from the current one, switch
        if cached_provider != self._cfg.PROVIDER:
            log.info(
                "Cache provider (%s) differs from configured (%s) — switching to cached",
                cached_provider.value,
                self._cfg.PROVIDER.value,
            )
            self._cfg.PROVIDER = cached_provider

        log.info(
            "Series cache hit -> '%s' (provider=%s, TMDB id=%s, AniList id=%s, Kitsu id=%s) — skipping API search",
            self._cfg.SERIES_NAME,
            self._cfg.PROVIDER.value,
            self._cfg.TMDB_SERIES_ID,
            self._cfg.ANILIST_ID,
            self._cfg.KITSU_ID,
        )

    def _resolve_via_registry(self, cache: SeriesCache) -> None:
        """
        Resolve series metadata using the provider registry.

        This replaces the old per-provider _resolve_via_*() methods
        with a single, generic flow:
          1. Search using the registry
          2. Apply the result to config
          3. Cross-reference with other providers for cache completeness
          4. Save to cache
        """
        try:
            result = ProviderRegistry.search(self._cfg)
        except ValueError as e:
            log.error("%s", e)
            return

        if not result:
            log.error(
                "Could not find '%s' on %s — aborting.",
                self._cfg.SERIES_NAME,
                self._cfg.PROVIDER.value,
            )
            return

        # Apply the search result
        self._apply_search_result(result)

        log.info(
            "Resolved via %s -> ID: %d  Series name: '%s'",
            result.provider_name,
            result.provider_id,
            result.series_name,
        )

        # Cross-reference with other providers for cache completeness
        self._cross_reference_ids()

        # Save to cache
        cache.save(
            series_name=self._cfg.SERIES_NAME,
            provider=self._cfg.PROVIDER,
            tmdb_series_id=self._cfg.TMDB_SERIES_ID,
            anilist_id=self._cfg.ANILIST_ID,
            kitsu_id=self._cfg.KITSU_ID,
        )

    def _apply_search_result(self, result: SeriesSearchResult) -> None:
        """Apply a search result's ID and name to the config."""
        self._cfg.SERIES_NAME = result.series_name

        provider = self._cfg.PROVIDER
        if provider == Provider.TMDB:
            self._cfg.TMDB_SERIES_ID = result.provider_id
        elif provider == Provider.AniList:
            self._cfg.ANILIST_ID = result.provider_id
        elif provider == Provider.Kitsu:
            self._cfg.KITSU_ID = result.provider_id

    def _cross_reference_ids(self) -> None:
        """
        Try to resolve IDs from other providers for cache completeness.

        This means a Kitsu-resolved series will also have AniList/TMDB IDs
        in the cache, making provider switches seamless.
        """
        provider = self._cfg.PROVIDER
        series_name = self._cfg.SERIES_NAME

        # Try AniList if not already resolved
        if self._cfg.ANILIST_ID is None:
            try:
                from renamer.providers.anilist import AniListFetcher
                al = AniListFetcher()
                al_id = al.find_id(series_name)
                if al_id:
                    self._cfg.ANILIST_ID = al_id
                    log.info("Cross-ref: resolved AniList ID: %d", al_id)
            except Exception:
                pass

        # Try TMDB if not already resolved and we have an API key
        if self._cfg.TMDB_SERIES_ID is None and self._cfg.TMDB_API_KEY:
            try:
                from renamer.providers.tmdb import TMDBSearch
                searcher = TMDBSearch(self._cfg.TMDB_API_KEY)
                tmdb_result = searcher.find(series_name)
                if tmdb_result:
                    self._cfg.TMDB_SERIES_ID = tmdb_result[0]
                    log.info("Cross-ref: resolved TMDB ID: %d", self._cfg.TMDB_SERIES_ID)
            except Exception:
                pass

        # Try Kitsu if not already resolved
        if self._cfg.KITSU_ID is None:
            try:
                from renamer.providers.kitsu import KitsuFetcher
                kf = KitsuFetcher()
                kf_result = kf.find_series(series_name)
                if kf_result:
                    self._cfg.KITSU_ID = kf_result[0]
                    log.info("Cross-ref: resolved Kitsu ID: %d", self._cfg.KITSU_ID)
            except Exception:
                pass

    # ── internals ────────────────────────────────────────
    def _season_folder(self, season: int) -> "Path":
        from pathlib import Path
        name = (
            self._cfg.SPECIALS_FOLDER_NAME
            if season == 0
            else self._cfg.SEASON_FOLDER_TEMPLATE.format(season=season)
        )
        folder = self._cfg.MEDIA_DIR / name
        folder.mkdir(exist_ok=True)
        return folder

    def _format_name(self, info: EpisodeInfo, ext: str) -> str:
        clean = re.sub(r'[\\/*?:"<>|]', "", info.title)
        if info.is_special:
            return self._cfg.SPECIAL_TEMPLATE.format(
                series=self._cfg.SERIES_NAME,
                episode=info.episode,
                absolute=info.absolute,
                title=clean,
                ext=ext,
            )

        # Long-running anime (e.g. One Piece, Naruto) use the absolute episode
        # number as the episode field so e.g. episode 1163 renders as S23E1163
        # rather than S23E01 (which would be meaningless to media managers).
        #
        # Threshold behaviour (ABSOLUTE_EPISODE_THRESHOLD in .env):
        #   > 0  → use absolute when absolute > threshold  (default: 100)
        #   = 0  → always use within-season numbering
        #   < 0  → always use absolute numbering
        threshold = self._cfg.ABSOLUTE_EPISODE_THRESHOLD
        if threshold < 0 or (threshold > 0 and info.absolute > threshold):
            episode_field = info.absolute
        else:
            episode_field = info.episode

        return self._cfg.NAME_TEMPLATE.format(
            series=self._cfg.SERIES_NAME,
            season=info.season,
            episode=episode_field,
            absolute=info.absolute,
            title=clean,
            ext=ext,
        )

    @staticmethod
    def _clean_special_title(stem: str, series_name: str = "") -> str:
        """
        Clean a special episode's filename stem into a readable title.

        Strips release-group brackets, quality tags, SxxExx prefixes, and
        the series name prefix so the result is just the episode title.

        Examples:
          [Judas] Re Zero - OVA (Memory Snow) [1080p][HEVC x265 10bit]
          →  OVA (Memory Snow)

          One Piece - S00E42 - [Judas] One Piece - SP42
          →  SP42

          [SubGroup] Naruto Shippuuden - Fan Letter [720p]
          →  Fan Letter
        """
        import re as _re
        cleaned = stem

        # 1. Remove all bracketed tags: [Judas], [1080p], [HEVC x265 10bit], etc.
        cleaned = _re.sub(r"\[[^\]]*\]", "", cleaned)

        # 2. Remove parentheses containing ONLY technical noise
        cleaned = _re.sub(
            r"\((?:\d{3,4}p?|HEVC|x26\d|10bit|Multi-Subs|AAC|FLAC|BD|DVD|UNCEN|UNCUT)\s*\)",
            "", cleaned, flags=_re.IGNORECASE,
        )

        # 3. Collapse whitespace early so anchor-based matches work reliably
        #    (bracket removal in step 1 can leave leading/trailing spaces).
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()

        # 4. Strip SxxExx / S00Exx segment and everything before it.
        #    e.g. "One Piece - S00E42 - One Piece - SP42" → "One Piece - SP42"
        if _re.search(r"[Ss]\d+[Ee]\d+", cleaned):
            cleaned = _re.sub(r".*?[Ss]\d+[Ee]\d+\s*[-–]?\s*", "", cleaned, count=1)

        # 5. Strip any leading series-name prefix (case-insensitive).
        #    Applied both when no SxxExx was present (raw torrent name) and
        #    after step 3 in case the series name repeats after the tag.
        #
        #    The match is done word-by-word from the longest prefix down so
        #    that a short-form in the filename ("Re Zero") still matches even
        #    when the resolved name is longer ("Re Zero kara Hajimeru …").
        if series_name:
            # Build candidates: full name, then progressively shorter prefixes
            # (stop at 2 words so we don't accidentally strip too little)
            words = series_name.strip().split()
            for n in range(len(words), 1, -1):
                prefix = " ".join(words[:n])
                escaped = _re.escape(prefix)
                new_cleaned = _re.sub(
                    rf"^{escaped}\s*[-–]?\s*", "", cleaned, flags=_re.IGNORECASE
                )
                if new_cleaned != cleaned:
                    cleaned = new_cleaned
                    break

        # 6. Collapse whitespace and strip leading/trailing dashes
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()
        cleaned = _re.sub(r"^[-–\s]+|[-–\s]+$", "", cleaned).strip()

        return cleaned if cleaned else stem

    def _process_files(
        self,
        episode_map: dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        dry_run: bool,
    ) -> list[RenameResult]:
        media_dir = self._cfg.MEDIA_DIR
        files = sorted(p for p in media_dir.rglob("*") if p.is_file())
        results: list[RenameResult] = []
        session_history: dict[str, str] = {}

        organize = self._cfg.ORGANIZE_INTO_FOLDERS
        mode_label = (
            "DRY RUN — no files will be changed"
            if dry_run
            else "LIVE — files will be renamed"
            + (" & organised into season folders" if organize else "")
        )
        log.info("Scanning: %s | %s", media_dir, mode_label)

        season_ep_map: dict[tuple[int, int], EpisodeInfo] = {
            (ep.season, ep.episode): ep for ep in episode_map.values()
        }

        # Track auto-incrementing special number for unnumbered specials (OVA, OAD, etc.)
        _next_special_num: int = max(specials_map.keys(), default=0) + 1

        for path in files:
            if path.suffix.lower() not in self._cfg.VIDEO_EXTENSIONS:
                continue

            sp_num = self._special.parse(path.name)
            if sp_num is not None:
                # sp_num == 0 means "special detected but number unknown"
                # → auto-assign the next available special number
                if sp_num == 0:
                    sp_num = _next_special_num
                    _next_special_num += 1

                info = specials_map.get(sp_num) or EpisodeInfo(
                    absolute=sp_num,
                    season=0,
                    episode=sp_num,
                    title=self._clean_special_title(path.stem, self._cfg.SERIES_NAME),
                    source="filename",
                    is_special=True,
                )
                self._handle_file(path, info, dry_run, organize, results, session_history)
                continue

            # Check for explicit season and episode
            season_num, ep_num = self._parser.parse_season_episode(path.name)
            info = None
            if season_num is not None and ep_num is not None:
                info = season_ep_map.get((season_num, ep_num))

                # ── Absolute-number fallback ───────────────────────────────
                # Files from long-running anime (One Piece, Naruto, etc.) are
                # commonly named with the absolute episode number in the
                # episode field, e.g. "One Piece - S22E1100.mkv".
                # TMDB stores S22 with only ~50 within-season episodes, so
                # the (season=22, episode=1100) lookup fails.  When the
                # episode number exceeds the threshold we treat it as an
                # absolute number and look it up in episode_map directly.
                if not info:
                    threshold = self._cfg.ABSOLUTE_EPISODE_THRESHOLD
                    treat_as_absolute = (
                        threshold < 0
                        or (threshold > 0 and ep_num > threshold)
                    )
                    if treat_as_absolute and ep_num in episode_map:
                        info = episode_map[ep_num]
                        log.info(
                            "S%02dE%d not in season map — matched as absolute ep %d",
                            season_num, ep_num, ep_num,
                        )

                # ── Season-overflow fallback ───────────────────────────────
                # If the season number in the filename exceeds the highest
                # season in the episode map (provider re-numbered seasons),
                # try the episode under the last known season instead.
                if not info:
                    max_season = max((s for s, e in season_ep_map.keys()), default=1)
                    if season_num > max_season:
                        info = season_ep_map.get((max_season, ep_num))
                        if info:
                            log.info(
                                "Season %d > max season %d — mapped ep %d to S%02d",
                                season_num, max_season, ep_num, max_season,
                            )

                if not info:
                    log.warning("S%02dE%d not in episode map — skipped.", season_num, ep_num)
                    results.append(
                        RenameResult(
                            path.name, "", EpisodeInfo(0, season_num, ep_num, ""),
                            skipped=True,
                        )
                    )
                    continue
            else:
                # Fallback to absolute episode number lookup
                abs_num = self._parser.parse(path.name)
                if abs_num is None:
                    log.warning("Skipped (unrecognised): %s", path.name)
                    results.append(
                        RenameResult(path.name, "", EpisodeInfo(0, 0, 0, ""), skipped=True)
                    )
                    continue

                if abs_num not in episode_map:
                    log.warning("Ep %d not in episode map — skipped.", abs_num)
                    results.append(
                        RenameResult(
                            path.name, "", EpisodeInfo(abs_num, 0, 0, ""), skipped=True
                        )
                    )
                    continue
                info = episode_map[abs_num]

            self._handle_file(path, info, dry_run, organize, results, session_history)

        done = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if r.error)

        if not dry_run and session_history:
            self._history.save(session_history)

        tag = "Would process" if dry_run else "Processed"
        log.info("%s: %d | Skipped: %d | Errors: %d", tag, done, skipped, failed)
        if not dry_run and session_history:
            log.info("Undo log: %s", self._cfg.history_file)

        return results

    def _handle_file(
        self,
        path,
        info: EpisodeInfo,
        dry_run: bool,
        organize: bool,
        results: list[RenameResult],
        session_history: dict[str, str],
    ) -> None:
        new_name = self._format_name(info, path.suffix)

        if organize:
            dest_folder = (
                self._season_folder(info.season)
                if not dry_run
                else self._cfg.MEDIA_DIR
                / (
                    self._cfg.SPECIALS_FOLDER_NAME
                    if info.season == 0
                    else self._cfg.SEASON_FOLDER_TEMPLATE.format(season=info.season)
                )
            )
            dest_path = dest_folder / new_name
            rel_key = str(dest_folder.relative_to(self._cfg.MEDIA_DIR) / new_name)
        else:
            dest_path = self._cfg.MEDIA_DIR / new_name
            rel_key = new_name

        if path.resolve() == dest_path.resolve():
            results.append(RenameResult(path.name, new_name, info, skipped=True))
            return

        threshold = self._cfg.ABSOLUTE_EPISODE_THRESHOLD
        if threshold < 0 or (threshold > 0 and info.absolute > threshold):
            ep_display = info.absolute
        else:
            ep_display = info.episode

        season_tag = (
            "SP" if info.is_special else f"S{info.season:02d}E{ep_display}"
        )
        log.info(
            "%s  |  %s  ->  %s",
            season_tag,
            path.name,
            (dest_path.parent.name + "/" if organize else "") + new_name,
        )

        result = RenameResult(
            path.name,
            new_name,
            info,
            dest_dir=str(dest_path.parent.relative_to(self._cfg.MEDIA_DIR)),
        )

        if not dry_run:
            try:
                if organize:
                    dest_path.parent.mkdir(exist_ok=True)

                if self._rename_via_qbit:
                    self._rename_via_qbit(
                        old_path=path,
                        new_path=dest_path,
                        new_name=new_name,
                    )
                else:
                    path.rename(dest_path)

                result.success = True
                session_history[rel_key] = path.name

            except Exception as e:
                result.error = str(e)
                log.error("Failed to rename '%s': %s", path.name, e)
        else:
            result.success = True

        results.append(result)
