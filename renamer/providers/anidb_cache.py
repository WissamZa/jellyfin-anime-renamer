"""
renamer.providers.anidb_cache
=============================
SQLite cache for AniDB file lookups.

Primary key: (ed2k, size) — the compound key used by AniDB's FILE command.
Cache entries expire after 30 days to balance freshness with API quota.

This mirrors anipyrenamer's cache.py but integrates with our config
and logging infrastructure.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from renamer.config import get_logger

log = get_logger(__name__)

# Cache entry TTL
CACHE_STALE_DAYS = 30
CACHE_STALE_SECONDS = CACHE_STALE_DAYS * 86_400


@dataclass
class AniDBFileInfo:
    """Cached AniDB file identification result."""

    ed2k: str
    size: int
    fid: int                   # AniDB file ID
    aid: int                   # AniDB anime ID
    eid: int                   # AniDB episode ID
    gid: int                   # AniDB group ID
    anime_title_romaji: str = ""
    anime_title_english: str = ""
    anime_title_kanji: str = ""
    episode_number: str = ""
    episode_title_en: str = ""
    episode_title_romaji: str = ""
    episode_title_kanji: str = ""
    group_name: str = ""
    quality: str = ""
    source: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    resolution: str = ""
    cached_at: float = 0.0


class AniDBCache:
    """
    SQLite-backed cache for AniDB file lookups.

    The database is stored alongside the project as ``anidb_cache.db``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent.parent / "anidb_cache.db"
        self._db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        """Create the cache table if it doesn't exist."""
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS anidb_file_cache (
                    ed2k                TEXT    NOT NULL,
                    size                INTEGER NOT NULL,
                    fid                 INTEGER NOT NULL,
                    aid                 INTEGER NOT NULL,
                    eid                 INTEGER NOT NULL,
                    gid                 INTEGER NOT NULL,
                    anime_title_romaji  TEXT    DEFAULT '',
                    anime_title_english TEXT    DEFAULT '',
                    anime_title_kanji   TEXT    DEFAULT '',
                    episode_number      TEXT    DEFAULT '',
                    episode_title_en    TEXT    DEFAULT '',
                    episode_title_romaji TEXT   DEFAULT '',
                    episode_title_kanji TEXT    DEFAULT '',
                    group_name          TEXT    DEFAULT '',
                    quality             TEXT    DEFAULT '',
                    source              TEXT    DEFAULT '',
                    video_codec         TEXT    DEFAULT '',
                    audio_codec         TEXT    DEFAULT '',
                    resolution          TEXT    DEFAULT '',
                    cached_at           REAL    NOT NULL,
                    PRIMARY KEY (ed2k, size)
                );
                CREATE INDEX IF NOT EXISTS idx_anidb_aid
                    ON anidb_file_cache (aid);
            """)
            conn.commit()
        finally:
            conn.close()

    def get(self, size: int, ed2k: str) -> AniDBFileInfo | None:
        """
        Look up cached file info by (size, ed2k).

        Returns None if no entry exists or the entry is stale (>30 days).
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM anidb_file_cache WHERE ed2k = ? AND size = ?",
                (ed2k, size),
            ).fetchone()

            if row is None:
                return None

            # Check staleness
            cached_at = row[-1]  # last column is cached_at
            if time.time() - cached_at > CACHE_STALE_SECONDS:
                log.debug("AniDB cache entry stale for ed2k=%s size=%d", ed2k[:8], size)
                return None

            return self._row_to_info(row)
        finally:
            conn.close()

    def set(self, info: AniDBFileInfo) -> None:
        """Insert or update a cache entry."""
        if info.cached_at == 0:
            info.cached_at = time.time()

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO anidb_file_cache
                (ed2k, size, fid, aid, eid, gid,
                 anime_title_romaji, anime_title_english, anime_title_kanji,
                 episode_number, episode_title_en, episode_title_romaji, episode_title_kanji,
                 group_name, quality, source, video_codec, audio_codec, resolution, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info.ed2k, info.size, info.fid, info.aid, info.eid, info.gid,
                    info.anime_title_romaji, info.anime_title_english, info.anime_title_kanji,
                    info.episode_number, info.episode_title_en, info.episode_title_romaji,
                    info.episode_title_kanji, info.group_name, info.quality, info.source,
                    info.video_codec, info.audio_codec, info.resolution, info.cached_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_by_aid(self, aid: int) -> list[AniDBFileInfo]:
        """Return all cached entries for a given anime ID (useful for batch lookups)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM anidb_file_cache WHERE aid = ?",
                (aid,),
            ).fetchall()
            return [self._row_to_info(r) for r in rows]
        finally:
            conn.close()

    def clear_stale(self) -> int:
        """Remove all entries older than CACHE_STALE_DAYS. Returns count of removed rows."""
        cutoff = time.time() - CACHE_STALE_SECONDS
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM anidb_file_cache WHERE cached_at < ?", (cutoff,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def clear_all(self) -> None:
        """Delete all cache entries."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM anidb_file_cache")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_info(row: Any) -> AniDBFileInfo:
        """Convert a database row to AniDBFileInfo."""
        return AniDBFileInfo(
            ed2k=row[0],
            size=row[1],
            fid=row[2],
            aid=row[3],
            eid=row[4],
            gid=row[5],
            anime_title_romaji=row[6],
            anime_title_english=row[7],
            anime_title_kanji=row[8],
            episode_number=row[9],
            episode_title_en=row[10],
            episode_title_romaji=row[11],
            episode_title_kanji=row[12],
            group_name=row[13],
            quality=row[14],
            source=row[15],
            video_codec=row[16],
            audio_codec=row[17],
            resolution=row[18],
            cached_at=row[19],
        )
