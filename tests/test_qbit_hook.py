"""
tests/test_qbit_hook.py
=======================
Tests for qbit_hook utility functions and the LibraryIndex class.
"""

import json

import pytest

from qbit_hook import find_matching_folder, normalize_for_matching
from renamer.library_index import LibraryIndex, reset_library_index

# ─────────────────────────────────────────────────────────────────────────────
# normalize_for_matching
# ─────────────────────────────────────────────────────────────────────────────


def test_normalize_for_matching():
    # Romaji spelling variants
    assert (
        normalize_for_matching("Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U")
        == "jidohanbaikiniumarekawattaorewameikyuohokou"
    )
    assert (
        normalize_for_matching("Jidouhanbaiki ni Umarekawatta Ore wa Meikyuu o Samayou")
        == "jidohanbaikiniumarekawattaorewameikyuosamayo"
    )

    # Season stripping
    base_shingeki = normalize_for_matching("Shingeki no Kyojin")
    assert normalize_for_matching("Shingeki no Kyojin Season 3") == base_shingeki
    assert normalize_for_matching("Shingeki no Kyojin 3rd Season") == base_shingeki

    base_mushoku = normalize_for_matching("Mushoku Tensei: Jobless Reincarnation")
    assert (
        normalize_for_matching("Mushoku Tensei: Jobless Reincarnation Season 2 Part 2")
        == base_mushoku
    )

    base_spice = normalize_for_matching("Spice and Wolf: Merchant Meets the Wise Wolf")
    assert (
        normalize_for_matching("Spice and Wolf: Merchant Meets the Wise Wolf Part II") == base_spice
    )


# ─────────────────────────────────────────────────────────────────────────────
# find_matching_folder
# ─────────────────────────────────────────────────────────────────────────────


def test_find_matching_folder(tmp_path):
    # Setup test directories
    base = tmp_path
    target_dir = base / "Jidouhanbaiki ni Umarekawatta Ore wa Meikyuu o Samayou"
    target_dir.mkdir()

    # Matching folder should be found even with spelling variations and season suffixes
    matched = find_matching_folder(
        base,
        candidates=[
            "Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U",
            "Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U 2nd Season",
        ],
    )
    assert matched == target_dir

    # Exact mismatch should return None
    assert find_matching_folder(base, candidates=["Different Show", "Another Different"]) is None


# ─────────────────────────────────────────────────────────────────────────────
# LibraryIndex — unit tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Always reset the global singleton between tests."""
    reset_library_index()
    yield
    reset_library_index()


@pytest.fixture
def index(tmp_path) -> LibraryIndex:
    """Return a fresh LibraryIndex backed by a temp directory."""
    return LibraryIndex(tmp_path / "library_index.json")


class TestLibraryIndexUpdate:
    def test_update_creates_entry(self, index, tmp_path):
        folder = tmp_path / "One Piece"
        folder.mkdir()
        index.update(folder, "One Piece", tmdb_id=37854, anilist_id=21)
        assert len(index) == 1

    def test_update_preserves_existing_ids(self, index, tmp_path):
        folder = tmp_path / "One Piece"
        folder.mkdir()
        # First update — set tmdb_id
        index.update(folder, "One Piece", tmdb_id=37854)
        # Second update — add anilist_id, omit tmdb_id (should not clobber it)
        index.update(folder, "One Piece", anilist_id=21)

        entries = index.entries()
        key = str(folder.resolve())
        assert entries[key]["tmdb_id"] == 37854
        assert entries[key]["anilist_id"] == 21

    def test_update_same_folder_twice_single_entry(self, index, tmp_path):
        folder = tmp_path / "Naruto"
        folder.mkdir()
        index.update(folder, "Naruto", tmdb_id=1)
        index.update(folder, "Naruto", tmdb_id=1)
        assert len(index) == 1


