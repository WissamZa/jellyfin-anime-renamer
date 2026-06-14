"""
renamer.db — AnimeDatabase
===========================
SQLite-backed backup database for anime file tracking.

Database location: same directory as the app (where .env / qbit_hook.json live)
File: anime_backup.db

Design principles
-----------------
1. **WAL mode + foreign keys** — safe concurrent reads, referential integrity.
2. **Progressive hash matching** — cheapest check first (CRC32 from filename),
   then single-pass MD5+SHA1, then isolated ED2K only for new/missing records.
3. **Atomic batch transactions** — folder scans and JSON imports use a single
   connection with a context manager; never open/close per-item.
4. **Safe data hydration** — ``update_record_safe`` only fills NULL columns;
   ``update_record_force`` is a separate escape-hatch for explicit user edits.
   Safe upsert workflows cross-reference payloads against existing rows before
   issuing any UPDATE.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from renamer.config import get_logger

log = get_logger(__name__)

# ═══════════════════════ CONSTANTS ═══════════════════════

DEFAULT_DB_NAME = "anime_backup.db"
PAGE_SIZE = 20

# CRC32 pattern from fansub filenames: [A1B2C3D4] or (A1B2C3D4)
CRC32_PATTERN = re.compile(r"[\[\(]([A-Fa-f0-9]{8})[\]\)]")

# Group name pattern: [GroupName] at start of filename
GROUP_PATTERN = re.compile(r"^\[([^\]]+)\]")

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS anime_backup (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_title_en  TEXT,
    anime_title_rom TEXT    NOT NULL,
    file_name       TEXT    NOT NULL,
    season_num      INTEGER DEFAULT NULL,
    episode_num     INTEGER DEFAULT NULL,
    size_in_bytes   INTEGER,
    crc32           TEXT,
    md5             TEXT    UNIQUE,
    sha1            TEXT    UNIQUE,
    ed2k            TEXT    UNIQUE,
    group_name      TEXT,
    torrent_name    TEXT,
    torrent_hash    TEXT,
    date_added      TEXT DEFAULT (datetime('now', 'localtime')),
    tmdb_id         INTEGER
);

-- CRC32: fast-filter index only (32-bit, collision-prone, NOT unique)
CREATE INDEX IF NOT EXISTS idx_crc32 ON anime_backup(crc32);

-- md5 / sha1 / ed2k are UNIQUE columns — SQLite creates implicit
-- unique indexes, so no redundant CREATE INDEX statements needed.

-- Other lookup indexes
CREATE INDEX IF NOT EXISTS idx_anime_title_en ON anime_backup(anime_title_en);
CREATE INDEX IF NOT EXISTS idx_anime_title_rom ON anime_backup(anime_title_rom);
CREATE INDEX IF NOT EXISTS idx_tmdb_id ON anime_backup(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_group_name ON anime_backup(group_name);
CREATE INDEX IF NOT EXISTS idx_size_bytes ON anime_backup(size_in_bytes);

-- Composite index for season+episode structural lookups
CREATE INDEX IF NOT EXISTS idx_season_episode
    ON anime_backup(anime_title_rom, season_num, episode_num);
"""


# ═══════════════════════ DATA CLASSES ═══════════════════════


@dataclass
class UpsertResult:
    """Result of a smart upsert operation."""

    action: str  # "inserted" | "skipped" | "needs_update"
    missing_fields: list[str]
    record_id: int
    missing: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)


@dataclass
class ScanResult:
    """Result of a folder scan operation (pending, not yet committed)."""

    added: int = 0
    skipped: int = 0
    updated: int = 0
    renamed: int = 0  # files detected as renamed (same hash, different name)
    collisions: int = 0
    errors: int = 0
    pending_inserts: list[dict] = field(default_factory=list)
    pending_updates: list[tuple[int, dict, dict]] = field(default_factory=list)
    pending_renames: list[tuple[int, dict, dict]] = field(default_factory=list)
    # (record_id, {field: new_value}, existing_record) — force-updates for renamed files


# ═══════════════════════ HASH HELPERS ═══════════════════════


def extract_crc32_from_filename(filename: str) -> str | None:
    """Extract CRC32 hex from fansub filename like ``[A1B2C3D4]``."""
    m = CRC32_PATTERN.search(filename)
    return m.group(1).upper() if m else None


def extract_group_name(filename: str) -> str | None:
    """Extract fansub group name from ``[GroupName]`` at start of filename."""
    m = GROUP_PATTERN.match(filename)
    return m.group(1) if m else None


def compute_hashes_fast(
    file_path: Path,
    *,
    need_sha1: bool = True,
    need_ed2k: bool = False,
) -> dict[str, str]:
    """
    Compute hashes progressively — cheapest to most expensive.

    **Pass 1** (single disk read): CRC32 + MD5 + SHA1
    **Pass 2** (only if *need_ed2k* is True): ED2K via AniDB chunked algorithm.

    Parameters
    ----------
    file_path:
        Path to the file on disk.
    need_sha1:
        If True, compute SHA1 alongside MD5 in the same pass.
    need_ed2k:
        If True, compute ED2K in a separate pass (AniDB block-chunking).

    Returns
    -------
    dict[str, str]
        Keys present depend on flags: always ``crc32`` + ``md5``,
        optionally ``sha1`` and ``ed2k``.
    """
    crc32_val: int = 0
    md5_h = hashlib.md5()
    sha1_h = hashlib.sha1() if need_sha1 else None

    with open(file_path, "rb") as fh:
        while chunk := fh.read(65536):
            crc32_val = zlib.crc32(chunk, crc32_val)
            md5_h.update(chunk)
            if sha1_h is not None:
                sha1_h.update(chunk)

    result: dict[str, str] = {
        "crc32": format(crc32_val & 0xFFFFFFFF, "08X"),
        "md5": md5_h.hexdigest(),
    }
    if sha1_h is not None:
        result["sha1"] = sha1_h.hexdigest()

    if need_ed2k:
        from renamer.ed2k import compute_ed2k

        result["ed2k"] = compute_ed2k(file_path)

    return result


