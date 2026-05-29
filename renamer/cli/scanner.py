"""
renamer.cli.scanner
===================
Media library scanning logic for Jellyfin Anime Renamer.
"""

import sqlite3

from renamer.config import Config
from renamer.parsers import EpisodeNumberParser, SpecialParser


def scan_library_to_db(cfg: Config) -> None:
    """
    Scan the main media folder for all anime subfolders and build
    a SQLite database with series, seasons, and episode info.

    Scans both video files and subtitle files so that subtitle
    associations are tracked in the database.

    The database is saved alongside the media folder as
    anime_library.db.
    """
    db_path = cfg.media_dir / "anime_library.db"
    parser = EpisodeNumberParser()
    special_parser = SpecialParser()

    print(f"\n  Scanning: {cfg.media_dir}")
    print(f"  Database: {db_path}")

    # Collect all media files recursively (video + subtitles)
    media_exts = cfg.all_media_extensions
    files = sorted(
        p for p in cfg.media_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in media_exts
    )

    # Separate video and subtitle files for different processing
    video_files = [f for f in files if f.suffix.lower() in cfg.video_extensions]
    subtitle_files = [f for f in files if f.suffix.lower() in cfg.subtitle_extensions]

    if not video_files:
        print("  No video files found.")
        return

    print(f"  Found {len(video_files)} video(s), {len(subtitle_files)} subtitle(s).")

    # Build series info from folder structure
    # Each immediate subfolder of MEDIA_DIR is a series
    series_map: dict[str, dict] = {}  # folder_name -> {id, name, path}

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Create tables
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS series (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name TEXT    NOT NULL UNIQUE,
            title       TEXT    NOT NULL,
            path        TEXT    NOT NULL,
            tmdb_id     INTEGER,
            anilist_id  INTEGER,
            romaji      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS seasons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id   INTEGER NOT NULL,
            season_num  INTEGER NOT NULL,
            folder_name TEXT,
            path        TEXT,
            FOREIGN KEY (series_id) REFERENCES series(id),
            UNIQUE(series_id, season_num)
        );
        CREATE TABLE IF NOT EXISTS episodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id   INTEGER NOT NULL,
            filename    TEXT    NOT NULL,
            title       TEXT,
            season_num  INTEGER NOT NULL,
            episode_num INTEGER NOT NULL,
            absolute_num INTEGER,
            is_special  INTEGER DEFAULT 0,
            file_size   INTEGER,
            file_path   TEXT    NOT NULL,
            FOREIGN KEY (season_id) REFERENCES seasons(id)
        );
        CREATE TABLE IF NOT EXISTS subtitles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id   INTEGER NOT NULL,
            season_id   INTEGER,
            filename    TEXT    NOT NULL,
            language    TEXT,
            subtitle_ext TEXT   NOT NULL,
            file_size   INTEGER,
            file_path   TEXT    NOT NULL,
            video_filename TEXT,
            FOREIGN KEY (series_id) REFERENCES series(id),
            FOREIGN KEY (season_id) REFERENCES seasons(id)
        );
    """)

    # Group video files by top-level subfolder (series)
    for f in video_files:
        try:
            rel = f.relative_to(cfg.media_dir)
        except ValueError:
            continue

        # First component is the series folder
        parts = rel.parts
        if len(parts) < 1:
            continue

        series_folder = parts[0]
        if series_folder not in series_map:
            series_path = cfg.media_dir / series_folder
            # Use the folder name as the title (sanitised)
            clean_title = series_folder.replace(".", " ").replace("_", " ").strip()
            cur.execute(
                "INSERT OR IGNORE INTO series (folder_name, title, path) VALUES (?, ?, ?)",
                (series_folder, clean_title, str(series_path)),
            )
            cur.execute(
                "SELECT id FROM series WHERE folder_name = ?",
                (series_folder,),
            )
            row = cur.fetchone()
            series_map[series_folder] = {
                "id": row[0],
                "name": clean_title,
                "path": series_path,
            }

    # Parse each video file for season/episode info
    episode_count = 0
    for f in video_files:
        try:
            rel = f.relative_to(cfg.media_dir)
        except ValueError:
            continue

        parts = rel.parts
        if len(parts) < 1:
            continue

        series_folder = parts[0]
        sinfo = series_map.get(series_folder)
        if not sinfo:
            continue

        series_id = sinfo["id"]

        # Check if this is a special
        sp_num = special_parser.parse(f.name)
        is_special = sp_num is not None

        # Parse season/episode from filename
        season_num, ep_num = parser.parse_season_episode(f.name)
        abs_num = None

        if season_num is None or ep_num is None:
            abs_num = parser.parse(f.name)
            if abs_num is not None:
                season_num = 1
                ep_num = abs_num
            else:
                season_num = 0
                ep_num = 0
                is_special = True

        if is_special and season_num != 0:
            # Keep the parsed season but mark as special
            is_special = True

        # Determine season folder from path
        season_folder = None
        season_path = None
        if len(parts) >= 2:
            season_folder = parts[1]
            season_path = str(cfg.media_dir / series_folder / parts[1])

        # Insert season
        cur.execute(
            "INSERT OR IGNORE INTO seasons (series_id, season_num, folder_name, path) VALUES (?, ?, ?, ?)",
            (series_id, season_num or 0, season_folder, season_path),
        )
        cur.execute(
            "SELECT id FROM seasons WHERE series_id = ? AND season_num = ?",
            (series_id, season_num or 0),
        )
        season_row = cur.fetchone()

        # Insert episode
        file_size = f.stat().st_size if f.exists() else 0
        cur.execute(
            """INSERT INTO episodes
               (season_id, filename, title, season_num, episode_num, absolute_num, is_special, file_size, file_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season_row[0] if season_row else None,
                f.name,
                None,  # title will be filled by metadata if available
                season_num or 0,
                ep_num or 0,
                abs_num,
                1 if is_special else 0,
                file_size,
                str(f),
            ),
        )
        episode_count += 1

    # Scan subtitle files and insert into subtitles table
    subtitle_count = 0
    from renamer.subtitles import _split_subtitle_stem
    for f in subtitle_files:
        try:
            rel = f.relative_to(cfg.media_dir)
        except ValueError:
            continue

        parts = rel.parts
        if len(parts) < 1:
            continue

        series_folder = parts[0]
        sinfo = series_map.get(series_folder)
        if not sinfo:
            continue

        series_id = sinfo["id"]

        # Determine season from path
        season_id = None
        if len(parts) >= 2:
            cur.execute(
                "SELECT id FROM seasons WHERE series_id = ? AND folder_name = ?",
                (series_id, parts[1]),
            )
            season_row = cur.fetchone()
            if season_row:
                season_id = season_row[0]

        # Extract language tag from subtitle stem
        base_stem, lang_tag = _split_subtitle_stem(f.stem)
        language = lang_tag.lstrip(".") if lang_tag else None

        # Try to find the matching video filename
        video_filename = None
        for vf in video_files:
            if vf.parent == f.parent and vf.stem == base_stem:
                video_filename = vf.name
                break

        file_size = f.stat().st_size if f.exists() else 0
        cur.execute(
            """INSERT INTO subtitles
               (series_id, season_id, filename, language, subtitle_ext, file_size, file_path, video_filename)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                series_id,
                season_id,
                f.name,
                language,
                f.suffix.lower(),
                file_size,
                str(f),
                video_filename,
            ),
        )
        subtitle_count += 1

    conn.commit()

    # Print summary
    cur.execute("SELECT COUNT(*) FROM series")
    n_series = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM seasons")
    n_seasons = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM episodes")
    n_episodes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM subtitles")
    n_subtitles = cur.fetchone()[0]

    conn.close()

    print(
        f"\n  Database created: {db_path}\n"
        f"  Series: {n_series}  |  Seasons: {n_seasons}  |  "
        f"Episodes: {n_episodes}  |  Subtitles: {n_subtitles}"
    )
