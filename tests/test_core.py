"""
tests/test_core.py
==================
Unit tests for the core, side-effect-free functions.
"""

from pathlib import Path

import pytest

from renamer.config import START_MODE_CONTINUING, START_MODE_PER_SEASON, Config, Provider
from renamer.parsers import EpisodeNumberParser, SpecialParser
from renamer.providers.base import EpisodeInfo, RenameResult
from renamer.renamer import (
    AnimeRenamer,
    _clean_special_title,
    _compute_display_episode,
    _format_episode_number,
    sanitize_name,
)

# ---------------------------------------------------------------------------
# sanitize_name
# ---------------------------------------------------------------------------

class TestSanitizeName:
    def test_removes_illegal_chars(self):
        assert sanitize_name('Show: "Title"') == "Show Title"

    def test_normalises_em_dash(self):
        assert sanitize_name("Re\u2014Zero") == "Re-Zero"

    def test_normalises_ellipsis(self):
        assert sanitize_name("Title\u2026Continued") == "Title...Continued"

    def test_collapses_spaces(self):
        assert sanitize_name("Two  Spaces") == "Two Spaces"

    def test_strips_leading_dot(self):
        assert sanitize_name(".Hidden") == "Hidden"

    def test_strips_trailing_hyphen(self):
        assert sanitize_name("Title -") == "Title"

    def test_empty_string(self):
        assert sanitize_name("") == ""


# ---------------------------------------------------------------------------
# _clean_special_title
# ---------------------------------------------------------------------------

class TestCleanSpecialTitle:
    def test_removes_resolution_bracket(self):
        result = _clean_special_title("OVA [1080p][SubGroup]")
        assert "1080p" not in result

    def test_keeps_meaningful_content(self):
        result = _clean_special_title("Violet Evergarden (Memory Snow)")
        assert "Memory Snow" in result

    def test_strips_codec(self):
        result = _clean_special_title("Episode HEVC x265")
        assert "HEVC" not in result
        assert "x265" not in result


# ---------------------------------------------------------------------------
# _format_episode_number
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (1, "01"),
    (9, "09"),
    (10, "10"),
    (99, "99"),
    (100, "100"),
    (999, "999"),
    (1000, "1000"),
    (1200, "1200"),
])
def test_format_episode_number(n, expected):
    assert _format_episode_number(n) == expected


# ---------------------------------------------------------------------------
# _compute_display_episode
# ---------------------------------------------------------------------------

class TestComputeDisplayEpisode:
    def _info(self, season=1, episode=5, absolute=17):
        return EpisodeInfo(absolute=absolute, season=season, episode=episode, title="")

    def _cfg(self, **kwargs):
        return Config(**kwargs)

    def test_per_season_returns_episode(self):
        info = self._info()
        cfg = self._cfg(episode_start_mode=START_MODE_PER_SEASON)
        assert _compute_display_episode(info, cfg, {}) == 5

    def test_absolute_numbering(self):
        info = self._info()
        cfg = self._cfg(absolute_numbering=True)
        assert _compute_display_episode(info, cfg, {}) == 17

    def test_continuing_mode_adds_offset(self):
        info = self._info(season=2, episode=3)
        cfg = self._cfg(episode_start_mode=START_MODE_CONTINUING)
        offsets = {1: 0, 2: 12}  # S1 had 12 eps
        assert _compute_display_episode(info, cfg, offsets) == 15  # 3 + 12

    def test_continuing_mode_missing_season_returns_episode(self):
        info = self._info(season=3, episode=1)
        cfg = self._cfg(episode_start_mode=START_MODE_CONTINUING)
        offsets = {1: 0, 2: 12}  # season 3 not in offsets
        assert _compute_display_episode(info, cfg, offsets) == 1


# ---------------------------------------------------------------------------
# EpisodeNumberParser
# ---------------------------------------------------------------------------

