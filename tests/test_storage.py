"""
tests/test_storage.py
=====================
Unit tests for RenameHistory and SeriesCache.
"""

import json
from pathlib import Path

import pytest

from renamer.cache import SeriesCache
from renamer.config import Provider
from renamer.history import RenameHistory


# ---------------------------------------------------------------------------
# RenameHistory
# ---------------------------------------------------------------------------

class TestRenameHistory:
    def test_load_returns_empty_if_no_file(self, tmp_path):
        h = RenameHistory(tmp_path / "history.json")
        assert h.load() == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "history.json"
        h = RenameHistory(path)
        data = {str(tmp_path / "new.mkv"): str(tmp_path / "old.mkv")}
        h.save(data)
        loaded = h.load()
        assert loaded == data

    def test_save_merges_with_existing(self, tmp_path):
        path = tmp_path / "history.json"
        h = RenameHistory(path)
        h.save({"/new1.mkv": "/old1.mkv"})
        h.save({"/new2.mkv": "/old2.mkv"})
        loaded = h.load()
        assert "/new1.mkv" in loaded
        assert "/new2.mkv" in loaded

    def test_clear_entries_removes_keys(self, tmp_path):
        path = tmp_path / "history.json"
        h = RenameHistory(path)
        h.save({"/new1.mkv": "/old1.mkv", "/new2.mkv": "/old2.mkv"})
        h.clear_entries(["/new1.mkv"])
        loaded = h.load()
        assert "/new1.mkv" not in loaded
        assert "/new2.mkv" in loaded

    def test_load_upgrades_relative_paths(self, tmp_path):
        """Legacy entries with relative paths should be resolved against media_dir."""
        path = tmp_path / "history.json"
        # Write a legacy-format history with relative paths
        legacy = {"new.mkv": "old.mkv"}
        path.write_text(json.dumps(legacy))
        h = RenameHistory(path, media_dir=tmp_path)
        loaded = h.load()
        # Keys should now be absolute
        for key in loaded:
            assert Path(key).is_absolute()

    def test_load_handles_corrupt_json(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text("not json {{")
        h = RenameHistory(path)
        assert h.load() == {}

    def test_clear_nonexistent_key_is_noop(self, tmp_path):
        path = tmp_path / "history.json"
        h = RenameHistory(path)
        h.save({"/new.mkv": "/old.mkv"})
        h.clear_entries(["/does_not_exist.mkv"])
        assert h.load() == {"/new.mkv": "/old.mkv"}


# ---------------------------------------------------------------------------
# SeriesCache
# ---------------------------------------------------------------------------

class TestSeriesCache:
    def test_load_returns_none_if_no_cache(self, tmp_path):
        cache = SeriesCache(tmp_path)
        assert cache.load() is None

    def test_save_and_load_roundtrip(self, tmp_path):
        cache = SeriesCache(tmp_path)
        cache.save(
            series_name="Attack on Titan",
            provider=Provider.TMDB,
            tmdb_series_id=1429,
            anilist_id=None,
            kitsu_id=None,
        )
        data = cache.load()
        assert data is not None
        assert data["series_name"] == "Attack on Titan"
        assert data["tmdb_series_id"] == 1429
        assert data["provider"] == Provider.TMDB

    def test_provider_is_enum_after_load(self, tmp_path):
        cache = SeriesCache(tmp_path)
        cache.save("Naruto", Provider.AniList)
        data = cache.load()
        assert isinstance(data["provider"], Provider)
        assert data["provider"] == Provider.AniList

    def test_clear_removes_cache_file(self, tmp_path):
        cache = SeriesCache(tmp_path)
        cache.save("Test", Provider.Kitsu)
        assert (tmp_path / ".series_cache.json").exists()
        cache.clear()
        assert not (tmp_path / ".series_cache.json").exists()

    def test_clear_is_noop_if_no_file(self, tmp_path):
        cache = SeriesCache(tmp_path)
        cache.clear()  # Should not raise

    def test_load_handles_corrupt_json(self, tmp_path):
        (tmp_path / ".series_cache.json").write_text("{ bad json }")
        cache = SeriesCache(tmp_path)
        assert cache.load() is None

    def test_saves_all_optional_fields(self, tmp_path):
        cache = SeriesCache(tmp_path)
        cache.save(
            series_name="One Piece",
            provider=Provider.TMDB,
            tmdb_series_id=37854,
            anilist_id=21,
            kitsu_id=12,
            episode_group_id="abc123",
            episode_start_mode="per_season",
        )
        data = cache.load()
        assert data["anilist_id"] == 21
        assert data["kitsu_id"] == 12
        assert data["episode_group_id"] == "abc123"
        assert data["episode_start_mode"] == "per_season"