def format_size(size_bytes: int | None) -> str:
    """Format bytes as human-readable size string."""
    if not size_bytes:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.2f} PB"


# ═══════════════════════ DATABASE CLASS ═══════════════════════


class AnimeDatabase:
    """
    SQLite-backed backup database for anime file records.

    Parameters
    ----------
    db_path:
        Path to the database file.  If *None*, defaults to
        ``<project_root>/anime_backup.db`` where the project root is the
        directory containing ``.env`` and ``qbit_hook.json``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent / DEFAULT_DB_NAME
        self._path = db_path
        self._ensure_schema()

    # ── Schema ──────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    # ── Connection factory ──────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """
        Return a new connection with WAL mode and foreign keys enabled.

        Callers should use ``with self._connect() as conn:`` so the
        connection is closed deterministically.
        """
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Core CRUD ───────────────────────────────────────

    def add_record(self, **fields: Any) -> int:
        """Insert a new record and return its ID."""
        cols: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if v is not None:
                cols.append(k)
                vals.append(v)

        col_str = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)

        with self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO anime_backup ({col_str}) VALUES ({placeholders})",
                vals,
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_by_id(self, record_id: int) -> dict | None:
        """Return a single record dict by ID, or *None*."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM anime_backup WHERE id = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    # ── Safe vs Force update ────────────────────────────

    def update_record_safe(
        self, record_id: int, conn: sqlite3.Connection | None = None, **fields: Any
    ) -> tuple[bool, list[str]]:
        """
        **Safe hydration** — only fill columns that are currently NULL.

        Existing non-null values are never overwritten.  This is the
        default update path for automated workflows (folder scans,
        qbit_hook, JSON imports).

        Parameters
        ----------
        record_id:
            The row to update.
        conn:
            Optional existing connection (for batch transactions).
            If *None*, opens and closes its own connection.
        **fields:
            Column=value pairs to potentially set.

        Returns
        -------
        (success, list_of_fields_actually_updated)
        """
        if not fields:
            return False, []

        def _do(c: sqlite3.Connection) -> tuple[bool, list[str]]:
            row = c.execute("SELECT * FROM anime_backup WHERE id = ?", (record_id,)).fetchone()
            if not row:
                return False, []

            safe: dict[str, Any] = {}
            for k, v in fields.items():
                if v is not None and row[k] is None:
                    safe[k] = v

            if not safe:
                return False, []

            set_parts = [f"{k} = ?" for k in safe]
            vals = list(safe.values()) + [record_id]
            c.execute(f"UPDATE anime_backup SET {', '.join(set_parts)} WHERE id = ?", vals)
            return True, list(safe.keys())

        if conn is not None:
            return _do(conn)

        with self._connect() as c:
            ok, updated = _do(c)
            c.commit()
            return ok, updated

    def update_record_force(
        self, record_id: int, conn: sqlite3.Connection | None = None, **fields: Any
    ) -> bool:
        """
        **Force override** — unconditionally set columns, even if non-null.

        This is the escape-hatch for explicit user edits from the
        interactive menu.  Automated workflows should always use
        ``update_record_safe`` instead.
        """
        if not fields:
            return False

        set_parts = [f"{k} = ?" for k in fields]
        vals = list(fields.values()) + [record_id]

        def _do(c: sqlite3.Connection) -> bool:
            c.execute(f"UPDATE anime_backup SET {', '.join(set_parts)} WHERE id = ?", vals)
            return c.execute("SELECT changes()").fetchone()[0] > 0

        if conn is not None:
            return _do(conn)

        with self._connect() as c:
            ok = _do(c)
            c.commit()
            return ok

    def delete_record(self, record_id: int) -> bool:
        """Delete a record by ID."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM anime_backup WHERE id = ?", (record_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Listing / Pagination ────────────────────────────

    def list_all(
        self,
        limit: int = PAGE_SIZE,
        offset: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        """Return paginated records, newest first."""

        def _do(c: sqlite3.Connection) -> list[dict]:
            rows = c.execute(
                "SELECT * FROM anime_backup ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

        if conn is not None:
            return _do(conn)
        with self._connect() as c:
            return _do(c)

    def count(self) -> int:
        """Total number of records."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM anime_backup").fetchone()[0]

    # ── Progressive hash matching ───────────────────────

    def find_existing(
        self,
        *,
        crc32: str | None = None,
        md5: str | None = None,
        sha1: str | None = None,
        ed2k: str | None = None,
        file_name: str | None = None,
        size: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict | None:
        """
        Find an existing record using progressive hash matching.

        Strategy (cheapest → most expensive):

        1. **Strong hashes** (md5 / sha1 / ed2k) → direct UNIQUE lookup
           (guaranteed correct, single index seek).
        2. **CRC32** → fast filter; if candidates found, verify by
           ``(file_name, size_in_bytes)`` to rule out collisions.
        3. **Fallback** → match by ``(file_name, size_in_bytes)``.

        Returns the confirmed match dict, or *None*.
        """

        def _do(c: sqlite3.Connection) -> dict | None:
            # ── Strong hashes first (unique, no collision risk) ──
            if md5:
                row = c.execute("SELECT * FROM anime_backup WHERE md5 = ?", (md5,)).fetchone()
                if row:
                    return dict(row)

            if sha1:
                row = c.execute("SELECT * FROM anime_backup WHERE sha1 = ?", (sha1,)).fetchone()
                if row:
                    return dict(row)

            if ed2k:
                row = c.execute("SELECT * FROM anime_backup WHERE ed2k = ?", (ed2k,)).fetchone()
                if row:
                    return dict(row)

            # ── CRC32: fast filter, MUST verify (collision-prone) ──
            if crc32:
                candidates = c.execute(
                    "SELECT * FROM anime_backup WHERE crc32 = ?", (crc32,)
                ).fetchall()
                if not candidates:
                    # No CRC32 match → definitely a new file
                    return None

                for cand in candidates:
                    if self._verify_same_file(dict(cand), file_name=file_name, size=size):
                        return dict(cand)

                # CRC32 matched but nothing verified → CRC32 collision
                return None

            # ── Last resort: file_name + size ──
            if file_name and size:
                row = c.execute(
                    "SELECT * FROM anime_backup WHERE file_name = ? AND size_in_bytes = ?",
                    (file_name, size),
                ).fetchone()
                if row:
                    return dict(row)

            return None

        if conn is not None:
            return _do(conn)
        with self._connect() as c:
            return _do(c)

    @staticmethod
    def _verify_same_file(
        candidate: dict,
        file_name: str | None = None,
        size: int | None = None,
    ) -> bool:
        """
        After CRC32 matched, verify it is actually the same file.

        Confirmation requires matching both ``file_name`` **and**
        ``size_in_bytes``.
        """
        return (
            file_name
            and candidate.get("file_name") == file_name
            and size is not None
            and candidate.get("size_in_bytes") == size
        )

    # ── Search / Filter ─────────────────────────────────

    def search(self, query: str) -> list[dict]:
        """Search across title, filename, group, and torrent fields."""
        like = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM anime_backup
                   WHERE anime_title_en LIKE ? OR anime_title_rom LIKE ?
                      OR file_name LIKE ? OR group_name LIKE ?
                      OR torrent_name LIKE ?
                   ORDER BY anime_title_rom, season_num, episode_num
                   LIMIT 200""",
                (like, like, like, like, like),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_by_anime_title(self, title: str) -> list[dict]:
        """Get all records matching *anime_title_rom* or *anime_title_en*."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM anime_backup
                   WHERE anime_title_rom = ? OR anime_title_en = ?
                   ORDER BY season_num, episode_num""",
                (title, title),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_by_tmdb_id(self, tmdb_id: int) -> list[dict]:
        """Get all records for a given TMDB series."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM anime_backup WHERE tmdb_id = ?
                   ORDER BY season_num, episode_num""",
                (tmdb_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_missing_fields(self, record_id: int) -> list[str]:
        """Return column names that are NULL for a given record."""
        record = self.get_by_id(record_id)
        if not record:
            return []
        return [k for k, v in record.items() if v is None and k != "id"]

    # ── Smart Upsert ────────────────────────────────────

    def smart_upsert(self, record: dict, conn: sqlite3.Connection | None = None) -> UpsertResult:
        """
        Identify whether *record* should be inserted, skipped, or used to
        fill missing fields on an existing row.

        **Does NOT modify the database** — the caller must confirm and then
        call ``apply_update`` or ``add_record`` explicitly.

        Rules:
          * Never overwrite existing non-null values.
          * Only identify NULL fields that the new data could fill.
        """
        existing = self.find_existing(
            conn=conn,
            crc32=record.get("crc32"),
            md5=record.get("md5"),
            sha1=record.get("sha1"),
            ed2k=record.get("ed2k"),
            file_name=record.get("file_name"),
            size=record.get("size_in_bytes"),
        )

        if not existing:
            return UpsertResult("inserted", [], 0)

        missing: dict[str, Any] = {}
        for key, new_val in record.items():
            old_val = existing.get(key)
            if old_val is None and new_val is not None:
                missing[key] = new_val

        if not missing:
            return UpsertResult("skipped", [], existing["id"])

        return UpsertResult(
            "needs_update",
            list(missing.keys()),
            existing["id"],
            missing=missing,
            existing=existing,
        )

    def apply_update(
        self, record_id: int, fields: dict, conn: sqlite3.Connection | None = None
    ) -> tuple[bool, list[str]]:
        """Apply a previously confirmed safe update (fills NULLs only)."""
        ok, updated = self.update_record_safe(record_id, conn=conn, **fields)
        if ok:
            log.info("Record #%d: filled missing fields: %s", record_id, updated)
        return ok, updated

    # ── Scan Folder (two-phase: identify → confirm → commit) ──

    def scan_folder(
        self,
        folder: Path,
        video_extensions: tuple[str, ...],
        cfg: Any | None = None,
    ) -> ScanResult:
        """
        Phase 1 — scan a folder for video files and identify new/existing
        records.  **No database changes are made.**  Call ``apply_scan()``
        after user confirmation to commit.

        Series name resolution order:
          1. Extract from video filenames via ``extract_series_from_filename``
          2. Fall back to ``folder.name`` (resolved to absolute path first)
          3. API calls to TMDB + AniList are made **once per unique series**
             (not per file) to avoid redundant lookups.
        """
        from renamer.parsers import EpisodeNumberParser

        # Resolve to absolute path so folder.name is never "." or ".."
        folder = folder.resolve()

        result = ScanResult()
        parser = EpisodeNumberParser()

        video_files = sorted(
            p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in video_extensions
        )

        if not video_files:
            return result

        # ── Pre-scan: extract series names from filenames ──
        # Group files by detected series name so we only call
        # TMDB/AniList once per series.
        series_name_map: dict[Path, str] = {}  # {video_path: series_name}
        # {series_name: (title_en, title_rom, tmdb_id)}
        resolved_cache: dict[str, tuple[str | None, str | None, int | None]] = {}

        for vf in video_files:
            detected = _extract_series_name_from_file(vf.name)
            series_name_map[vf] = detected

        # Collect unique series names and resolve titles
        unique_series = sorted({v for v in series_name_map.values() if v})
        if unique_series and cfg is not None:
            print(
                f"  Resolving {len(unique_series)} series title(s) via TMDB + AniList …", flush=True
            )

        for idx, detected in enumerate(unique_series, 1):
            if detected not in resolved_cache and cfg is not None:
                print(f"    [{idx}/{len(unique_series)}] '{detected}' … ", end="", flush=True)
                title_en, title_rom, tmdb_id = _resolve_titles(detected, cfg)
                resolved_cache[detected] = (title_en, title_rom, tmdb_id)
                result_str = title_rom or detected
                if tmdb_id:
                    result_str += f" (TMDB:{tmdb_id})"
                print(result_str, flush=True)

        # Also try the folder name if no series was detected from any file
        folder_series = _clean_folder_name_for_search(folder.name)
        if folder_series and folder_series not in resolved_cache and cfg is not None:
            print(f"    Folder name '{folder_series}' … ", end="", flush=True)
            resolved_cache[folder_series] = _resolve_titles(folder_series, cfg)
            title_en, title_rom, tmdb_id = resolved_cache[folder_series]
            result_str = title_rom or folder_series
            if tmdb_id:
                result_str += f" (TMDB:{tmdb_id})"
            print(result_str, flush=True)

        # Use a single connection for the entire scan (read-only)
        total = len(video_files)
        print(f"\n  Scanning {total} video file(s) in {folder.name} …")

        with self._connect() as conn:
            for i, vf in enumerate(video_files, 1):
                try:
                    # Step 1: CRC32 from filename (ZERO cost)
                    crc32 = extract_crc32_from_filename(vf.name)
                    file_size = vf.stat().st_size

                    # Step 2: quick match check by CRC32 + filename + size
                    existing = self.find_existing(
                        conn=conn,
                        crc32=crc32,
                        file_name=vf.name,
                        size=file_size,
                    )

                    if existing:
                        missing = self.get_missing_fields(existing["id"])
                        if missing:
                            # Compute only the hashes that are missing
                            need_ed2k = "ed2k" in missing
                            need_sha1 = "sha1" in missing or (
                                "md5" in missing
                            )  # single-pass gets both
                            print(
                                f"  [{i}/{total}] {vf.name} — updating missing hashes …",
                                flush=True,
                            )
                            hashes = compute_hashes_fast(
                                vf, need_sha1=need_sha1, need_ed2k=need_ed2k
                            )

                            update_fields: dict[str, Any] = {}
                            for k in missing:
                                if k in hashes:
                                    update_fields[k] = hashes[k]

                            if update_fields:
                                result.pending_updates.append(
                                    (existing["id"], update_fields, existing)
                                )
                                result.updated += 1
                                print(
                                    f"      -> filled: {', '.join(update_fields.keys())}",
                                    flush=True,
                                )
                            else:
                                result.skipped += 1
                        else:
                            result.skipped += 1
                            print(f"  [{i}/{total}] {vf.name} — already in DB", flush=True)
                        continue

                    # Step 3: new file — compute MD5+SHA1 first (single pass)
                    print(
                        f"  [{i}/{total}] {vf.name} — hashing (MD5+SHA1) …",
                        end="",
                        flush=True,
                    )
                    hashes = compute_hashes_fast(vf, need_sha1=True, need_ed2k=False)
                    if crc32:
                        hashes["crc32"] = crc32  # prefer filename CRC32
                    print(" done", flush=True)

                    # Step 3b: Second match check with strong hashes.
                    # File may have been renamed since last scan — same content
                    # but different filename.  If found, update the existing
                    # record instead of inserting a duplicate.
                    existing_by_hash = self.find_existing(
                        conn=conn,
                        md5=hashes.get("md5"),
                        sha1=hashes.get("sha1"),
                    )

                    if existing_by_hash:
                        # File was renamed — update existing record
                        force_fields: dict[str, Any] = {}
                        safe_fields: dict[str, Any] = {}

                        # Always update file_name if it changed
                        if existing_by_hash.get("file_name") != vf.name:
                            force_fields["file_name"] = vf.name

                        # Update size if it changed
                        if file_size and existing_by_hash.get("size_in_bytes") != file_size:
                            force_fields["size_in_bytes"] = file_size

                        # Update crc32 if it changed (file content might be same
                        # but filename CRC32 different)
                        if crc32 and existing_by_hash.get("crc32") is None:
                            safe_fields["crc32"] = crc32

                        # Safe-fill any missing hash fields
                        for hk in ("md5", "sha1"):
                            if hk in hashes and existing_by_hash.get(hk) is None:
                                safe_fields[hk] = hashes[hk]

                        # Compute ED2K only if missing
                        if existing_by_hash.get("ed2k") is None:
                            print(
                                "      hashing (ED2K) …",
                                end="",
                                flush=True,
                            )
                            ed2k_hashes = compute_hashes_fast(vf, need_sha1=False, need_ed2k=True)
                            if "ed2k" in ed2k_hashes:
                                safe_fields["ed2k"] = ed2k_hashes["ed2k"]
                            print(" done", flush=True)

                        if force_fields:
                            result.pending_renames.append(
                                (existing_by_hash["id"], force_fields, existing_by_hash)
                            )
                        if safe_fields:
                            result.pending_updates.append(
                                (existing_by_hash["id"], safe_fields, existing_by_hash)
                            )

                        if force_fields or safe_fields:
                            result.renamed += 1
                            old_name = existing_by_hash.get("file_name", "?")
                            print(
                                f"      -> RENAMED: was '{old_name}' (record #{existing_by_hash['id']})",
                                flush=True,
                            )
                            log.info(
                                "Renamed file detected: '%s' -> was '%s' (record #%d)",
                                vf.name,
                                old_name,
                                existing_by_hash["id"],
                            )
                        else:
                            result.skipped += 1
                        continue

                    # Step 3c: truly new file — compute ED2K too
                    print(
                        f"  [{i}/{total}] {vf.name} — hashing (ED2K) …",
                        end="",
                        flush=True,
                    )
                    ed2k_hashes = compute_hashes_fast(vf, need_sha1=False, need_ed2k=True)
                    if "ed2k" in ed2k_hashes:
                        hashes["ed2k"] = ed2k_hashes["ed2k"]
                    print(" done", flush=True)

                    # Parse season/episode
                    season_num, episode_num = parser.parse_season_episode(vf.name)
                    if season_num is None or episode_num is None:
                        abs_num = parser.parse(vf.name)
                        if abs_num is not None:
                            season_num = 1
                            episode_num = abs_num

                    # Extract group
                    group = extract_group_name(vf.name)

                    # ── Resolve series name (filename > folder name) ──
                    detected_name = series_name_map.get(vf, "")
                    fallback_name = folder_series or folder.name
                    best_name = detected_name or fallback_name

                    # Build record
                    record: dict[str, Any] = {
                        "anime_title_rom": best_name,
                        "file_name": vf.name,
                        "season_num": season_num,
                        "episode_num": episode_num,
                        "size_in_bytes": file_size,
                        "group_name": group,
                        **hashes,
                    }

                    # Apply pre-resolved TMDB titles (if available)
                    cached = resolved_cache.get(detected_name)
                    if not cached and folder_series:
                        cached = resolved_cache.get(folder_series)
                    if not cached and best_name:
                        cached = resolved_cache.get(best_name)
                    if cached:
                        title_en, title_rom, tmdb_id = cached
                        if title_en:
                            record["anime_title_en"] = title_en
                        if title_rom:
                            record["anime_title_rom"] = title_rom
                        if tmdb_id:
                            record["tmdb_id"] = tmdb_id
                        tmdb_str = f" (TMDB:{tmdb_id})" if tmdb_id else ""
                        print(
                            f"      -> NEW: {title_rom or best_name}{tmdb_str}",
                            flush=True,
                        )

                    result.pending_inserts.append(record)
                    result.added += 1

                except Exception as e:
                    log.error("Error scanning %s: %s", vf.name, e)
                    print(f"      -> ERROR: {e}", flush=True)
                    result.errors += 1

        # Print scan summary
        print(
            f"\n  Scan complete: {result.added} new, {result.renamed} renamed, "
            f"{result.updated} updated, {result.skipped} skipped, "
            f"{result.errors} errors",
            flush=True,
        )

        return result

    def apply_scan(self, scan_result: ScanResult) -> tuple[int, int]:
        """
        Phase 2 — commit a confirmed scan result inside a single atomic
        transaction.  Returns ``(inserted_count, updated_count)``.
        """
        inserted = 0
        updated = 0

        with self._connect() as conn:
            try:
                # ── Batch inserts (with IntegrityError safety net) ──
                for record in scan_result.pending_inserts:
                    cols: list[str] = []
                    vals: list[Any] = []
                    for k, v in record.items():
                        if v is not None:
                            cols.append(k)
                            vals.append(v)

                    col_str = ", ".join(cols)
                    placeholders = ", ".join("?" for _ in cols)
                    try:
                        conn.execute(
                            f"INSERT INTO anime_backup ({col_str}) VALUES ({placeholders})",
                            vals,
                        )
                        inserted += 1
                    except sqlite3.IntegrityError as ie:
                        # UNIQUE constraint violation — file already in DB by hash
                        # but scan missed it (e.g. renamed file with same content).
                        # Do a safe update instead of crashing.
                        log.warning(
                            "IntegrityError on INSERT for '%s': %s — doing safe update instead",
                            record.get("file_name", "?"),
                            ie,
                        )
                        existing = self.find_existing(
                            conn=conn,
                            md5=record.get("md5"),
                            sha1=record.get("sha1"),
                            ed2k=record.get("ed2k"),
                        )
                        if existing:
                            # Force-update file_name, safe-fill missing fields
                            if existing.get("file_name") != record.get("file_name"):
                                self.update_record_force(
                                    existing["id"],
                                    conn=conn,
                                    file_name=record["file_name"],
                                )
                            safe: dict[str, Any] = {}
                            for k, v in record.items():
                                if (
                                    k not in ("file_name",)
                                    and v is not None
                                    and existing.get(k) is None
                                ):
                                    safe[k] = v
                            if safe:
                                self.update_record_safe(existing["id"], conn=conn, **safe)
                            updated += 1
                        else:
                            log.error(
                                "IntegrityError but no existing record found for '%s'",
                                record.get("file_name", "?"),
                            )

                # ── Batch safe updates (fill NULLs) ──
                for record_id, fields, _existing in scan_result.pending_updates:
                    ok, _ = self.update_record_safe(record_id, conn=conn, **fields)
                    if ok:
                        updated += 1

                # ── Batch rename force-updates (update file_name etc.) ──
                for record_id, force_fields, _existing in scan_result.pending_renames:
                    ok = self.update_record_force(record_id, conn=conn, **force_fields)
                    if ok:
                        updated += 1

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        log.info(
            "Apply scan: %d inserted, %d updated (incl. %d renamed)",
            inserted,
            updated,
            len(scan_result.pending_renames),
        )
        return inserted, updated

    # ── Export / Import ─────────────────────────────────

    def export_to_json(self, output_path: Path, records: list[dict] | None = None) -> Path:
        """
        Export records to a JSON file.
        If *records* is *None*, exports all records.
        """
        if records is None:
            records = self.list_all(limit=999999, offset=0)

        output_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Exported %d records to %s", len(records), output_path)
        return output_path

    def import_from_json(self, json_path: Path) -> tuple[int, int, int]:
        """
        Import records from a JSON file inside a single atomic transaction.
        Uses safe hydration — only fills NULLs, never overwrites.

        Returns ``(added, updated, skipped)``.
        """
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = [data]

        added = 0
        updated = 0
        skipped = 0

        with self._connect() as conn:
            try:
                for record in data:
                    result = self.smart_upsert(record, conn=conn)

                    if result.action == "inserted":
                        cols: list[str] = []
                        vals: list[Any] = []
                        for k, v in record.items():
                            if v is not None:
                                cols.append(k)
                                vals.append(v)

                        col_str = ", ".join(cols)
                        placeholders = ", ".join("?" for _ in cols)
                        conn.execute(
                            f"INSERT INTO anime_backup ({col_str}) VALUES ({placeholders})",
                            vals,
                        )
                        added += 1

                    elif result.action == "needs_update":
                        ok, _ = self.update_record_safe(
                            result.record_id, conn=conn, **result.missing
                        )
                        if ok:
                            updated += 1
                        else:
                            skipped += 1
                    else:
                        skipped += 1

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        log.info("Import: %d added, %d updated, %d skipped", added, updated, skipped)
        return added, updated, skipped

    # ── Statistics ──────────────────────────────────────

    def find_files_for_rename(
        self,
        folder: Path,
        video_extensions: tuple[str, ...],
    ) -> list[dict]:
        """
        Find video files on disk and match them to DB records by hash.

        For each matched file where the current filename differs from
        the DB's ``file_name``, returns a dict with:
          - ``disk_path``: absolute Path on disk
          - ``current_name``: current filename
          - ``db_name``: filename stored in the database
          - ``record``: the full DB record dict
          - ``needs_rename``: True if names differ

        Uses progressive hash matching: CRC32 from filename first,
        then MD5+SHA1, then ED2K — cheapest first.

        Returns only files that exist on disk AND have a matching
        DB record. Files not in the DB are skipped.
        """
        folder = folder.resolve()
        video_files = sorted(
            p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in video_extensions
        )

        if not video_files:
            return []

        results: list[dict] = []
        total = len(video_files)
        print(f"\n  Matching {total} file(s) against database …", flush=True)

        with self._connect() as conn:
            for i, vf in enumerate(video_files, 1):
                # Step 1: CRC32 from filename (zero cost)
                crc32 = extract_crc32_from_filename(vf.name)
                file_size = vf.stat().st_size

                # Step 2: Try fast match by CRC32 + filename + size
                record = self.find_existing(
                    conn=conn,
                    crc32=crc32,
                    file_name=vf.name,
                    size=file_size,
                )

                # Step 3: If not found, compute MD5+SHA1 and try again
                if not record:
                    print(
                        f"  [{i}/{total}] {vf.name} — hashing (MD5+SHA1) …",
                        end="",
                        flush=True,
                    )
                    hashes = compute_hashes_fast(vf, need_sha1=True, need_ed2k=False)
                    record = self.find_existing(
                        conn=conn,
                        md5=hashes.get("md5"),
                        sha1=hashes.get("sha1"),
                    )
                    print(" done", flush=True)

                # Step 4: If still not found, compute ED2K and try again
                if not record:
                    print(
                        f"  [{i}/{total}] {vf.name} — hashing (ED2K) …",
                        end="",
                        flush=True,
                    )
                    ed2k_hashes = compute_hashes_fast(vf, need_sha1=False, need_ed2k=True)
                    record = self.find_existing(
                        conn=conn,
                        ed2k=ed2k_hashes.get("ed2k"),
                    )
                    print(" done", flush=True)

                if record:
                    db_name = record.get("file_name", "")
                    needs_rename = db_name != vf.name
                    results.append(
                        {
                            "disk_path": vf,
                            "current_name": vf.name,
                            "db_name": db_name,
                            "record": record,
                            "needs_rename": needs_rename,
                        }
                    )

        return results

    def restore_original_names(
        self,
        folder: Path,
        video_extensions: tuple[str, ...],
        dry_run: bool = True,
    ) -> tuple[int, int, int]:
        """
        Rename files back to their original names using hash-based DB lookup.

        For each video file on disk, looks up its hash in the database.
        If the DB record has a different ``file_name`` than the current
        filename on disk, renames the file back to the DB name.

        Parameters
        ----------
        folder:
            Directory to scan for video files.
        video_extensions:
            Tuple of video file extensions to consider.
        dry_run:
            If True (default), only preview changes without renaming.

        Returns
        -------
        (renamed, skipped, errors)
            Count of files renamed, skipped (already match), and errors.
        """
        matches = self.find_files_for_rename(folder, video_extensions)

        # Filter to only files that need renaming
        to_rename = [m for m in matches if m["needs_rename"]]

        if not to_rename:
            print("\n  All files already match their database names.", flush=True)
            return 0, len(matches), 0

        print(f"\n  Found {len(to_rename)} file(s) to rename:", flush=True)

        renamed = 0
        skipped = 0
        errors = 0

        for m in to_rename:
            disk_path: Path = m["disk_path"]
            db_name: str = m["db_name"]
            current_name: str = m["current_name"]

            dest_path = disk_path.parent / db_name

            # Safety: check destination doesn't already exist
            if dest_path.exists() and dest_path.resolve() != disk_path.resolve():
                print(
                    f"    SKIP (target exists): {current_name} -> {db_name}",
                    flush=True,
                )
                skipped += 1
                continue

            mode_label = "[DRY]" if dry_run else ""
            print(
                f"    {mode_label} {current_name} -> {db_name}",
                flush=True,
            )

            if not dry_run:
                try:
                    disk_path.rename(dest_path)
                    renamed += 1
                    # Also update the DB to reflect the new (original) name
                    self.update_record_force(m["record"]["id"], file_name=db_name)
                except Exception as exc:
                    log.error("Failed to rename %s: %s", disk_path.name, exc)
                    print(f"      ERROR: {exc}", flush=True)
                    errors += 1
            else:
                renamed += 1

        mode_str = "Would rename" if dry_run else "Renamed"
        print(
            f"\n  {mode_str}: {renamed} | Skipped: {skipped} | Errors: {errors}",
            flush=True,
        )

        return renamed, skipped, errors

    def get_statistics(self) -> dict[str, Any]:
        """Return database statistics."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM anime_backup").fetchone()[0]
            unique_series = conn.execute(
                "SELECT COUNT(DISTINCT anime_title_rom) FROM anime_backup"
            ).fetchone()[0]
            unique_groups = conn.execute(
                "SELECT COUNT(DISTINCT group_name) FROM anime_backup WHERE group_name IS NOT NULL"
            ).fetchone()[0]
            total_size = conn.execute(
                "SELECT COALESCE(SUM(size_in_bytes), 0) FROM anime_backup"
            ).fetchone()[0]
            crc32_count = conn.execute(
                "SELECT COUNT(*) FROM anime_backup WHERE crc32 IS NOT NULL"
            ).fetchone()[0]
            md5_count = conn.execute(
                "SELECT COUNT(*) FROM anime_backup WHERE md5 IS NOT NULL"
            ).fetchone()[0]
            sha1_count = conn.execute(
                "SELECT COUNT(*) FROM anime_backup WHERE sha1 IS NOT NULL"
            ).fetchone()[0]
            ed2k_count = conn.execute(
                "SELECT COUNT(*) FROM anime_backup WHERE ed2k IS NOT NULL"
            ).fetchone()[0]
            tmdb_count = conn.execute(
                "SELECT COUNT(*) FROM anime_backup WHERE tmdb_id IS NOT NULL"
            ).fetchone()[0]

            top_groups = conn.execute(
                """SELECT group_name, COUNT(*) as cnt FROM anime_backup
                   WHERE group_name IS NOT NULL
                   GROUP BY group_name ORDER BY cnt DESC LIMIT 5"""
            ).fetchall()

            return {
                "total": total,
                "unique_series": unique_series,
                "unique_groups": unique_groups,
                "total_size": total_size,
                "crc32_count": crc32_count,
                "md5_count": md5_count,
                "sha1_count": sha1_count,
                "ed2k_count": ed2k_count,
                "tmdb_count": tmdb_count,
                "top_groups": [(r["group_name"], r["cnt"]) for r in top_groups],
            }


