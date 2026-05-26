"""
renamer — AnimeRenamer (main engine).
"""

import re
from pathlib import Path
from typing import Callable, Optional

from renamer.cache import SeriesCache
from renamer.config import Config, Provider, get_logger
from renamer.history import RenameHistory
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.providers.base import EpisodeGroupInfo, EpisodeInfo, RenameResult
from renamer.providers.registry import get_registry

log = get_logger()


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

        # Try to load cached series info
        cache = SeriesCache(self._cfg.MEDIA_DIR)
        cached = cache.load()
        if cached:
            log.info("Loaded series cache: %s", cached.get("series_name"))
            # Fill in any missing IDs from cache
            if not self._cfg.TMDB_SERIES_ID and cached.get("tmdb_series_id"):
                self._cfg.TMDB_SERIES_ID = cached["tmdb_series_id"]
            if not self._cfg.ANILIST_ID and cached.get("anilist_id"):
                self._cfg.ANILIST_ID = cached["anilist_id"]
            if not self._cfg.KITSU_ID and cached.get("kitsu_id"):
                self._cfg.KITSU_ID = cached["kitsu_id"]
            if not self._cfg.SERIES_NAME and cached.get("series_name"):
                self._cfg.SERIES_NAME = cached["series_name"]
            if isinstance(cached.get("provider"), Provider):
                self._cfg.provider = cached["provider"]
            if not self._cfg.EPISODE_GROUP_ID and cached.get("episode_group_id"):
                self._cfg.EPISODE_GROUP_ID = cached["episode_group_id"]

        # Use series folder name as default if still unset
        if not self._cfg.SERIES_NAME:
            self._cfg.SERIES_NAME = self._cfg.MEDIA_DIR.name

        # Auto-search for series ID if missing
        self._auto_search_series()

        # Create fetcher via registry
        registry = get_registry()
        fetcher = registry.create_fetcher(self._cfg.provider, self._cfg)
        if fetcher is None:
            log.error(
                "Could not create fetcher for provider: %s",
                self._cfg.provider.value,
            )
            return []

        episode_map = fetcher.fetch()
        specials_map = fetcher.fetch_specials()

        if not episode_map:
            log.error("Could not build episode map — aborting.")
            return []

        # Save cache with resolved IDs
        cache.save(
            series_name=self._cfg.SERIES_NAME,
            provider=self._cfg.provider,
            tmdb_series_id=self._cfg.TMDB_SERIES_ID,
            anilist_id=self._cfg.ANILIST_ID,
            kitsu_id=self._cfg.KITSU_ID,
            episode_group_id=self._cfg.EPISODE_GROUP_ID,
        )

        # Cross-reference IDs across providers for cache completeness
        self._cross_reference_ids()

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

    # ── Auto-search ──────────────────────────────────────
    def _auto_search_series(self) -> None:
        """
        If the active provider's series ID is missing, search by
        SERIES_NAME automatically and fill it in.

        This is non-interactive (always picks the top result) and
        silently does nothing on failure.
        """
        name = self._cfg.SERIES_NAME
        if not name:
            return

        registry = get_registry()

        # TMDB: need TMDB_SERIES_ID
        if (
            self._cfg.provider == Provider.TMDB
            and not self._cfg.TMDB_SERIES_ID
            and self._cfg.TMDB_API_KEY
        ):
            log.info("Auto-searching TMDB for '%s' …", name)
            result = registry.search(Provider.TMDB, name, self._cfg)
            if result:
                self._cfg.TMDB_SERIES_ID = result.series_id
                if result.series_name:
                    self._cfg.SERIES_NAME = result.series_name
                log.info(
                    "Auto-found: '%s' (TMDB ID %d)",
                    self._cfg.SERIES_NAME, self._cfg.TMDB_SERIES_ID,
                )
            else:
                log.warning("TMDB auto-search returned nothing for '%s'.", name)

        # AniList: need ANILIST_ID
        if (
            self._cfg.provider == Provider.AniList
            and not self._cfg.ANILIST_ID
        ):
            log.info("Auto-searching AniList for '%s' …", name)
            try:
                from renamer.providers.anilist import AniListFetcher
                af = AniListFetcher()
                aid = af.find_id(name)
                if aid:
                    self._cfg.ANILIST_ID = aid
                    romaji = af.find_romaji(name)
                    if romaji:
                        self._cfg.SERIES_NAME = romaji
                    log.info(
                        "Auto-found: '%s' (AniList ID %d)",
                        self._cfg.SERIES_NAME, self._cfg.ANILIST_ID,
                    )
            except Exception as exc:
                log.warning("AniList auto-search failed: %s", exc)

        # Kitsu: need KITSU_ID
        if (
            self._cfg.provider == Provider.Kitsu
            and not self._cfg.KITSU_ID
        ):
            log.info("Auto-searching Kitsu for '%s' …", name)
            try:
                from renamer.providers.kitsu import KitsuFetcher
                kf = KitsuFetcher(None, self._cfg)
                result = kf.find_series(name)
                if result:
                    self._cfg.KITSU_ID, romaji = result
                    self._cfg.SERIES_NAME = romaji
                    log.info(
                        "Auto-found: '%s' (Kitsu ID %d)",
                        self._cfg.SERIES_NAME, self._cfg.KITSU_ID,
                    )
            except Exception as exc:
                log.warning("Kitsu auto-search failed: %s", exc)

    # ── Episode Group listing ────────────────────────────
    def list_episode_groups(self) -> list[EpisodeGroupInfo]:
        """
        List available TMDB episode groups for the current series.
        Only works with the TMDB provider.
        """
        if self._cfg.provider != Provider.TMDB:
            log.warning(
                "Episode groups are a TMDB-only feature. "
                "Current provider: %s", self._cfg.provider.value,
            )
            return []

        if not self._cfg.TMDB_API_KEY or not self._cfg.TMDB_SERIES_ID:
            log.error(
                "TMDB API key and series ID are required for episode groups."
            )
            return []

        from renamer.providers.tmdb import TMDBFetcher
        fetcher = TMDBFetcher(
            self._cfg.TMDB_API_KEY,
            self._cfg.TMDB_SERIES_ID,
            self._cfg,
        )
        return fetcher.fetch_episode_groups()

    # ── internals ────────────────────────────────────────
    def _cross_reference_ids(self) -> None:
        """Try to fill in missing provider IDs by cross-referencing."""
        # If we have an AniList ID but no Kitsu ID, try to look up Kitsu
        # This is best-effort; failures are silently ignored.
        if self._cfg.ANILIST_ID and not self._cfg.KITSU_ID:
            try:
                from renamer.providers.kitsu import KitsuFetcher
                kf = KitsuFetcher(None, self._cfg)
                result = kf.find_series(self._cfg.SERIES_NAME)
                if result:
                    self._cfg.KITSU_ID = result[0]
                    log.info("Cross-ref: found Kitsu ID %d", self._cfg.KITSU_ID)
            except Exception:
                pass

    def _season_folder(self, season: int) -> Path:
        name = (
            self._sanitize_name(self._cfg.SPECIALS_FOLDER_NAME)
            if season == 0
            else self._sanitize_name(
                self._cfg.SEASON_FOLDER_TEMPLATE.format(season=season)
            )
        )
        folder = self._cfg.MEDIA_DIR / name
        folder.mkdir(exist_ok=True)
        return folder

    @staticmethod
    def _clean_special_title(title: str) -> str:
        """
        Strip release-group brackets and codec noise from special filenames,
        but keep meaningful content like (Memory Snow).
        """
        # Remove [Group] [1080p] style brackets
        cleaned = re.sub(r"\[(?:[^\]]*?(?:1080p|720p|480p|HEVC|x264|x265|AV1|WEB|BD|DVD|[0-9]+bit|[A-Z]{2,5}-(?:RIP|release)))\]", "", title, flags=re.IGNORECASE)
        # Remove standalone codec/resolution tags
        cleaned = re.sub(r"\b(?:1080p|720p|480p|HEVC|x264|x265|AV1)\b", "", cleaned, flags=re.IGNORECASE)
        # Collapse multiple spaces
        cleaned = re.sub(r" {2,}", " ", cleaned).strip()
        # Remove leading/trailing hyphens and spaces
        cleaned = cleaned.strip(" -_")
        return cleaned

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """
        Remove characters that are invalid in filenames or cause
        problems with Jellyfin scanning.

        Strips:  \\ / : * ? " < > |  (filesystem-illegal on Windows)
        Plus:    ;   (Jellyfin treats as separator)
        Replaces:  — –   →   -   (normalise dashes)
        Replaces:  …   →   ...
        Collapses multiple spaces.
        """
        # Normalise unicode dashes to plain hyphen FIRST (before stripping)
        cleaned = name.replace('\u2014', '-')   # em dash —
        cleaned = cleaned.replace('\u2013', '-') # en dash –
        cleaned = cleaned.replace('\u2012', '-') # figure dash
        cleaned = cleaned.replace('\u2015', '-') # horizontal bar
        # Normalise ellipsis
        cleaned = cleaned.replace('\u2026', '...')  # …
        # Remove filesystem-illegal and Jellyfin-confusing characters
        cleaned = re.sub(r'[\\/*?:"<>|;]', '', cleaned)
        # Remove other Unicode whitespace that can confuse Jellyfin
        cleaned = re.sub(r'[\u00a0\u2000-\u200b\u2028\u2029\u3000]', ' ', cleaned)
        # Collapse multiple spaces / hyphens
        cleaned = re.sub(r' {2,}', ' ', cleaned)
        cleaned = re.sub(r'-{2,}', '-', cleaned)
        # Strip leading/trailing whitespace, dots, hyphens, underscores
        cleaned = cleaned.strip(' .-_')
        return cleaned

    def _format_name(self, info: EpisodeInfo, ext: str) -> str:
        series = self._sanitize_name(self._cfg.SERIES_NAME)
        clean = self._sanitize_name(info.title)
        if info.is_special:
            ep_num = info.episode
            return self._cfg.SPECIAL_TEMPLATE.format(
                series=series,
                episode=ep_num,
                title=clean,
                ext=ext,
            )

        # Smart episode number width
        if self._cfg.ABSOLUTE_NUMBERING:
            ep_display = info.absolute
        else:
            ep_display = info.episode

        # Use wider format for large episode numbers
        if ep_display >= 1000:
            ep_fmt = f"{ep_display:04d}"
        elif ep_display >= 100:
            ep_fmt = f"{ep_display:03d}"
        else:
            ep_fmt = f"{ep_display:02d}"

        # Build template with smart width
        template = self._cfg.NAME_TEMPLATE
        formatted = template.format(
            series=series,
            season=info.season,
            episode=ep_display,
            title=clean,
            ext=ext,
        )
        # Fix the episode formatting: template uses {episode:02d} but we
        # may need 3 or 4 digits.  Replace the E02d portion.
        if ep_display >= 100:
            formatted = re.sub(
                r"E\d{2}(?=\s*-)",
                f"E{ep_fmt}",
                formatted,
            )

        return formatted

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

        # Build mapping for quick lookup by (season, episode)
        season_ep_map = {
            (ep.season, ep.episode): ep for ep in episode_map.values()
        }

        # Track auto-assigned special numbers
        next_auto_special = max(specials_map.keys(), default=0) + 1

        for path in files:
            if path.suffix.lower() not in self._cfg.VIDEO_EXTENSIONS:
                continue

            sp_num = self._special.parse(path.name)
            if sp_num is not None:
                # Auto-increment special numbers when sp_num == 0
                if sp_num == 0:
                    sp_num = next_auto_special
                    next_auto_special += 1

                info = specials_map.get(sp_num) or EpisodeInfo(
                    absolute=sp_num,
                    season=0,
                    episode=sp_num,
                    title=self._clean_special_title(path.stem),
                    source="filename",
                    is_special=True,
                )
                self._handle_file(
                    path, info, dry_run, organize, results, session_history
                )
                continue

            # Check explicit season and episode
            season_num, ep_num = self._parser.parse_season_episode(path.name)
            info = None
            if season_num is not None and ep_num is not None:
                if season_num == 0:
                    # S00Exx — look up in specials map
                    info = specials_map.get(ep_num)
                    if info:
                        self._handle_file(
                            path, info, dry_run, organize,
                            results, session_history,
                        )
                        continue
                    # Not in specials map — create a fallback special entry
                    info = EpisodeInfo(
                        absolute=ep_num,
                        season=0,
                        episode=ep_num,
                        title=self._clean_special_title(path.stem),
                        source="filename",
                        is_special=True,
                    )
                    self._handle_file(
                        path, info, dry_run, organize,
                        results, session_history,
                    )
                    continue

                info = season_ep_map.get((season_num, ep_num))
                if not info:
                    max_season = max(
                        (s for s, e in season_ep_map.keys()), default=0
                    )
                    log.warning(
                        "S%02dE%02d not in episode map — skipped.%s",
                        season_num, ep_num,
                        (
                            f"  (TMDB only has {max_season} season(s) "
                            f"— try an Episode Group for more seasons)"
                            if season_num > max_season
                            else ""
                        ),
                    )
                    results.append(
                        RenameResult(
                            path.name, "",
                            EpisodeInfo(0, season_num, ep_num, ""),
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
                        RenameResult(
                            path.name, "",
                            EpisodeInfo(0, 0, 0, ""),
                            skipped=True,
                        )
                    )
                    continue

                if abs_num not in episode_map:
                    log.warning(
                        "Ep %d not in episode map — skipped.", abs_num
                    )
                    results.append(
                        RenameResult(
                            path.name, "",
                            EpisodeInfo(abs_num, 0, 0, ""),
                            skipped=True,
                        )
                    )
                    continue
                info = episode_map[abs_num]

            self._handle_file(
                path, info, dry_run, organize,
                results, session_history,
            )

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
        path: Path,
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
                else self._cfg.MEDIA_DIR / (
                    self._cfg.SPECIALS_FOLDER_NAME
                    if info.season == 0
                    else self._cfg.SEASON_FOLDER_TEMPLATE.format(
                        season=info.season
                    )
                )
            )
            dest_path = dest_folder / new_name
            rel_key = str(
                dest_folder.relative_to(self._cfg.MEDIA_DIR) / new_name
            )
        else:
            dest_path = self._cfg.MEDIA_DIR / new_name
            rel_key = new_name

        if path.resolve() == dest_path.resolve():
            results.append(RenameResult(path.name, new_name, info, skipped=True))
            return

        season_tag = (
            "SP" if info.is_special
            else f"S{info.season:02d}E{info.episode:02d}"
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
