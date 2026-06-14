"""
renamer.hash_organizer
======================
Hash-first multi-series organizer for mixed anime folders.

This module implements the core feature from anipyrenamer: scan a folder
containing mixed anime files from multiple series, identify each file by
its ED2K hash via AniDB, group files by anime, and organize them into
proper series/season folder structures.

When AniDB is unavailable (no credentials, auth failure, cache miss),
falls back to **filename-based identification**: parses the series name
and episode number from each file's name, then resolves the proper romaji
title via AniList and TMDB search.

Flow
----
1. SCAN       — Find all video files recursively in the target directory
2. HASH       — Compute ED2K hash + file size for each file
3. IDENTIFY   — Look up each (size, ed2k) via cache → AniDB API
4. FALLBACK   — For unidentified files, parse series + episode from filename
5. RESOLVE    — Search AniList/TMDB for proper romaji title & episode titles
6. GROUP      — Group files by anime ID (aid) or series name
7. PLAN       — Build a rename plan for each anime series
8. EXECUTE    — Move files into organized folder structure

Example
-------
Before (all in one folder)::

    /download/Unsorted/
      [SubGroup] Jujutsu Kaisen - 01 [1080p].mkv
      [SubGroup] Jujutsu Kaisen - 02 [1080p].mkv
      [Another] One Punch Man - 05 [720p].mkv
      random_episode.mkv

After::

    /download/Unsorted/
      Jujutsu Kaisen/
        Season 01/
          Jujutsu Kaisen - S01E01 - Ryomen Sukuna.mkv
          Jujutsu Kaisen - S01E02 - For Myself.mkv
      One Punch Man/
        Season 01/
          One Punch Man - S01E05 - The Ultimate Master.mkv
      _unidentified/
        random_episode.mkv
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from renamer.config import Config, get_logger
from renamer.ed2k import compute_ed2k, get_file_size
from renamer.providers.anidb import AniDBClient
from renamer.providers.anidb_cache import AniDBCache, AniDBFileInfo
from renamer.renamer import sanitize_name
from renamer.subtitles import find_matching_subtitles, rename_subtitle

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FilenameInfo:
    """Series and episode info extracted from a filename."""

    series_name: str
    season: int
    episode: int
    episode_title: str = ""
    # Filled in after AniList/TMDB resolution
    resolved_romaji: str = ""
    resolved_english: str = ""


@dataclasses.dataclass
class HashedFile:
    """A video file with its ED2K hash computed."""

    path: Path
    size: int
    ed2k: str
    info: AniDBFileInfo | None = None
    filename_info: FilenameInfo | None = None
    identified_method: str = ""  # "anidb", "filename", or ""


@dataclasses.dataclass
class ResolvedSeries:
    """Series info resolved from a filename via AniList/TMDB search."""

    search_name: str  # Original extracted name from filename
    romaji_title: str = ""  # Best romaji title from AniList/TMDB
    english_title: str = ""  # English title from AniList/TMDB
    anilist_id: int | None = None
    tmdb_id: int | None = None
    episode_titles: dict[int, str] = dataclasses.field(default_factory=dict)
    source: str = ""  # "anilist", "tmdb", or ""


@dataclasses.dataclass
class OrganizePlan:
    """Rename plan for a single anime series."""

    anime_title: str
    anime_title_english: str
    aid: int
    files: list[HashedFile]
    dest_folder: Path
    identification_method: str = "anidb"  # "anidb" or "filename"

    @property
    def display_title(self) -> str:
        return self.anime_title or self.anime_title_english or f"AniDB-{self.aid}"


@dataclasses.dataclass
class OrganizeResult:
    """Result of the hash-organize operation."""

    total_files: int = 0
    identified: int = 0
    identified_by_hash: int = 0
    identified_by_filename: int = 0
    unidentified: int = 0
    series_count: int = 0
    moved: int = 0
    errors: int = 0
    skipped: int = 0
    plans: list[OrganizePlan] = dataclasses.field(default_factory=list)
    unidentified_files: list[HashedFile] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Filename-based series extraction
# ---------------------------------------------------------------------------

# Regex to strip leading subgroup tags: [SubGroup] ...
_SUBGROUP_RE = re.compile(r"^\[[^\]]+\]\s*")

# Regex to strip trailing codec/resolution/quality brackets
_CODEC_BRACKET_RE = re.compile(
    r"\s*\[(?:"
    r"[^\]]*?(?:1080p|720p|480p|2160p|4K|HEVC|x264|x265|AV1|10bit|8bit|Hi10|"
    r"WEB|BD|DVD|Multi-Sub|MultiSub|Dual-Audio|Batch|Complete|CR|WEBRip|WEB-DL|AAC|FLAC)"
    r"[^\]]*?)\]",
    re.IGNORECASE,
)

# Regex to strip language/source/hash bracket tags
_LANG_HASH_RE = re.compile(
    r"\s*\[(?:"
    r"[A-Z]{2,4}"  # 2-4 letter uppercase codes: JPN, ENG, JA
    r"|www"
    r"|v\d+"  # version tags: v2, v3
    r"|[0-9A-Fa-f]{6,8}"  # 6-8 char hex hash: 8B6A5238, D8F2303A
    r")\s*\]",
    re.IGNORECASE,
)

# Regex to strip language in parentheses: (JA), (EN)
_LANG_PAREN_RE = re.compile(r"\s*\([A-Z]{2,4}\)\s*$", re.IGNORECASE)

# Episode number patterns (ordered by specificity)
_SEASON_EP_RE = re.compile(r"\s*[-–]\s*S(\d{1,2})\s*E(\d{1,4})", re.IGNORECASE)
_DASH_EP_RE = re.compile(r"\s*[-–]\s*(\d{2,4})(?:v\d+)?\s*(?:$|\.|\[)")
_BRACKET_EP_RE = re.compile(r"\[(\d{2,4})\]")


def extract_series_from_filename(filename: str) -> FilenameInfo | None:
    """
    Extract series name, season, and episode number from an anime filename.

    Handles common patterns like:
        [Judas] Medaka Kuroiwa - S01E01.mkv
        Lv2 kara Cheat datta Motoyuusha Kouho no Mattari Isekai Life - S01E01.mkv
        [Erai-raws] Isekai Quartet 3 - 02 [1080p].mkv
        [Judas] Gachiakuta - S01E07.mkv
        Isekai Mokushiroku - S01E09 - Title.mkv

    Returns FilenameInfo if a series name + episode can be extracted, None otherwise.
    """
    stem = Path(filename).stem

    # Strip leading subgroup tag
    cleaned = _SUBGROUP_RE.sub("", stem)

    # Try S01E01 pattern first (most specific)
    m = _SEASON_EP_RE.search(cleaned)
    if m:
        series_part = cleaned[: m.start()].strip()
        season = int(m.group(1))
        episode = int(m.group(2))
        # Check for episode title after the S01E01 match
        after = cleaned[m.end() :].strip()
        ep_title = ""
        if after.startswith("-"):
            ep_title = after[1:].strip()
        elif after:
            ep_title = after

        # Clean the series name
        series_name = _clean_extracted_name(series_part)
        if series_name:
            return FilenameInfo(
                series_name=series_name,
                season=season,
                episode=episode,
                episode_title=_clean_extracted_name(ep_title) if ep_title else "",
            )

    # Try bracket episode: [SubGroup] Series - 02 [...].mkv
    # First strip all bracket tags, then try dash-episode
    no_brackets = cleaned
    prev = None
    while prev != no_brackets:
        prev = no_brackets
        no_brackets = _CODEC_BRACKET_RE.sub("", no_brackets)
    prev = None
    while prev != no_brackets:
        prev = no_brackets
        no_brackets = _LANG_HASH_RE.sub("", no_brackets)
    no_brackets = _LANG_PAREN_RE.sub("", no_brackets).strip()

    # Try dash-episode: Series - 02
    m = _DASH_EP_RE.search(no_brackets)
    if m:
        series_part = no_brackets[: m.start()].strip()
        episode = int(m.group(1))
        series_name = _clean_extracted_name(series_part)
        if series_name:
            return FilenameInfo(
                series_name=series_name,
                season=1,
                episode=episode,
            )

    # Try bracket episode: [02] or [E02]
    m = _BRACKET_EP_RE.search(cleaned)
    if m:
        ep_str = m.group(1)
        # Avoid matching resolution-like brackets [1080] or year [2024]
        ep_val = int(ep_str)
        if ep_val < 100:  # Episode numbers are typically < 100
            series_part = cleaned[: m.start()].strip()
            series_name = _clean_extracted_name(series_part)
            if series_name:
                return FilenameInfo(
                    series_name=series_name,
                    season=1,
                    episode=ep_val,
                )

    return None


def _clean_extracted_name(name: str) -> str:
    """Clean a series name extracted from a filename."""
    # Strip remaining bracket tags
    prev = None
    while prev != name:
        prev = name
        name = _CODEC_BRACKET_RE.sub("", name)
    prev = None
    while prev != name:
        prev = name
        name = _LANG_HASH_RE.sub("", name)
    # Strip trailing dashes, dots, spaces
    name = name.strip(" .-_–—")
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name)
    return name


# ---------------------------------------------------------------------------
# Core organizer
# ---------------------------------------------------------------------------


class HashOrganizer:
    """
    Hash-first multi-series organizer with filename fallback.

    Scans a directory for video files, identifies each by ED2K hash
    via AniDB, then falls back to filename parsing for files that
    couldn't be identified by hash. For filename-identified files,
    searches AniList and TMDB to resolve the proper romaji title
    and episode titles.

    Parameters
    ----------
    cfg : Config
        Runtime configuration (uses AniDB credentials from cfg).
    media_dir : Path
        The directory to scan and organize.
    """

    # Folder name for files that couldn't be identified
    UNIDENTIFIED_FOLDER = "_unidentified"

    def __init__(self, cfg: Config, media_dir: Path | None = None) -> None:
        self._cfg = cfg
        self._media_dir = media_dir or cfg.media_dir
        self._cache = AniDBCache()
        self._client: AniDBClient | None = None
        self._result = OrganizeResult()
        # Cache resolved series names to avoid duplicate API calls
        self._resolved_cache: dict[str, ResolvedSeries] = {}

    # ── Public API ────────────────────────────────────────────

    def scan_and_identify(self) -> OrganizeResult:
        """
        Phase 1-5: Scan, hash, identify via AniDB, fallback to filename,
        then resolve titles via AniList/TMDB.

        Returns an OrganizeResult with the identification results
        but does NOT move any files.
        """
        media_dir = self._media_dir
        if not media_dir.is_dir():
            log.error("Hash-organize: directory does not exist: %s", media_dir)
            return self._result

        # Step 1: Scan for video files
        video_files = sorted(
            p
            for p in media_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in self._cfg.video_extensions
        )

        if not video_files:
            log.info("Hash-organize: no video files found in %s", media_dir)
            return self._result

        # Filter out files that are already inside organized folders
        # (e.g. inside "Season 01/" subfolders) to avoid re-processing
        video_files = self._filter_organized(video_files)

        self._result.total_files = len(video_files)
        log.info("Hash-organize: found %d video file(s) in %s", len(video_files), media_dir)

        # Step 2: Compute ED2K hashes
        hashed: list[HashedFile] = []
        for i, f in enumerate(video_files, 1):
            print(f"  Hashing [{i}/{len(video_files)}]: {f.name} … ", end="", flush=True)
            try:
                size = get_file_size(f)
                ed2k = compute_ed2k(f)
                hf = HashedFile(path=f, size=size, ed2k=ed2k)
                hashed.append(hf)
                print(f"OK ({ed2k[:8]}…)")
            except Exception as exc:
                log.warning("Could not hash %s: %s", f.name, exc)
                print(f"FAILED ({exc})")
                self._result.errors += 1

        if not hashed:
            return self._result

        # Step 3: Identify each file via cache → AniDB API
        client = self._get_client()
        offline = self._cfg.anidb_offline

        for i, hf in enumerate(hashed, 1):
            # Check cache first
            info = self._cache.get(hf.size, hf.ed2k)

            if info is None and not offline and client:
                # Cache miss — query AniDB API
                print(f"  Looking up [{i}/{len(hashed)}]: {hf.path.name} … ", end="", flush=True)
                api_info = client.file_lookup(hf.size, hf.ed2k)
                if api_info:
                    self._cache.set(api_info)
                    info = api_info
                    print(
                        f"OK -> {info.anime_title_romaji or info.anime_title_english} E{info.episode_number}"
                    )
                else:
                    print("not found on AniDB")
            elif info:
                print(
                    f"  Cache hit [{i}/{len(hashed)}]: {hf.path.name} -> {info.anime_title_romaji or info.anime_title_english} E{info.episode_number}"
                )

            if info and info.aid:
                hf.info = info
                hf.identified_method = "anidb"
                self._result.identified += 1
                self._result.identified_by_hash += 1
            else:
                # Step 4: Fallback to filename-based identification
                fname_info = extract_series_from_filename(hf.path.name)
                if fname_info and fname_info.series_name and fname_info.episode:
                    hf.filename_info = fname_info
                    hf.identified_method = "filename"
                    self._result.identified += 1
                    self._result.identified_by_filename += 1
                    log.info(
                        "Filename fallback: %s -> %s S%02dE%02d",
                        hf.path.name,
                        fname_info.series_name,
                        fname_info.season,
                        fname_info.episode,
                    )
                else:
                    self._result.unidentified += 1
                    self._result.unidentified_files.append(hf)

        # Disconnect AniDB client
        if client:
            client.disconnect()

        # Step 5: Resolve series titles via AniList/TMDB for filename-identified files
        self._resolve_all_series_titles(hashed)

        # Step 6: Group by anime/series
        self._result.plans = self._build_plans(hashed)
        self._result.series_count = len(self._result.plans)

        # Print summary of identification methods
        print("\n  Identification summary:")
        print(f"    AniDB hash : {self._result.identified_by_hash}")
        print(f"    Filename   : {self._result.identified_by_filename}")
        print(f"    Unidentified: {self._result.unidentified}")

        return self._result

    def preview(self) -> None:
        """Print a preview of the organization plan."""
        result = self._result
        if result.total_files == 0:
            print("\n  No video files found to organize.")
            return

        print(f"\n{'═' * 60}")
        print("  HASH-ORGANIZE PREVIEW")
        print(
            f"  Total: {result.total_files} | Identified: {result.identified} "
            f"(hash: {result.identified_by_hash}, filename: {result.identified_by_filename}) | "
            f"Unidentified: {result.unidentified} | Series: {result.series_count}"
        )
        print(f"{'═' * 60}")

        for plan in result.plans:
            method_tag = " [filename]" if plan.identification_method == "filename" else " [AniDB]"
            print(f"\n  📂 {plan.display_title}  ({len(plan.files)} file(s)){method_tag}")
            print(f"     Destination: {plan.dest_folder}")
            for hf in plan.files:
                if hf.identified_method == "anidb" and hf.info:
                    ep_num = hf.info.episode_number or "?"
                    ep_title = hf.info.episode_title_en or hf.info.episode_title_romaji or ""
                    new_name = self._format_filename(
                        plan.display_title, ep_num, ep_title, hf.path.suffix
                    )
                    print(f"     {hf.path.name}")
                    print(f"       -> Season 01/{new_name}")
                elif hf.identified_method == "filename" and hf.filename_info:
                    fi = hf.filename_info
                    # Use resolved romaji title for episode title if available
                    ep_title = fi.episode_title
                    if not ep_title:
                        # Check if we have resolved episode titles
                        resolved = self._resolved_cache.get(fi.series_name)
                        if resolved and resolved.episode_titles:
                            ep_title = resolved.episode_titles.get(fi.episode, "")
                    new_name = self._format_filename(
                        plan.display_title, str(fi.episode), ep_title, hf.path.suffix
                    )
                    print(f"     {hf.path.name}")
                    print(f"       -> Season {fi.season:02d}/{new_name}")
                else:
                    print(f"     {hf.path.name}  (no metadata)")

        if result.unidentified_files:
            dest = self._media_dir / self.UNIDENTIFIED_FOLDER
            print(f"\n  ❓ Unidentified ({len(result.unidentified_files)} file(s))")
            print(f"     Destination: {dest}")
            for hf in result.unidentified_files:
                print(f"     {hf.path.name}")
                print(f"       -> {self.UNIDENTIFIED_FOLDER}/{hf.path.name}")

        print()

    def execute(self, dry_run: bool = True) -> OrganizeResult:
        """
        Execute the organization plan.

        Moves identified files into series/season folders and
        unidentified files into an _unidentified/ folder.

        Parameters
        ----------
        dry_run : bool
            If True, only prints what would happen without moving files.

        Returns
        -------
        OrganizeResult
            Summary of the operation.
        """
        result = self._result

        if not result.plans and not result.unidentified_files:
            print("\n  Nothing to organize. Run scan_and_identify() first.")
            return result

        mode = "DRY RUN" if dry_run else "LIVE"
        print(f"\n  Organizing files ({mode}) …\n")

        session_history: dict[str, str] = {}

        # Process each anime series
        for plan in result.plans:
            # Determine season number and episode info for each file
            for hf in plan.files:
                if hf.identified_method == "anidb" and hf.info:
                    season_num = 1
                    ep_num = hf.info.episode_number or "0"
                    ep_title = hf.info.episode_title_en or hf.info.episode_title_romaji or ""
                elif hf.identified_method == "filename" and hf.filename_info:
                    fi = hf.filename_info
                    season_num = fi.season
                    ep_num = str(fi.episode)
                    ep_title = fi.episode_title
                    # Use resolved episode title if available
                    if not ep_title:
                        resolved = self._resolved_cache.get(fi.series_name)
                        if resolved and resolved.episode_titles:
                            ep_title = resolved.episode_titles.get(fi.episode, "")
                else:
                    continue

                season_folder = plan.dest_folder / f"Season {season_num:02d}"
                if not dry_run:
                    season_folder.mkdir(parents=True, exist_ok=True)

                new_name = self._format_filename(
                    plan.display_title, ep_num, ep_title, hf.path.suffix
                )
                dest_path = season_folder / new_name

                # Check if already at destination
                if hf.path.resolve() == dest_path.resolve():
                    result.skipped += 1
                    continue

                # Check if destination exists
                if dest_path.exists() and dest_path.resolve() != hf.path.resolve():
                    log.warning("Target already exists: %s — skipping %s", dest_path, hf.path.name)
                    result.skipped += 1
                    continue

                if dry_run:
                    rel_dest = season_folder.relative_to(self._media_dir) / new_name
                    print(f"  [DRY] {hf.path.name}")
                    print(f"         -> {rel_dest}")
                    result.moved += 1
                else:
                    try:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        hf.path.rename(dest_path)
                        session_history[str(dest_path.resolve())] = str(hf.path.resolve())
                        result.moved += 1
                        rel_dest = season_folder.relative_to(self._media_dir) / new_name
                        print(f"  ✓ {hf.path.name}  ->  {rel_dest}")

                        # Move matching subtitles
                        self._move_subtitles(
                            hf.path, dest_path, season_folder, dry_run, session_history
                        )
                    except Exception as exc:
                        log.error("Failed to move %s: %s", hf.path.name, exc)
                        result.errors += 1
                        print(f"  ✗ {hf.path.name}  FAILED: {exc}")

        # Move unidentified files
        if result.unidentified_files:
            unid_folder = self._media_dir / self.UNIDENTIFIED_FOLDER
            if not dry_run:
                unid_folder.mkdir(parents=True, exist_ok=True)

            for hf in result.unidentified_files:
                dest_path = unid_folder / hf.path.name

                if hf.path.resolve() == dest_path.resolve():
                    result.skipped += 1
                    continue

                if dest_path.exists() and dest_path.resolve() != hf.path.resolve():
                    result.skipped += 1
                    continue

                if dry_run:
                    print(f"  [DRY] {hf.path.name}  ->  {self.UNIDENTIFIED_FOLDER}/{hf.path.name}")
                    result.moved += 1
                else:
                    try:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        hf.path.rename(dest_path)
                        session_history[str(dest_path.resolve())] = str(hf.path.resolve())
                        result.moved += 1
                        print(f"  ? {hf.path.name}  ->  {self.UNIDENTIFIED_FOLDER}/{hf.path.name}")

                        # Move matching subtitles
                        self._move_subtitles(
                            hf.path, dest_path, unid_folder, dry_run, session_history
                        )
                    except Exception as exc:
                        log.error("Failed to move %s: %s", hf.path.name, exc)
                        result.errors += 1
                        print(f"  ✗ {hf.path.name}  FAILED: {exc}")

        # Save undo history
        if not dry_run and session_history:
            from renamer.history import RenameHistory

            history = RenameHistory(
                self._media_dir / "rename_history.json", media_dir=self._media_dir
            )
            history.save(session_history)
            log.info("Hash-organize undo log: %s", self._media_dir / "rename_history.json")

        # Print summary
        mode_label = "Would organize" if dry_run else "Organized"
        print(f"\n{'═' * 60}")
        print(f"  HASH-ORGANIZE COMPLETE ({mode})")
        print(
            f"  {mode_label}: {result.moved} | Skipped: {result.skipped} | "
            f"Errors: {result.errors} | Series: {result.series_count}"
        )
        print(f"{'═' * 60}\n")

        return result

    # ── Title resolution via AniList/TMDB ─────────────────────

    def _resolve_all_series_titles(self, hashed: list[HashedFile]) -> None:
        """
        Resolve all filename-extracted series names via AniList/TMDB.

        For each unique series name extracted from filenames, searches
        AniList (primary) and TMDB (fallback) to find the proper romaji
        title and episode titles. This ensures folders and filenames use
        the correct, full anime title instead of abbreviated names.
        """
        # Collect unique series names from filename-identified files
        unique_names: dict[str, list[HashedFile]] = {}
        for hf in hashed:
            if hf.identified_method == "filename" and hf.filename_info:
                name = hf.filename_info.series_name
                unique_names.setdefault(name, []).append(hf)

        if not unique_names:
            return

        print(f"\n  Resolving {len(unique_names)} series title(s) via AniList/TMDB …")

        for name, files in unique_names.items():
            # Check cache first
            if name in self._resolved_cache:
                resolved = self._resolved_cache[name]
            else:
                print(f"    Searching: '{name}' … ", end="", flush=True)
                resolved = self._resolve_series_title(name)
                self._resolved_cache[name] = resolved

                if resolved.romaji_title and resolved.romaji_title != name:
                    print(f"-> {resolved.romaji_title} [{resolved.source}]")
                elif resolved.english_title:
                    print(f"-> {resolved.english_title} [{resolved.source}]")
                else:
                    print("not found (using filename as-is)")

            # Update FilenameInfo with resolved titles
            for hf in files:
                fi = hf.filename_info
                if fi and resolved:
                    fi.resolved_romaji = resolved.romaji_title
                    fi.resolved_english = resolved.english_title
                    # Also update episode titles from provider if available
                    if resolved.episode_titles and fi.episode in resolved.episode_titles:
                        fi.episode_title = fi.episode_title or resolved.episode_titles[fi.episode]

    def _resolve_series_title(self, name: str) -> ResolvedSeries:
        """
        Search AniList and TMDB for the proper romaji title of a series.

        Resolution order:
          1. AniList (human-curated romaji — highest quality)
          2. TMDB (Japanese title → pykakasi romanisation)
          3. Fallback: use the extracted filename as-is

        Also fetches episode titles when possible.
        """
        result = ResolvedSeries(search_name=name)

        # --- Strategy 1: AniList (best romaji quality) ---
        try:
            from renamer.providers.anilist import AniListFetcher

            anilist = AniListFetcher()

            # Search for the anime and get romaji title
            romaji = anilist.find_romaji(name)
            if romaji:
                result.romaji_title = romaji
                result.anilist_id = anilist._id
                result.source = "anilist"
                log.info(
                    "AniList resolved '%s' -> romaji='%s' (id=%s)",
                    name,
                    romaji,
                    result.anilist_id,
                )

                # Also get English title
                media = anilist._gql(
                    anilist.SERIES_QUERY,
                    {"search": name},
                )
                if media:
                    titles = media.get("title", {})
                    result.english_title = titles.get("english", "") or ""

                # Fetch episode titles
                if result.anilist_id:
                    ep_map = anilist.fetch()
                    if ep_map:
                        result.episode_titles = {
                            ep_info.episode: ep_info.title for ep_info in ep_map.values()
                        }
                        log.info(
                            "AniList: got %d episode titles for '%s'",
                            len(result.episode_titles),
                            romaji,
                        )

                return result

        except Exception as exc:
            log.warning("AniList search failed for '%s': %s", name, exc)

        # --- Strategy 2: TMDB (Japanese title → romanised) ---
        try:
            if self._cfg.tmdb_api_key:
                from renamer.providers.tmdb import TMDBSearch

                search = TMDBSearch(self._cfg.tmdb_api_key)
                found = search.find(name)
                if found:
                    tmdb_id, romaji = found
                    result.romaji_title = romaji
                    result.tmdb_id = tmdb_id
                    result.source = "tmdb"
                    log.info(
                        "TMDB resolved '%s' -> romaji='%s' (id=%d)",
                        name,
                        romaji,
                        tmdb_id,
                    )

                    # Also fetch English name from TMDB
                    from renamer.providers.tmdb import _TMDBGetHelper

                    helper = _TMDBGetHelper()
                    show_data = helper._get(
                        f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                        {"api_key": self._cfg.tmdb_api_key, "language": "en-US"},
                    )
                    if show_data:
                        result.english_title = show_data.get("name", "")

                    # Fetch episode titles from TMDB
                    try:
                        from renamer.providers.tmdb import TMDBFetcher

                        fetcher = TMDBFetcher(
                            api_key=self._cfg.tmdb_api_key,
                            series_id=tmdb_id,
                            cfg=self._cfg,
                        )
                        ep_map = fetcher.fetch()
                        if ep_map:
                            result.episode_titles = {
                                ep_info.episode: ep_info.title for ep_info in ep_map.values()
                            }
                            log.info(
                                "TMDB: got %d episode titles for '%s'",
                                len(result.episode_titles),
                                romaji,
                            )
                    except Exception as exc:
                        log.warning("TMDB episode fetch failed for '%s': %s", name, exc)

                    return result

        except Exception as exc:
            log.warning("TMDB search failed for '%s': %s", name, exc)

        # --- Strategy 3: Try with cleaned/alternative search terms ---
        # Sometimes the filename extraction gives a slightly different name
        alt_names = self._generate_search_variants(name)
        for alt_name in alt_names:
            if alt_name == name:
                continue
            try:
                from renamer.providers.anilist import AniListFetcher

                anilist = AniListFetcher()
                romaji = anilist.find_romaji(alt_name)
                if romaji:
                    result.romaji_title = romaji
                    result.anilist_id = anilist._id
                    result.source = "anilist-alt"
                    log.info(
                        "AniList (alt search '%s') resolved -> romaji='%s'",
                        alt_name,
                        romaji,
                    )
                    # Get English title
                    media = anilist._gql(
                        anilist.SERIES_QUERY,
                        {"search": alt_name},
                    )
                    if media:
                        titles = media.get("title", {})
                        result.english_title = titles.get("english", "") or ""
                    # Fetch episode titles
                    if result.anilist_id:
                        ep_map = anilist.fetch()
                        if ep_map:
                            result.episode_titles = {
                                ep_info.episode: ep_info.title for ep_info in ep_map.values()
                            }
                    return result
            except Exception:
                pass

            # Also try TMDB with alt name
            if self._cfg.tmdb_api_key:
                try:
                    from renamer.providers.tmdb import TMDBSearch

                    search = TMDBSearch(self._cfg.tmdb_api_key)
                    found = search.find(alt_name)
                    if found:
                        tmdb_id, romaji = found
                        result.romaji_title = romaji
                        result.tmdb_id = tmdb_id
                        result.source = "tmdb-alt"
                        log.info(
                            "TMDB (alt search '%s') resolved -> romaji='%s'",
                            alt_name,
                            romaji,
                        )
                        return result
                except Exception:
                    pass

        # --- Fallback: use filename as-is ---
        log.info(
            "Could not resolve '%s' via AniList or TMDB — using filename as-is",
            name,
        )
        return result

    @staticmethod
    def _generate_search_variants(name: str) -> list[str]:
        """
        Generate alternative search terms for a series name.

        Handles common cases where the filename has a shortened or
        different version of the title:
          - "Madougushi" → also try full name variations
          - Remove trailing season indicators
          - Remove common abbreviations
        """
        variants = [name]

        # Remove trailing season indicators: "Series 2nd Season", "Series S2"
        cleaned = re.sub(r"\s+(?:2nd|3rd|4th|\d+th)\s+Season$", "", name, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+S\d+$", "", name)
        if cleaned != name:
            variants.append(cleaned)

        # Remove trailing "Season X" pattern
        cleaned = re.sub(r"\s+Season\s*\d*$", "", name, flags=re.IGNORECASE)
        if cleaned != name:
            variants.append(cleaned)

        # Remove trailing Roman numerals (e.g., "Series II", "Series III")
        cleaned = re.sub(r"\s+(?:II|III|IV|V|VI|VII|VIII|IX|X)$", "", name)
        if cleaned != name:
            variants.append(cleaned)

        return variants

    # ── Internal helpers ──────────────────────────────────────

    def _get_client(self) -> AniDBClient | None:
        """Create or return the AniDB client."""
        if self._client is not None:
            return self._client

        cfg = self._cfg
        if not cfg.anidb_username or not cfg.anidb_password:
            log.warning(
                "AniDB credentials not configured. "
                "Set ANIDB_USERNAME and ANIDB_PASSWORD in .env. "
                "Hash-organize will use filename-based identification."
            )
            return None

        self._client = AniDBClient(
            username=cfg.anidb_username,
            password=cfg.anidb_password,
            client_name=cfg.anidb_client or "jenameramer",
            client_ver=cfg.anidb_client_ver or 1,
            api_key=cfg.anidb_api_key or None,
        )

        # Try to connect
        if not self._client.connect():
            log.error(
                "AniDB: failed to authenticate — falling back to filename-based identification"
            )
            print(
                "\n  AniDB authentication failed. Falling back to filename-based identification …"
            )
            return None

        return self._client

    def _filter_organized(self, files: list[Path]) -> list[Path]:
        """
        Filter out files that are already in organized folder structures.

        A file is considered "already organized" if it's inside a
        "Season NN" subfolder, since that indicates it was previously
        processed by the renamer.
        """
        organized_keywords = {
            "season 01",
            "season 02",
            "season 03",
            "season 04",
            "season 05",
            "season 06",
            "season 07",
            "season 08",
            "season 09",
            "season 10",
            "specials",
        }
        filtered = []
        for f in files:
            try:
                rel = f.relative_to(self._media_dir)
                # Check if any parent folder looks like a season folder
                parts_lower = [p.lower() for p in rel.parts]
                if any(p in organized_keywords for p in parts_lower):
                    log.debug("Skipping already-organized file: %s", f.name)
                    continue
            except ValueError:
                pass
            filtered.append(f)
        return filtered

    def _build_plans(self, hashed: list[HashedFile]) -> list[OrganizePlan]:
        """Group identified files by anime/series and build OrganizePlans."""
        # Group AniDB-identified files by aid
        aid_groups: dict[int, list[HashedFile]] = {}
        # Group filename-identified files by series name
        name_groups: dict[str, list[HashedFile]] = {}

        for hf in hashed:
            if hf.identified_method == "anidb" and hf.info and hf.info.aid:
                aid_groups.setdefault(hf.info.aid, []).append(hf)
            elif hf.identified_method == "filename" and hf.filename_info:
                key = hf.filename_info.series_name
                name_groups.setdefault(key, []).append(hf)

        plans: list[OrganizePlan] = []

        # AniDB-identified groups
        for aid, files in sorted(aid_groups.items(), key=lambda x: -len(x[1])):
            first_info = files[0].info
            if not first_info:
                continue

            anime_title = (
                first_info.anime_title_romaji or first_info.anime_title_english or f"AniDB-{aid}"
            )
            anime_title_english = first_info.anime_title_english or ""

            folder_name = sanitize_name(anime_title)
            dest_folder = self._media_dir / folder_name

            if dest_folder.exists() and not dest_folder.is_dir():
                dest_folder = self._media_dir / f"{folder_name} ({aid})"

            plan = OrganizePlan(
                anime_title=anime_title,
                anime_title_english=anime_title_english,
                aid=aid,
                files=files,
                dest_folder=dest_folder,
                identification_method="anidb",
            )
            plans.append(plan)

        # Filename-identified groups — use resolved romaji titles
        for series_name, files in sorted(name_groups.items(), key=lambda x: -len(x[1])):
            # Use resolved romaji title if available
            resolved = self._resolved_cache.get(series_name)
            if resolved and resolved.romaji_title:
                anime_title = resolved.romaji_title
                anime_title_english = resolved.english_title
            else:
                anime_title = series_name
                anime_title_english = ""

            folder_name = sanitize_name(anime_title)
            dest_folder = self._media_dir / folder_name

            # Avoid collision with AniDB-created folders or existing folders
            if dest_folder.exists() and not dest_folder.is_dir():
                dest_folder = self._media_dir / f"{folder_name} (filename)"

            plan = OrganizePlan(
                anime_title=anime_title,
                anime_title_english=anime_title_english,
                aid=0,
                files=files,
                dest_folder=dest_folder,
                identification_method="filename",
            )
            plans.append(plan)

        return plans

    def _format_filename(self, series_title: str, ep_num: str, ep_title: str, ext: str) -> str:
        """Format a filename following the Jellyfin naming convention."""
        # Parse episode number (handle S1, C1, etc.)
        clean_ep = ep_num.strip()
        if clean_ep.upper().startswith("S") or clean_ep.upper().startswith("C"):
            # Special episode
            digits = re.search(r"\d+", clean_ep)
            if digits:
                return f"{series_title} - S00E{int(digits.group()):02d} - {ep_title}{ext}"
            return f"{series_title} - {ep_title}{ext}"

        try:
            ep_int = int(clean_ep)
        except ValueError:
            return f"{series_title} - {ep_title}{ext}"

        # Use the configured template format
        safe_title = sanitize_name(ep_title) if ep_title else ""
        return f"{series_title} - S01E{ep_int:02d} - {safe_title}{ext}"

    def _move_subtitles(
        self,
        original_video_path: Path,
        new_video_path: Path,
        dest_dir: Path,
        dry_run: bool,
        session_history: dict[str, str],
    ) -> None:
        """Find and move subtitle files alongside the renamed video."""
        subtitle_exts = self._cfg.subtitle_extensions
        if not subtitle_exts:
            return

        matches = find_matching_subtitles(
            original_video_path,
            self._media_dir,
            subtitle_exts,
            search_recursive=True,
        )

        new_stem = new_video_path.stem
        for sub_path in matches:
            result = rename_subtitle(
                sub_path=sub_path,
                new_video_stem=new_stem,
                dest_dir=dest_dir,
                dry_run=dry_run,
            )
            if result and not dry_run:
                session_history[str(result.resolve())] = str(sub_path.resolve())