class TestLibraryIndexFindByIds:
    def test_find_by_tmdb_id(self, index, tmp_path):
        folder = tmp_path / "One Piece"
        folder.mkdir()
        index.update(folder, "One Piece", tmdb_id=37854)
        result = index.find_by_ids(tmdb_id=37854)
        assert result == folder

    def test_find_by_anilist_id(self, index, tmp_path):
        folder = tmp_path / "One Piece"
        folder.mkdir()
        index.update(folder, "One Piece", anilist_id=21)
        result = index.find_by_ids(anilist_id=21)
        assert result == folder

    def test_find_by_kitsu_id(self, index, tmp_path):
        folder = tmp_path / "One Piece"
        folder.mkdir()
        index.update(folder, "One Piece", kitsu_id=12)
        result = index.find_by_ids(kitsu_id=12)
        assert result == folder

    def test_no_match_returns_none(self, index, tmp_path):
        folder = tmp_path / "Naruto"
        folder.mkdir()
        index.update(folder, "Naruto", tmdb_id=1234)
        result = index.find_by_ids(tmdb_id=99999)
        assert result is None

    def test_all_ids_none_returns_none(self, index):
        result = index.find_by_ids()
        assert result is None

    def test_stale_entry_removed_returns_none(self, index, tmp_path):
        """If the folder no longer exists, find_by_ids removes the entry and returns None."""
        ghost_folder = tmp_path / "Ghost Series"
        # Do NOT create it — simulate a deleted folder
        index._data[str(ghost_folder.resolve())] = {
            "name": "Ghost Series",
            "tmdb_id": 42,
            "anilist_id": None,
            "kitsu_id": None,
            "anidb_aid": None,
            "updated_at": "2026-01-01T00:00:00",
        }
        result = index.find_by_ids(tmdb_id=42)
        assert result is None
        # Entry should have been removed
        assert str(ghost_folder.resolve()) not in index.entries()

    def test_case_insensitive_folder_name_resolved_by_id(self, index, tmp_path):
        """The key scenario: folder is 'One Piece' but torrent says 'ONE PIECE'.
        With the same TMDB ID resolved for both, the index returns the correct folder."""
        one_piece_folder = tmp_path / "One Piece"
        one_piece_folder.mkdir()
        index.update(one_piece_folder, "One Piece", tmdb_id=37854)

        # Hook resolves 'ONE PIECE' torrent → tmdb_id=37854 → finds 'One Piece' folder
        result = index.find_by_ids(tmdb_id=37854)
        assert result == one_piece_folder
        assert result.name == "One Piece"  # not "ONE PIECE"


class TestLibraryIndexPersistence:
    def test_save_and_reload(self, tmp_path):
        idx_path = tmp_path / "library_index.json"
        folder = tmp_path / "One Piece"
        folder.mkdir()

        idx1 = LibraryIndex(idx_path)
        idx1.update(folder, "One Piece", tmdb_id=37854, anilist_id=21)
        idx1.save()

        # Load fresh instance from the same file
        idx2 = LibraryIndex(idx_path)
        assert len(idx2) == 1
        result = idx2.find_by_ids(tmdb_id=37854)
        assert result == folder

    def test_save_atomic_temp_file_cleanup(self, tmp_path):
        """After save(), the .tmp file should NOT exist."""
        idx_path = tmp_path / "library_index.json"
        folder = tmp_path / "Naruto"
        folder.mkdir()

        idx = LibraryIndex(idx_path)
        idx.update(folder, "Naruto", tmdb_id=1234)
        idx.save()

        assert idx_path.exists()
        assert not (tmp_path / "library_index.json.tmp").exists()


class TestLibraryIndexRebuild:
    def test_rebuild_reads_series_cache(self, tmp_path):
        """rebuild() should pick up IDs from .series_cache.json files."""
        base = tmp_path / "Torrent"
        base.mkdir()

        op_folder = base / "One Piece"
        op_folder.mkdir()
        cache = {
            "series_name": "One Piece",
            "provider": "tmdb",
            "tmdb_series_id": 37854,
            "anilist_id": 21,
            "kitsu_id": None,
        }
        (op_folder / ".series_cache.json").write_text(json.dumps(cache), encoding="utf-8")

        idx = LibraryIndex(tmp_path / "library_index.json")
        count = idx.rebuild(base)

        assert count == 1
        result = idx.find_by_ids(tmdb_id=37854)
        assert result == op_folder

    def test_rebuild_skips_folders_without_cache(self, tmp_path):
        """Folders with no .series_cache.json are skipped (added on next hook run)."""
        base = tmp_path / "Torrent"
        base.mkdir()
        (base / "Uncached Series").mkdir()

        idx = LibraryIndex(tmp_path / "library_index.json")
        count = idx.rebuild(base)
        assert count == 0

    def test_rebuild_nonexistent_base_path(self, tmp_path):
        idx = LibraryIndex(tmp_path / "library_index.json")
        count = idx.rebuild(tmp_path / "does_not_exist")
        assert count == 0

    def test_rebuild_multiple_folders(self, tmp_path):
        base = tmp_path / "Torrent"
        base.mkdir()

        series = [
            ("One Piece", 37854, 21),
            ("Naruto", 46260, 20),
            ("Bleach", 30984, 269),
        ]
        for name, tmdb, al in series:
            f = base / name
            f.mkdir()
            cache = {
                "series_name": name,
                "provider": "tmdb",
                "tmdb_series_id": tmdb,
                "anilist_id": al,
                "kitsu_id": None,
            }
            (f / ".series_cache.json").write_text(json.dumps(cache), encoding="utf-8")

        idx = LibraryIndex(tmp_path / "library_index.json")
        count = idx.rebuild(base)

        assert count == 3
        assert idx.find_by_ids(tmdb_id=37854) == base / "One Piece"
        assert idx.find_by_ids(anilist_id=20) == base / "Naruto"
        assert idx.find_by_ids(tmdb_id=30984) == base / "Bleach"
