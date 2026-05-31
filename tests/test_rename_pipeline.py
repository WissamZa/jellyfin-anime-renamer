"""
tests/test_rename_pipeline.py
==============================
Integration-level tests for the full rename pipeline.
Uses a temp directory with fake video files — no network calls.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from renamer.config import Config
from renamer.providers.base import EpisodeInfo
from renamer.renamer import AnimeRenamer


def _make_episode_map(*titles: str, season: int = 1) -> dict[int, EpisodeInfo]:
    return {
        i: EpisodeInfo(absolute=i, season=season, episode=i, title=t)
        for i, t in enumerate(titles, start=1)
    }


@pytest.fixture
def media_dir(tmp_path):
    """A temporary directory with a handful of fake .mkv files."""
    files = [
        "Show 01.mkv",
        "Show 02.mkv",
        "Show 03.mkv",
    ]
    for name in files:
        (tmp_path / name).touch()
    return tmp_path


@pytest.fixture
def cfg(media_dir):
    return Config(
        tmdb_api_key="fake",
        series_name="Test Show",
        tmdb_series_id=999,
        media_dir=media_dir,
        base_download_path=Path("/some/other/path"),
        organize_into_folders=False,
    )


class TestDryRun:
    def test_returns_results_for_all_files(self, cfg, media_dir):
        ep_map = _make_episode_map("First", "Second", "Third")
        renamer = AnimeRenamer(cfg)

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            results = renamer._process_files(ep_map, {}, dry_run=True)

        assert len(results) == 3
        assert all(r.success for r in results)

    def test_no_files_modified_in_dry_run(self, cfg, media_dir):
        ep_map = _make_episode_map("First", "Second", "Third")
        renamer = AnimeRenamer(cfg)
        original_names = {p.name for p in media_dir.iterdir()}

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            renamer._process_files(ep_map, {}, dry_run=True)

        assert {p.name for p in media_dir.iterdir()} == original_names


class TestLiveRename:
    def test_renames_files(self, cfg, media_dir):
        ep_map = _make_episode_map("First", "Second", "Third")
        renamer = AnimeRenamer(cfg)

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            results = renamer._process_files(ep_map, {}, dry_run=False)

        assert all(r.success for r in results)
        # All .mkv files should now have proper names
        renamed = {p.name for p in media_dir.glob("*.mkv")}
        assert "Test Show - S01E01 - First.mkv" in renamed

    def test_creates_history_file(self, cfg, media_dir):
        ep_map = _make_episode_map("Alpha", "Beta", "Gamma")
        renamer = AnimeRenamer(cfg)

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            renamer._process_files(ep_map, {}, dry_run=False)

        assert cfg.history_file.exists()
        history = json.loads(cfg.history_file.read_text())
        assert len(history) == 3


class TestOrganiseIntoFolders:
    def test_creates_season_folder(self, tmp_path):
        (tmp_path / "Show 01.mkv").touch()
        cfg = Config(
            tmdb_api_key="fake",
            series_name="Test Show",
            media_dir=tmp_path,
            base_download_path=Path("/some/other/path"),
            organize_into_folders=True,
        )
        ep_map = _make_episode_map("First")
        renamer = AnimeRenamer(cfg)

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            renamer._process_files(ep_map, {}, dry_run=False)

        assert (tmp_path / "Season 01").is_dir()
        assert (tmp_path / "Season 01" / "Test Show - S01E01 - First.mkv").exists()


class TestUndo:
    def test_undo_restores_files(self, cfg, media_dir, monkeypatch):
        ep_map = _make_episode_map("First", "Second", "Third")
        renamer = AnimeRenamer(cfg)

        with patch.object(renamer, "_auto_search_series"), \
             patch.object(renamer, "_load_cache"), \
             patch.object(renamer, "_save_cache"), \
             patch.object(renamer, "_cross_reference_ids"):
            renamer._process_files(ep_map, {}, dry_run=False)

        original_names = {"Show 01.mkv", "Show 02.mkv", "Show 03.mkv"}

        monkeypatch.setattr("builtins.input", lambda _: "y")
        renamer.undo()

        current_names = {p.name for p in media_dir.glob("*.mkv")}
        assert current_names == original_names