# ═══════════════════════ TITLE RESOLUTION ═══════════════════════


def _resolve_titles(series_name: str, cfg: Any) -> tuple[str | None, str | None, int | None]:
    """
    Resolve both English and Romaji titles from TMDB + AniList.

    Returns ``(title_en, title_rom, tmdb_id)``.

    Resolution strategy:
      1. TMDB search → get tmdb_id + English title
      2. TMDB Alternative Titles → cross-reference for better match
      3. AniList search → get Romaji title (human-curated, highest quality)
      4. If TMDB failed but AniList succeeded → retry TMDB with AniList title
      5. If both failed → try search variants (season suffixes removed, etc.)
      6. Fallback: use series_name as romaji

    If *series_name* is empty or only contains non-word characters
    (e.g. ``"."``, ``".."``), returns ``(None, None, None)`` without
    making any API calls.
    """
    # Guard: skip API calls for empty / meaningless names
    cleaned = series_name.strip()
    if not cleaned or not re.search(r"\w", cleaned):
        log.warning("_resolve_titles: skipping empty/invalid series name '%s'", series_name)
        return None, None, None

    title_en: str | None = None
    title_rom: str | None = None
    tmdb_id: int | None = None
    alt_titles: list[str] = []  # TMDB alternative titles for cross-ref

    # ── Step 1: TMDB search for English name + ID ──
    with contextlib.suppress(Exception):
        from renamer.config import Provider
        from renamer.providers.registry import get_registry

        registry = get_registry()
        search_result = registry.search(Provider.TMDB, cleaned, cfg)
        if search_result:
            tmdb_id = search_result.tmdb_id or search_result.series_id
            title_en = search_result.series_name

            # ── Step 2: Fetch TMDB Alternative Titles for cross-reference ──
            if tmdb_id and cfg.tmdb_api_key:
                with contextlib.suppress(Exception):
                    from renamer.providers.tmdb import TMDBFetcher

                    fetcher = TMDBFetcher(
                        api_key=cfg.tmdb_api_key,
                        series_id=tmdb_id,
                        cfg=cfg,
                    )
                    alt_title_list = fetcher.fetch_alternative_titles()
                    if alt_title_list:
                        alt_titles = [t["title"] for t in alt_title_list]
                        log.info(
                            "TMDB Alternative Titles for id=%d: %s",
                            tmdb_id,
                            alt_titles[:5],
                        )
                        # If the search name matches an alt title better
                        # than the top result, the TMDB match is confirmed.
                        # Also check if AniList romaji is in alt titles.

    # ── Step 3: AniList for Romaji ──
    anilist_romaji: str | None = None
    anilist_english: str | None = None
    with contextlib.suppress(Exception):
        from renamer.providers.anilist import AniListFetcher

        af = AniListFetcher()
        media = af._gql(af.SERIES_QUERY, {"search": cleaned})
        if media:
            titles = media.get("title", {})
            anilist_romaji = titles.get("romaji") or titles.get("native")
            anilist_english = titles.get("english")
            title_rom = anilist_romaji
            if not title_en:
                title_en = anilist_english

    # ── Step 4: If TMDB failed but AniList found a title, retry TMDB ──
    if not tmdb_id and anilist_romaji:
        with contextlib.suppress(Exception):
            from renamer.config import Provider
            from renamer.providers.registry import get_registry

            registry = get_registry()
            search_result = registry.search(Provider.TMDB, anilist_romaji, cfg)
            if search_result:
                tmdb_id = search_result.tmdb_id or search_result.series_id
                title_en = search_result.series_name
                log.info(
                    "TMDB retry with AniList title '%s' -> id=%d",
                    anilist_romaji,
                    tmdb_id,
                )

                # Fetch alternative titles for this newly found series
                if tmdb_id and cfg.tmdb_api_key and not alt_titles:
                    with contextlib.suppress(Exception):
                        from renamer.providers.tmdb import TMDBFetcher

                        fetcher = TMDBFetcher(
                            api_key=cfg.tmdb_api_key,
                            series_id=tmdb_id,
                            cfg=cfg,
                        )
                        alt_title_list = fetcher.fetch_alternative_titles()
                        if alt_title_list:
                            alt_titles = [t["title"] for t in alt_title_list]

    # Also try TMDB with AniList English title if still no match
    if not tmdb_id and anilist_english and anilist_english != anilist_romaji:
        with contextlib.suppress(Exception):
            from renamer.config import Provider
            from renamer.providers.registry import get_registry

            registry = get_registry()
            search_result = registry.search(Provider.TMDB, anilist_english, cfg)
            if search_result:
                tmdb_id = search_result.tmdb_id or search_result.series_id
                title_en = search_result.series_name
                log.info(
                    "TMDB retry with AniList English '%s' -> id=%d",
                    anilist_english,
                    tmdb_id,
                )

    # ── Step 5: Try search variants if both TMDB and AniList failed ──
    if not tmdb_id and not title_rom:
        for variant in _generate_search_variants(cleaned):
            if variant == cleaned:
                continue
            # Try TMDB
            with contextlib.suppress(Exception):
                from renamer.config import Provider
                from renamer.providers.registry import get_registry

                registry = get_registry()
                search_result = registry.search(Provider.TMDB, variant, cfg)
                if search_result:
                    tmdb_id = search_result.tmdb_id or search_result.series_id
                    title_en = search_result.series_name
                    log.info(
                        "TMDB variant search '%s' -> id=%d",
                        variant,
                        tmdb_id,
                    )
                    break

            # Try AniList
            if not title_rom:
                with contextlib.suppress(Exception):
                    from renamer.providers.anilist import AniListFetcher

                    af = AniListFetcher()
                    media = af._gql(af.SERIES_QUERY, {"search": variant})
                    if media:
                        titles = media.get("title", {})
                        title_rom = titles.get("romaji") or titles.get("native")
                        if not title_en:
                            title_en = titles.get("english")
                        log.info(
                            "AniList variant search '%s' -> romaji='%s'",
                            variant,
                            title_rom,
                        )
                        break

    # ── Step 6: Cross-reference TMDB alt titles with AniList romaji ──
    # If the AniList romaji title appears in TMDB alt titles, it confirms
    # the TMDB match is correct and we should prefer AniList's romaji.
    if alt_titles and title_rom:
        # Case-insensitive check
        alt_lower = [t.lower() for t in alt_titles]
        if title_rom.lower() in alt_lower:
            log.info(
                "AniList romaji '%s' confirmed by TMDB alternative titles",
                title_rom,
            )
        elif title_en and title_rom.lower() != title_en.lower() and cleaned.lower() in alt_lower:
            log.info(
                "Search name '%s' confirmed by TMDB alternative titles for id=%d",
                cleaned,
                tmdb_id,
            )

    # ── Step 7: Final fallback ──
    if not title_rom:
        # Try to use an alt title that looks like romaji
        if alt_titles:
            from renamer.romaniser import get_romaniser

            romaniser = get_romaniser()
            for at in alt_titles:
                if romaniser.is_japanese(at):
                    title_rom = romaniser.to_romaji(at)
                    log.info(
                        "Using romanised TMDB alt title as romaji: '%s'",
                        title_rom,
                    )
                    break
        if not title_rom:
            title_rom = cleaned

    return title_en, title_rom, tmdb_id


