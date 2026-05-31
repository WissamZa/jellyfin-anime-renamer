"""
renamer.subtitles
=================
Subtitle matching and renaming logic.

When a video file is renamed, any accompanying subtitle files must be
renamed to match so that media players (and Jellyfin) can find them.

Typical anime subtitle filenames::

    [SubGroup] Series - 01 [1080p].ass
    [SubGroup] Series - 01 [1080p].en.srt
    [SubGroup] Series - 01.ja.ass
    Series - S01E01 - Title.ass
    Series - S01E01 - Title.en.srt

Matching strategy
-----------------
For each video file being renamed, we look for subtitle files in the
same directory whose **stem** (filename without extension) matches the
video's original stem — allowing for an optional language tag suffix
such as ``.en``, ``.ja``, ``.zh-hans``, etc.

When a match is found, the subtitle is renamed so its stem matches the
*new* video stem, preserving the language tag and subtitle extension.

Example::

    Video:  "[Erai] Frieren - 01 [1080p].mkv"
            -> "Frieren - S01E01 - A Journey's End .mkv"

    Sub:    "[Erai] Frieren - 01 [1080p].en.srt"
            -> "Frieren - S01E01 - A Journey's End .en.srt"

Public API
----------
* :func:`find_matching_subtitles`  — find subtitles that match a video file
* :func:`rename_subtitle`          — rename a single subtitle alongside its video
* :func:`process_subtitles_for_video` — full match + rename for one video
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from renamer.config import Config, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Language-tag detection
# ---------------------------------------------------------------------------

# Matches common subtitle language suffixes before the final extension.
# Examples:  ".en", ".ja", ".zh-hans", ".pt-br", ".en-US", ".ar"
# These appear between the "base" stem and the subtitle extension:
#   filename.en.srt   ->  base="filename",  lang=".en",  ext=".srt"
_LANG_TAG_RE = re.compile(
    r"""
    \.                      # dot before language tag
    (?:
        [a-z]{2,3}          # ISO 639-1/2/3 code (en, ja, zho)
        (?:[-][a-z]{2,4})?  # optional script/region (-US, -hans, -latn)
    )
    $                       # end of string (before subtitle ext is stripped)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _split_subtitle_stem(stem: str) -> tuple[str, str | None]:
    """
    Split a subtitle stem into (base_stem, language_tag).

    If no language tag is found, returns ``(stem, None)``.

    >>> _split_subtitle_stem("[Erai] Frieren - 01 [1080p].en")
    ('[Erai] Frieren - 01 [1080p]', '.en')
    >>> _split_subtitle_stem("Episode 01")
    ('Episode 01', None)
    >>> _split_subtitle_stem("Episode 01.zh-hans")
    ('Episode 01', '.zh-hans')
    """
    m = _LANG_TAG_RE.search(stem)
    if m:
        base = stem[: m.start()]
        lang = stem[m.start() :]  # includes the leading dot
        return base, lang
    return stem, None


# ---------------------------------------------------------------------------
# Finding matching subtitles
# ---------------------------------------------------------------------------


def find_matching_subtitles(
    video_path: Path,
    media_dir: Path,
    subtitle_extensions: Sequence[str],
    search_recursive: bool = False,
) -> list[Path]:
    """
    Find subtitle files that match *video_path*.

    A subtitle matches when its base stem (without language tag) equals
    the video file's stem.  The search is limited to the same directory
    as the video file by default.

    Parameters
    ----------
    video_path:
        Path to the video file.
    media_dir:
        Root media directory (used for recursive search).
    subtitle_extensions:
        Tuple/list of subtitle extensions to look for (e.g. ``.srt``, ``.ass``).
    search_recursive:
        If ``True``, search all of *media_dir* recursively instead of only
        the video's parent directory.  Useful for multi-file torrents.

    Returns
    -------
    list[Path]
        Paths to subtitle files that match the video, sorted for
        deterministic processing order.
    """
    video_stem = video_path.stem
    search_dir = video_path.parent

    # Collect candidate subtitle files
    if search_recursive:
        candidates = (
            p for p in media_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in subtitle_extensions
        )
    else:
        candidates = (
            p for p in search_dir.iterdir()
            if p.is_file() and p.suffix.lower() in subtitle_extensions
        )

    matches: list[Path] = []
    for sub_path in candidates:
        sub_stem, _lang = _split_subtitle_stem(sub_path.stem)
        if sub_stem == video_stem:
            matches.append(sub_path)

    return sorted(matches)


