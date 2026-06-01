"""
tests/test_config.py
====================
Tests for Config.from_env(), Config.validate(), and Provider.from_str().
"""

import os
from pathlib import Path

import pytest

from renamer.config import (
    START_MODE_CONTINUING,
    START_MODE_PER_SEASON,
    Config,
    Provider,
    get_logger,
)


# ---------------------------------------------------------------------------
# Provider.from_str
# ---------------------------------------------------------------------------

class TestProviderFromStr:
    def test_tmdb_case_insensitive(self):
        assert Provider.from_str("TMDB") == Provider.TMDB
        assert Provider.from_str("tmdb") == Provider.TMDB
        assert Provider.from_str("Tmdb") == Provider.TMDB

    def test_anilist(self):
        assert Provider.from_str("anilist") == Provider.AniList

    def test_kitsu(self):
        assert Provider.from_str("kitsu") == Provider.Kitsu

    def test_anidb(self):
        assert Provider.from_str("anidb") == Provider.AniDB

    def test_unknown_defaults_to_tmdb(self):
        assert Provider.from_str("garbage") == Provider.TMDB

    def test_whitespace_stripped(self):
        assert Provider.from_str("  tmdb  ") == Provider.TMDB


# ---------------------------------------------------------------------------
# Config defaults and types
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_default_provider(self):
        cfg = Config()
        assert cfg.provider == Provider.TMDB

    def test_default_episode_start_mode(self):
        cfg = Config()
        assert cfg.episode_start_mode == START_MODE_PER_SEASON

    def test_media_dir_coerced_to_path(self):
        cfg = Config(media_dir="/tmp")
        assert isinstance(cfg.media_dir, Path)

    def test_none_provider_becomes_tmdb(self):
        # Simulates CLI passing provider=None when flag not given
        cfg = Config(provider=None)  # type: ignore[arg-type]
        assert cfg.provider == Provider.TMDB

    def test_all_media_extensions_combined(self):
        cfg = Config()
        exts = cfg.all_media_extensions
        assert ".mkv" in exts
        assert ".srt" in exts
        assert ".ass" in exts


# ---------------------------------------------------------------------------
# Config.validate
# ---------------------------------------------------------------------------

class TestConfigValidate:
    def test_missing_tmdb_key_gives_error(self):
        cfg = Config(media_dir=Path("."))
        errors = cfg.validate()
        assert any("TMDB_API_KEY" in e for e in errors)

    def test_anilist_needs_no_api_key(self, tmp_path):
        cfg = Config(provider=Provider.AniList, media_dir=tmp_path)
        assert cfg.validate() == []

    def test_kitsu_needs_no_api_key(self, tmp_path):
        cfg = Config(provider=Provider.Kitsu, media_dir=tmp_path)
        assert cfg.validate() == []

    def test_anidb_requires_username_and_password(self, tmp_path):
        cfg = Config(provider=Provider.AniDB, media_dir=tmp_path)
        errors = cfg.validate()
        assert any("ANIDB_USERNAME" in e for e in errors)
        assert any("ANIDB_PASSWORD" in e for e in errors)

    def test_anidb_valid_with_credentials(self, tmp_path):
        cfg = Config(
            provider=Provider.AniDB,
            anidb_username="user",
            anidb_password="pass",
            media_dir=tmp_path,
        )
        assert cfg.validate() == []

    def test_nonexistent_media_dir_gives_error(self, tmp_path):
        cfg = Config(tmdb_api_key="key", media_dir=tmp_path / "does_not_exist")
        errors = cfg.validate()
        assert any("MEDIA_DIR" in e for e in errors)

    def test_valid_config_no_errors(self, tmp_path):
        cfg = Config(tmdb_api_key="key", media_dir=tmp_path)
        assert cfg.validate() == []

    def test_invalid_episode_start_mode(self, tmp_path):
        cfg = Config(tmdb_api_key="key", media_dir=tmp_path, episode_start_mode="bad_mode")
        errors = cfg.validate()
        assert any("EPISODE_START_MODE" in e for e in errors)


# ---------------------------------------------------------------------------
# Config properties
# ---------------------------------------------------------------------------

class TestConfigProperties:
    def test_history_file_path(self, tmp_path):
        cfg = Config(media_dir=tmp_path)
        assert cfg.history_file == tmp_path / "rename_history.json"

    def test_all_media_extensions_is_tuple(self):
        cfg = Config()
        assert isinstance(cfg.all_media_extensions, tuple)


# ---------------------------------------------------------------------------
# Config.from_env
# ---------------------------------------------------------------------------

class TestConfigFromEnv:
    def test_reads_tmdb_api_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMDB_API_KEY", "test_key_123")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.tmdb_api_key == "test_key_123"

    def test_reads_provider(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROVIDER", "anilist")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.provider == Provider.AniList

    def test_reads_organize_flag_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ORGANIZE_INTO_FOLDERS", "false")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.organize_into_folders is False

    def test_reads_absolute_numbering(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ABSOLUTE_NUMBERING", "true")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.absolute_numbering is True

    def test_overrides_take_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMDB_API_KEY", "env_key")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env(tmdb_api_key="override_key")
        assert cfg.tmdb_api_key == "override_key"

    def test_invalid_scan_depth_defaults_to_3(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SCAN_DEPTH", "not_a_number")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.scan_depth == 3

    def test_reads_episode_start_mode_continuing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EPISODE_START_MODE", START_MODE_CONTINUING)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.episode_start_mode == START_MODE_CONTINUING

    def test_invalid_episode_start_mode_reverts_to_per_season(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EPISODE_START_MODE", "nonsense")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        cfg = Config.from_env()
        assert cfg.episode_start_mode == START_MODE_PER_SEASON


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class TestGetLogger:
    def test_returns_logger(self):
        import logging
        logger = get_logger("test_renamer_logger")
        assert isinstance(logger, logging.Logger)

    def test_same_name_returns_same_logger(self):
        logger1 = get_logger("my_unique_logger_xyz")
        logger2 = get_logger("my_unique_logger_xyz")
        assert logger1 is logger2

    def test_logger_does_not_propagate(self):
        logger = get_logger("no_propagate_test")
        assert logger.propagate is False