class TestEpisodeNumberParser:
    @pytest.fixture
    def parser(self):
        return EpisodeNumberParser()

    @pytest.mark.parametrize("filename,expected", [
        ("Show S01E05.mkv", 5),
        ("Show - 1080p - [SubGroup] - ep 42.mkv", 42),
        ("Show [1105].mkv", 1105),
        ("Show - 27.mkv", 27),
        ("unrecognised.mkv", None),
    ])
    def test_parse(self, parser, filename, expected):
        assert parser.parse(filename) == expected

    @pytest.mark.parametrize("filename,expected_season,expected_ep", [
        ("Show S02E15.mkv", 2, 15),
        ("Show 2x08.mkv", 2, 8),
        ("Show Season 3 - 05.mkv", 3, 5),
        ("Show S00E03.mkv", 0, 3),
        ("no_season.mkv", None, None),
    ])
    def test_parse_season_episode(self, parser, filename, expected_season, expected_ep):
        s, e = parser.parse_season_episode(filename)
        assert s == expected_season
        assert e == expected_ep


# ---------------------------------------------------------------------------
# SpecialParser
# ---------------------------------------------------------------------------

class TestSpecialParser:
    @pytest.mark.parametrize("filename,expected", [
        ("Show SP1.mkv", 1),
        ("Show OVA 3.mkv", 3),
        ("Show OVA.mkv", 0),
        ("Show NCOP.mkv", 0),
        ("Show Episode 5.mkv", None),
        ("Show S01E02.mkv", None),
    ])
    def test_parse(self, filename, expected):
        assert SpecialParser.parse(filename) == expected


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_provider_is_tmdb(self):
        cfg = Config()
        assert cfg.provider == Provider.TMDB

    def test_validate_missing_api_key(self):
        cfg = Config(media_dir=Path("."))
        errors = cfg.validate()
        assert any("TMDB_API_KEY" in e for e in errors)

    def test_validate_missing_media_dir(self, tmp_path):
        cfg = Config(tmdb_api_key="key", media_dir=tmp_path / "nonexistent")
        errors = cfg.validate()
        assert any("MEDIA_DIR" in e for e in errors)

    def test_validate_passes_with_anilist(self, tmp_path):
        cfg = Config(provider=Provider.AniList, media_dir=tmp_path)
        errors = cfg.validate()
        assert errors == []

    def test_history_file_property(self, tmp_path):
        cfg = Config(media_dir=tmp_path)
        assert cfg.history_file == tmp_path / "rename_history.json"


# ---------------------------------------------------------------------------
# RenameResult
# ---------------------------------------------------------------------------

class TestRenameResult:
    def _info(self):
        return EpisodeInfo(absolute=1, season=1, episode=1, title="Title")

    def test_success_property(self):
        r = RenameResult("old.mkv", "new.mkv", self._info())
        r.success = True
        assert r.success is True
        assert r.skipped is False

    def test_skipped_property(self):
        r = RenameResult("old.mkv", "new.mkv", self._info())
        r.skipped = True
        assert r.skipped is True
        assert r.success is False


# ---------------------------------------------------------------------------
# AnimeRenamer._compute_season_offsets
# ---------------------------------------------------------------------------

class TestComputeSeasonOffsets:
    def _make_map(self, *seasons_with_eps):
        """Build a simple episode_map for (season, num_eps) pairs."""
        m = {}
        abs_n = 1
        for season, count in seasons_with_eps:
            for ep in range(1, count + 1):
                m[abs_n] = EpisodeInfo(abs_n, season, ep, f"Ep {ep}")
                abs_n += 1
        return m

    def test_single_season(self):
        em = self._make_map((1, 12))
        offsets = AnimeRenamer._compute_season_offsets(em)
        assert offsets == {1: 0}

    def test_two_seasons(self):
        em = self._make_map((1, 12), (2, 13))
        offsets = AnimeRenamer._compute_season_offsets(em)
        assert offsets == {1: 0, 2: 12}

    def test_specials_ignored(self):
        em = self._make_map((0, 3), (1, 10))  # season 0 = specials
        offsets = AnimeRenamer._compute_season_offsets(em)
        assert 0 not in offsets
        assert offsets[1] == 0