def _generate_search_variants(name: str) -> list[str]:
    """
    Generate alternative search terms for a series name.

    Handles common cases where the filename has a shortened or
    different version of the title.
    """
    variants = [name]

    # Remove trailing season indicators: "Series 2nd Season", "Series S2"
    cleaned = re.sub(r"\s+(?:2nd|3rd|4th|\d+th)\s+Season$", "", name, flags=re.IGNORECASE)
    if cleaned != name:
        variants.append(cleaned)

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

    # Remove trailing dash + number: "Series - 2" (sometimes season indicator)
    cleaned = re.sub(r"\s*-\s*\d+$", "", name)
    if cleaned != name:
        variants.append(cleaned)

    return variants


def _extract_series_name_from_file(filename: str) -> str:
    """
    Extract the series name from a video filename.

    Uses ``extract_series_from_filename`` from the hash_organizer module
    which handles common fansub patterns like::

        [SubGroup] Series Name - S01E01 - Title.mkv
        [Erai-raws] Series Name - 02 [1080p].mkv
        Series Name - S01E09 - Title.mkv

    Falls back to stripping the group tag and returning the stem
    if the structured parser doesn't match.

    Returns an empty string if no series name could be extracted.
    """
    with contextlib.suppress(Exception):
        from renamer.hash_organizer import extract_series_from_filename

        info = extract_series_from_filename(filename)
        if info and info.series_name:
            return info.series_name

    # Fallback: strip leading [Group] tag and return cleaned stem
    stem = Path(filename).stem
    cleaned = GROUP_PATTERN.sub("", stem).strip()
    # Remove trailing episode-like patterns: - 01, - S01E01, etc.
    cleaned = re.sub(r"\s*[-–]\s*(?:S\d+E\d+|\d{1,4})(?:v\d+)?\s*.*$", "", cleaned).strip()
    # Remove bracketed tags like [1080p], [HEVC]
    cleaned = re.sub(r"\s*\[[^\]]*\]", "", cleaned).strip()
    cleaned = cleaned.strip(" .-_–—")
    return cleaned if cleaned else ""


def _clean_folder_name_for_search(folder_name: str) -> str:
    """
    Clean a folder name for use as a TMDB/AniList search query.

    Strips common suffixes and brackets that would cause search failures,
    and returns an empty string for non-searchable names like ``"."``
    or generic directory names like ``"Downloads"``, ``"Videos"``.
    """
    name = folder_name.strip()
    # Reject clearly non-searchable names
    if not name or name in (".", "..") or not re.search(r"\w", name):
        return ""
    # Reject generic directory names that are never anime titles
    _GENERIC_DIRS = frozenset(
        {
            "downloads",
            "download",
            "videos",
            "video",
            "torrent",
            "torrents",
            "media",
            "anime",
            "tv",
            "movies",
            "movie",
            "series",
            "shows",
            "music",
            "incomplete",
            "completed",
            "archive",
            "temp",
            "tmp",
            "new",
            "old",
            "backup",
        }
    )
    if name.lower() in _GENERIC_DIRS:
        return ""
    # Strip year suffix: "Series (2024)" -> "Series"
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip()
    # Strip resolution tags: "Series [1080p]" -> "Series"
    name = re.sub(r"\s*\[[^\]]*\]", "", name).strip()
    return name.strip(" .-_–—")