# ---------------------------------------------------------------------------
# Renaming a single subtitle
# ---------------------------------------------------------------------------


def rename_subtitle(
    sub_path: Path,
    new_video_stem: str,
    dest_dir: Path,
    dry_run: bool = True,
    rename_fn: Callable[[Path, Path], None] | None = None,
) -> Path | None:
    """
    Rename a subtitle file to match a renamed video file.

    The subtitle's new name is built from the video's new stem,
    preserving the subtitle's language tag and extension.

    Parameters
    ----------
    sub_path:
        Current path of the subtitle file.
    new_video_stem:
        The new stem (filename without extension) of the video file.
    dest_dir:
        Destination directory (usually the season folder).
    dry_run:
        If ``True``, no files are modified.
    rename_fn:
        Optional custom rename function ``(old_path, new_path) -> None``.
        Used by qBittorrent hook to move files through the API.
        Falls back to ``Path.rename()`` when not provided.

    Returns
    -------
    Optional[Path]
        The new path of the subtitle file, or ``None`` on failure.
    """
    _, lang_tag = _split_subtitle_stem(sub_path.stem)
    ext = sub_path.suffix.lower()

    # Build new subtitle name:  new_video_stem + [lang_tag] + ext
    new_stem = f"{new_video_stem}{lang_tag or ''}"
    new_name = f"{new_stem}{ext}"
    new_path = dest_dir / new_name

    # Already named correctly
    if sub_path.resolve() == new_path.resolve():
        log.debug("Subtitle already correctly named: %s", sub_path.name)
        return new_path

    log.info(
        "Subtitle: %s  ->  %s",
        sub_path.resolve(),
        new_path.resolve(),
    )

    if dry_run:
        return new_path

    # Prevent overwriting pre-existing files at the destination
    if new_path.exists() and new_path.resolve() != sub_path.resolve():
        log.warning(
            "Subtitle target already exists: %s — skipping %s",
            new_path, sub_path,
        )
        return None

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if rename_fn:
            rename_fn(sub_path, new_path)
        else:
            sub_path.rename(new_path)
        return new_path
    except Exception as exc:
        log.error("Failed to rename subtitle %r: %s", sub_path.name, exc)
        return None


# ---------------------------------------------------------------------------
# High-level: process all subtitles for one video rename
# ---------------------------------------------------------------------------


def process_subtitles_for_video(
    original_video_path: Path,
    new_video_stem: str,
    dest_dir: Path,
    cfg: Config,
    dry_run: bool = True,
    rename_fn: Callable[[Path, Path], None] | None = None,
) -> list[Path]:
    """
    Find and rename all subtitle files that match a renamed video.

    This is the main entry point called by :class:`AnimeRenamer` after
    a video file has been (or will be) renamed.

    Parameters
    ----------
    original_video_path:
        The *original* path of the video file (before rename).
    new_video_stem:
        The new stem (filename without extension) of the renamed video.
    dest_dir:
        Destination directory for the subtitle (same as the video).
    cfg:
        Configuration (used for ``subtitle_extensions``).
    dry_run:
        If ``True``, no files are modified.
    rename_fn:
        Optional custom rename function for qBittorrent integration.

    Returns
    -------
    list[Path]
        New paths of successfully renamed subtitles.
    """
    subtitle_exts = cfg.subtitle_extensions
    if not subtitle_exts:
        return []

    matches = find_matching_subtitles(
        original_video_path,
        cfg.media_dir,
        subtitle_exts,
    )

    if not matches:
        return []

    renamed: list[Path] = []
    for sub_path in matches:
        result = rename_subtitle(
            sub_path=sub_path,
            new_video_stem=new_video_stem,
            dest_dir=dest_dir,
            dry_run=dry_run,
            rename_fn=rename_fn,
        )
        if result:
            renamed.append(result)

    return renamed
