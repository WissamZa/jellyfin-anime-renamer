"""
renamer.subtitle_matcher
========================
Rename subtitle files to match their corresponding video files.

Unlike the existing ``subtitles`` module (which matches by identical stem),
this module matches subtitles to videos by **season/episode number** (S01E01
pattern).  This is useful when subtitles are named with just the series name
and episode number, but the videos have been renamed with full episode titles.

Typical scenario::

    Video:    'Runway de Waratte - S01E01 - Koreha Kun no Monogatari.mkv'
    Subtitle: 'Runway de Waratte - S01E01.ass'
    Result:   'Runway de Waratte - S01E01 - Koreha Kun no Monogatari.ass'

Public API
----------
* :func:`scan_subtitle_matches`   — find all subtitle/video match plans
* :func:`preview_subtitle_renames` — preview what would be renamed
* :func:`execute_subtitle_renames` — rename matched subtitles
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from renamer.config import get_logger
from renamer.parsers import EpisodeNumberParser
from renamer.subtitles import _split_subtitle_stem

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SubtitleMatch:
    """A single subtitle-to-video match plan."""

    subtitle_path: Path
    video_path: Path
    new_subtitle_name: str
    language_tag: str | None = None

    @property
    def subtitle_name(self) -> str:
        return self.subtitle_path.name

    @property
    def video_name(self) -> str:
        return self.video_path.name

    @property
    def already_matches(self) -> bool:
        """True if the subtitle is already named correctly."""
        # Build the expected new name and compare
        expected = self.new_subtitle_name
        return self.subtitle_path.name == expected


@dataclass
class SubtitleScanResult:
    """Result of scanning a directory for subtitle matches."""

    matches: list[SubtitleMatch] = field(default_factory=list)
    unmatched_subtitles: list[Path] = field(default_factory=list)
    unmatched_videos: list[Path] = field(default_factory=list)

    @property
    def total_subtitles(self) -> int:
        return len(self.matches) + len(self.unmatched_subtitles)

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    @property
    def already_correct_count(self) -> int:
        return sum(1 for m in self.matches if m.already_matches)

    @property
    def needs_rename_count(self) -> int:
        return sum(1 for m in self.matches if not m.already_matches)


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------

# Pattern to extract S01E01 from a filename stem
_SEASON_EP_RE = re.compile(
    r"[Ss](\d{1,2})\s*[Ee][Pp]?\s*(\d{1,4})",
    re.IGNORECASE,
)


def _extract_season_episode(stem: str) -> tuple[int, int] | None:
    """
    Extract (season, episode) from a filename stem using S01E01 pattern.

    Falls back to the EpisodeNumberParser for other patterns.
    """
    m = _SEASON_EP_RE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Try EpisodeNumberParser for other formats (e.g., just episode number)
    season, ep = EpisodeNumberParser.parse_season_episode(stem)
    if season is not None and ep is not None:
        return season, ep
    return None


def scan_subtitle_matches(
    media_dir: Path,
    video_extensions: Sequence[str],
    subtitle_extensions: Sequence[str],
    scan_recursive: bool = False,
) -> SubtitleScanResult:
    """
    Scan a directory and match subtitle files to video files.

    Matching is done by comparing the (season, episode) number extracted
    from both the subtitle and video filenames.  When a match is found,
    the subtitle's new name is computed from the video's stem, preserving
    the subtitle's language tag and extension.

    Parameters
    ----------
    media_dir:
        Directory to scan.
    video_extensions:
        Recognised video file extensions (e.g. ``(".mkv", ".mp4")``).
    subtitle_extensions:
        Recognised subtitle file extensions (e.g. ``(".srt", ".ass")``).
    scan_recursive:
        If ``True``, scan subdirectories recursively.

    Returns
    -------
    SubtitleScanResult
        Match plans, unmatched subtitles, and unmatched videos.
    """
    video_exts = set(e.lower() for e in video_extensions)
    sub_exts = set(e.lower() for e in subtitle_extensions)

    # Collect files
    if scan_recursive:
        all_files = sorted(p for p in media_dir.rglob("*") if p.is_file())
    else:
        all_files = sorted(p for p in media_dir.iterdir() if p.is_file())

    videos: list[Path] = [f for f in all_files if f.suffix.lower() in video_exts]
    subtitles: list[Path] = [f for f in all_files if f.suffix.lower() in sub_exts]

    # Build a map: (season, episode) -> video Path
    # If multiple videos share the same (season, episode), we use the first one.
    video_map: dict[tuple[int, int], Path] = {}
    for v in videos:
        se = _extract_season_episode(v.stem)
        if se:
            if se not in video_map:
                video_map[se] = v
            else:
                log.debug(
                    "Duplicate S%02dE%02d video: %s (already have %s)",
                    se[0],
                    se[1],
                    v.name,
                    video_map[se].name,
                )

    # Also build a map of video stems for direct matching (when subtitle stem
    # already matches a video stem — these are already handled by the existing
    # subtitles module, so we skip them here unless the name differs)
    video_stems: dict[str, Path] = {}
    for v in videos:
        video_stems[v.stem.lower()] = v

    result = SubtitleScanResult()
    matched_sub_paths: set[Path] = set()

    for sub in subtitles:
        # Split subtitle stem into base + language tag
        base_stem, lang_tag = _split_subtitle_stem(sub.stem)
        sub_ext = sub.suffix.lower()

        # First, try S01E01 matching
        se = _extract_season_episode(base_stem)

        if se and se in video_map:
            video = video_map[se]
            # Compute new subtitle name: video stem + language tag + sub extension
            new_stem = f"{video.stem}{lang_tag or ''}"
            new_name = f"{new_stem}{sub_ext}"

            match = SubtitleMatch(
                subtitle_path=sub,
                video_path=video,
                new_subtitle_name=new_name,
                language_tag=lang_tag,
            )
            result.matches.append(match)
            matched_sub_paths.add(sub)
            continue

        # No S01E01 match — try absolute episode number matching
        # This handles subtitles named like "Series - 01.ass"
        abs_ep = EpisodeNumberParser.parse(base_stem)
        if abs_ep is not None:
            # Look for a video with this absolute episode in season 1
            # (most common case for single-season anime)
            found = False
            for (_s, e), video in video_map.items():
                if e == abs_ep:
                    new_stem = f"{video.stem}{lang_tag or ''}"
                    new_name = f"{new_stem}{sub_ext}"
                    match = SubtitleMatch(
                        subtitle_path=sub,
                        video_path=video,
                        new_subtitle_name=new_name,
                        language_tag=lang_tag,
                    )
                    result.matches.append(match)
                    matched_sub_paths.add(sub)
                    found = True
                    break
            if found:
                continue

        # No match found
        result.unmatched_subtitles.append(sub)

    # Track unmatched videos (videos with no subtitle)
    matched_video_paths: set[Path] = {m.video_path for m in result.matches}
    for v in videos:
        if v not in matched_video_paths:
            result.unmatched_videos.append(v)

    return result


# ---------------------------------------------------------------------------
# Preview & Execute
# ---------------------------------------------------------------------------


def preview_subtitle_renames(scan_result: SubtitleScanResult) -> None:
    """
    Print a human-readable preview of subtitle rename plans.

    Shows matched subtitles (with their new names), already-correct
    subtitles, and unmatched items.
    """
    matches = scan_result.matches
    unmatched_subs = scan_result.unmatched_subtitles
    unmatched_vids = scan_result.unmatched_videos

    if not matches and not unmatched_subs:
        print("\n  No subtitle files found in this directory.")
        return

    print(f"\n  {'=' * 60}")
    print("  SUBTITLE RENAME PREVIEW")
    print(f"  {'=' * 60}")

    # Show matches that need renaming
    needs_rename = [m for m in matches if not m.already_matches]
    already_ok = [m for m in matches if m.already_matches]

    if needs_rename:
        print(f"\n  Subtitles to rename ({len(needs_rename)}):")
        print(f"  {'─' * 60}")
        for m in needs_rename:
            lang = f"  [{m.language_tag}]" if m.language_tag else ""
            print(f"    {m.subtitle_name}")
            print(f"  -> {m.new_subtitle_name}{lang}")

    if already_ok:
        print(f"\n  Already correctly named ({len(already_ok)}):")
        print(f"  {'─' * 60}")
        for m in already_ok:
            print(f"    {m.subtitle_name}")

    if unmatched_subs:
        print(f"\n  Unmatched subtitles — no corresponding video ({len(unmatched_subs)}):")
        print(f"  {'─' * 60}")
        for s in unmatched_subs:
            print(f"    {s.name}")

    if unmatched_vids:
        print(f"\n  Videos without subtitles ({len(unmatched_vids)}):")
        print(f"  {'─' * 60}")
        for v in unmatched_vids:
            print(f"    {v.name}")

    print(f"\n  {'=' * 60}")
    total = scan_result.matched_count
    needs = scan_result.needs_rename_count
    ok = scan_result.already_correct_count
    print(
        f"  Total: {total} matched ({needs} to rename, {ok} already correct), "
        f"{len(unmatched_subs)} unmatched subtitles"
    )
    print(f"  {'=' * 60}")


def execute_subtitle_renames(
    scan_result: SubtitleScanResult,
    dry_run: bool = True,
) -> list[Path]:
    """
    Execute subtitle renames based on scan results.

    Parameters
    ----------
    scan_result:
        Result from :func:`scan_subtitle_matches`.
    dry_run:
        If ``True``, only log what would happen — no files are modified.

    Returns
    -------
    list[Path]
        New paths of successfully renamed (or would-be-renamed) subtitles.
    """
    renamed: list[Path] = []

    for match in scan_result.matches:
        if match.already_matches:
            log.debug("Already correctly named: %s", match.subtitle_name)
            renamed.append(match.subtitle_path)
            continue

        sub_path = match.subtitle_path
        new_path = sub_path.parent / match.new_subtitle_name

        log.info(
            "%s Subtitle: %s  ->  %s",
            "[DRY RUN]" if dry_run else "[LIVE]",
            sub_path.name,
            match.new_subtitle_name,
        )

        if dry_run:
            renamed.append(new_path)
            continue

        # Prevent overwriting existing files
        if new_path.exists() and new_path.resolve() != sub_path.resolve():
            log.warning(
                "Target already exists: %s — skipping %s",
                new_path,
                sub_path,
            )
            continue

        try:
            sub_path.rename(new_path)
            renamed.append(new_path)
        except OSError as exc:
            log.error("Failed to rename subtitle %r: %s", sub_path.name, exc)

    return renamed
