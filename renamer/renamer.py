"""
renamer.renamer
===============
The main renaming engine — ``AnimeRenamer``.

This module is intentionally free of I/O beyond file operations and logging.
All interactive prompts live in ``jellyfin_renamer.py``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

log = get_logger()

# Characters illegal in Windows filenames and/or confusing for Jellyfin
_ILLEGAL_CHARS = re.compile(r'[\\/*?:"<>|;]')
# Unicode whitespace variants that confuse Jellyfin
_UNICODE_SPACE = re.compile(r"[\u00a0\u2000-\u200b\u2028\u2029\u3000]")
# Unicode dashes → plain hyphen
_UNICODE_DASHES = str.maketrans({
    "\u2014": "-",  # em dash
    "\u2013": "-",  # en dash
    "\u2012": "-",  # figure dash
    "\u2015": "-",  # horizontal bar
    "\u2026": "...",  # ellipsis
})
# Codec/resolution tags inside brackets
_CODEC_BRACKET = re.compile(
    r"\[(?:[^\]]*?(?:1080p|720p|480p|HEVC|x264|x265|AV1|WEB|BD|DVD|"
    r"\d+bit|[A-Z]{2,5}-(?:RIP|release)))\]",
    re.IGNORECASE,
)
_CODEC_LOOSE = re.compile(r"\b(?:1080p|720p|480p|HEVC|x264|x265|AV1)\b", re.IGNORECASE)


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
    ) -> None:
        self._cfg = cfg
        self._history = RenameHistory(cfg.history_file)
        self._ep_parser = EpisodeNumberParser()
        self._sp_parser = SpecialParser()
        self._rename_via_qbit = rename_via_qbit

    # ── Public API ────────────────────────────────────────────

    def run(self, dry_run: bool = True) -> list[RenameResult]:
        """
        Scan ``cfg.media_dir``, resolve episode metadata, and rename files.

        Returns a list of :class:`RenameResult` objects (one per video file).
        When *dry_run* is ``True`` no files are modified.
        """
        errors = self._cfg.validate()
        if errors:
            for e in errors:
                log.error("Config error: %s", e)
            return []

        self._load_cache()

        if not self._cfg.series_name:
            self._cfg.series_name = self._cfg.media_dir.resolve().name

        self._auto_search_series()

        registry = get_registry()
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
            log.error("Provider configuration error: %s", exc)
            return []

        episode_map = fetcher.fetch()
        specials_map = fetcher.fetch_specials()

        if not episode_map:
            log.error("Could not build episode map — aborting.")
            return []

        self._save_cache()
        self._cross_reference_ids()

        return self._process_files(episode_map, specials_map, dry_run)

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

        restored, skipped_count, restored_keys = 0, 0, []
        for rel_new, orig_name in history.items():
            src = self._cfg.media_dir / rel_new
            dest = self._cfg.media_dir / orig_name
            if src.exists():
                try:
                    src.rename(dest)
                    log.info("Restored: %s -> %s", rel_new, orig_name)
                    restored_keys.append(rel_new)
                    restored += 1
                except OSError as exc:
                    log.error("Could not revert %r: %s", rel_new, exc)
            else:
                log.warning("Not found (skipped): %s", rel_new)
                skipped_count += 1

        self._history.clear_entries(restored_keys)
        log.info("Restored %d file(s). Skipped %d.", restored, skipped_count)

    def list_episode_groups(self) -> list[EpisodeGroupInfo]:
        """List available TMDB episode groups (TMDB provider only)."""
        if self._cfg.provider != Provider.TMDB:
            log.warning("Episode groups are TMDB-only. Current: %s", self._cfg.provider.value)
            return []
        if not self._cfg.tmdb_api_key or not self._cfg.tmdb_series_id:
            log.error("TMDB API key and series ID are required for episode groups.")
            return []
        from renamer.providers.tmdb import TMDBFetcher
        return TMDBFetcher(self._cfg.tmdb_api_key, self._cfg.tmdb_series_id, self._cfg).fetch_episode_groups()

    def list_alternative_titles(self) -> list[dict]:
        """List TMDB alternative titles (TMDB provider only)."""
        if self._cfg.provider != Provider.TMDB:
            log.warning("Alternative titles are TMDB-only. Current: %s", self._cfg.provider.value)
            return []
        if not self._cfg.tmdb_api_key or not self._cfg.tmdb_series_id:
            log.error("TMDB API key and series ID are required.")
            return []
        from renamer.providers.tmdb import TMDBFetcher
        return TMDBFetcher(self._cfg.tmdb_api_key, self._cfg.tmdb_series_id, self._cfg).fetch_alternative_titles()

    # ── Cache helpers ─────────────────────────────────────────

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
        if getattr(cfg, "provider", None) is None:
            cfg.provider = Provider.TMDB
        if not cfg.episode_group_id and data.get("episode_group_id"):
            cfg.episode_group_id = data["episode_group_id"]
        if data.get("episode_start_mode"):
            cfg.episode_start_mode = data["episode_start_mode"]

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
        )

    # ── Auto-search ───────────────────────────────────────────

    def _auto_search_series(self) -> None:
        """
        Silently search for a series ID if the active provider needs one but
        none is set.  Always picks the top result — no user interaction.
        """
        name = self._cfg.series_name
        if not name:
            return

        registry = get_registry()
        cfg = self._cfg

        if cfg.provider == Provider.TMDB and not cfg.tmdb_series_id and cfg.tmdb_api_key:
            log.info("Auto-searching TMDB for %r …", name)
            result = registry.search(Provider.TMDB, name, cfg)
            if result:
                cfg.tmdb_series_id = result.series_id
                cfg.series_name = result.series_name or name
                log.info("Auto-found: %r (TMDB ID %d)", cfg.series_name, cfg.tmdb_series_id)
            else:
                log.warning("TMDB auto-search returned nothing for %r.", name)

        elif cfg.provider == Provider.AniList and not cfg.anilist_id:
            log.info("Auto-searching AniList for %r …", name)
            try:
                from renamer.providers.anilist import AniListFetcher
                af = AniListFetcher()
                aid = af.find_id(name)
                if aid:
                    cfg.anilist_id = aid
                    romaji = af.find_romaji(name)
                    if romaji:
                        cfg.series_name = romaji
                    log.info("Auto-found: %r (AniList ID %d)", cfg.series_name, cfg.anilist_id)
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

    # ── File processing ───────────────────────────────────────

    def _process_files(
        self,
        episode_map: dict[int, EpisodeInfo],
        specials_map: dict[int, EpisodeInfo],
        dry_run: bool,
    ) -> list[RenameResult]:
        cfg = self._cfg
        files = sorted(p for p in cfg.media_dir.rglob("*") if p.is_file())
        results: list[RenameResult] = []
        session_history: dict[str, str] = {}

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
            )
            results.append(result)

        done = sum(1 for r in results if r.success)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if r.error)

        if not dry_run and session_history:
            self._history.save(session_history)

        tag = "Would process" if dry_run else "Processed"
        log.info("%s: %d | Skipped: %d | Errors: %d", tag, done, skipped, failed)
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
            info = specials_map.get(sp_num) or EpisodeInfo(
                absolute=sp_num,
                season=0,
                episode=sp_num,
                title=_clean_special_title(path.stem),
                source="filename",
                is_special=True,
            )
            return (
                self._handle_file(path, info, dry_run, season_offsets, session_history),
                next_auto_special,
            )

        # ── SxxExx detection ─────────────────────────────────
        season_num, ep_num = self._ep_parser.parse_season_episode(path.name)
        if season_num is not None and ep_num is not None:
            if season_num == 0:
                # S00Exx — look up in specials map
                info = specials_map.get(ep_num) or EpisodeInfo(
                    absolute=ep_num, season=0, episode=ep_num,
                    title=_clean_special_title(path.stem),
                    source="filename", is_special=True,
                )
                return (
                    self._handle_file(path, info, dry_run, season_offsets, session_history),
                    next_auto_special,
                )

            info = season_ep_map.get((season_num, ep_num))
            if not info:
                max_season = max((s for s, _ in season_ep_map), default=0)
                hint = (
                    f"  (TMDB only has {max_season} season(s) "
                    "— try an Episode Group for more seasons)"
                    if season_num > max_season else ""
                )
                log.warning("S%02dE%02d not in episode map — skipped.%s", season_num, ep_num, hint)
                return (
                    RenameResult(path.name, "", EpisodeInfo(0, season_num, ep_num, ""),
                                 status=RenameResult.Status.SKIPPED),
                    next_auto_special,
                )
            return (
                self._handle_file(path, info, dry_run, season_offsets, session_history),
                next_auto_special,
            )

        # ── Absolute number fallback ──────────────────────────
        abs_num = self._ep_parser.parse(path.name)
        if abs_num is None:
            log.warning("Skipped (unrecognised): %s", path.name)
            return (
                RenameResult(path.name, "", EpisodeInfo(0, 0, 0, ""),
                             status=RenameResult.Status.SKIPPED),
                next_auto_special,
            )

        info = episode_map.get(abs_num)
        if not info:
            log.warning("Ep %d not in episode map — skipped.", abs_num)
            return (
                RenameResult(path.name, "", EpisodeInfo(abs_num, 0, 0, ""),
                             status=RenameResult.Status.SKIPPED),
                next_auto_special,
            )

        return (
            self._handle_file(path, info, dry_run, season_offsets, session_history),
            next_auto_special,
        )

    def _handle_file(
        self,
        path: Path,
        info: EpisodeInfo,
        dry_run: bool,
        season_offsets: dict[int, int],
        session_history: dict[str, str],
    ) -> RenameResult:
        """Compute the new name, log it, and (in live mode) rename the file."""
        cfg = self._cfg
        new_name = self._format_name(info, path.suffix, season_offsets)

        if cfg.organize_into_folders:
            dest_folder = self._season_folder(info.season) if not dry_run else (
                cfg.media_dir / (
                    cfg.specials_folder_name if info.season == 0
                    else cfg.season_folder_template.format(season=info.season)
                )
            )
            dest_path = dest_folder / new_name
            rel_key = str(dest_folder.relative_to(cfg.media_dir) / new_name)
        else:
            dest_path = cfg.media_dir / new_name
            rel_key = new_name

        # Already named correctly
        if path.resolve() == dest_path.resolve():
            return RenameResult(path.name, new_name, info, status=RenameResult.Status.SKIPPED)

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
                session_history[rel_key] = path.name
            except Exception as exc:
                result.error = str(exc)
                result.status = RenameResult.Status.ERROR
                log.error("Failed to rename %r: %s", path.name, exc)
        else:
            result.status = RenameResult.Status.SUCCESS

        return result

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

        dest_label = (
            (dest_path.parent.name + "/") if cfg.organize_into_folders else ""
        ) + dest_path.name
        log.info("%s  |  %s  ->  %s", tag, path.name, dest_label)

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
                series=series, episode=info.episode, title=clean_title, ext=ext,
            )

        ep_display = _compute_display_episode(info, self._cfg, season_offsets or {})
        ep_fmt = _format_episode_number(ep_display)

        formatted = self._cfg.name_template.format(
            series=series, season=info.season, episode=ep_display,
            title=clean_title, ext=ext,
        )
        # Override the default 2-digit episode field when > 99
        if ep_display >= 100:
            formatted = re.sub(r"E\d{2}(?=\s*-)", f"E{ep_fmt}", formatted)

        return formatted

    def _season_folder(self, season: int) -> Path:
        cfg = self._cfg
        name = sanitize_name(
            cfg.specials_folder_name if season == 0
            else cfg.season_folder_template.format(season=season)
        )
        folder = cfg.media_dir / name
        folder.mkdir(exist_ok=True)
        return folder

    # ── Static utilities ──────────────────────────────────────

    @staticmethod
    def _compute_season_offsets(episode_map: dict[int, EpisodeInfo]) -> dict[int, int]:
        """
        Build a dict mapping season number -> episode count offset for
        "continuing" mode.

        Example: if S1 has 12 eps and S2 has 25:
            {1: 0, 2: 12, 3: 37}
        """
        counts: dict[int, int] = {}
        for ep in episode_map.values():
            if ep.season > 0:
                counts[ep.season] = max(counts.get(ep.season, 0), ep.episode)

        offsets: dict[int, int] = {}
        running = 0
        for sn in sorted(counts):
            offsets[sn] = running
            running += counts[sn]
        return offsets


# ---------------------------------------------------------------------------
# Module-level helpers (stateless, testable)
# ---------------------------------------------------------------------------


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
