"""
tests/test_title_matching.py
============================
Unit and integration tests for title similarity-based episode matching.
"""

from pathlib import Path
import pytest

from renamer.providers.base import EpisodeInfo, RenameResult
from renamer.renamer import (
    _normalize_title,
    _extract_title_suffix,
    _find_best_title_match,
    AnimeRenamer,
)
from renamer.config import Config


def test_normalize_title():
    assert _normalize_title("Come In, World - Vegapunk's Message") == "comeinworldvegapunksmessage"
    assert _normalize_title("[1080p] Powers on a Different Level! Luffy vs. Lucci (Dual-Audio)") == "powersonadifferentlevelluffyvslucci"
    assert _normalize_title("One Piece - S22E1141") == "onepieces22e1141"
    assert _normalize_title("ルフィ") == "ルフィ"


def test_extract_title_suffix():
    assert _extract_title_suffix("One Piece - S22E1141 - Come In, World - Vegapunk's Message.mkv") == "Come In, World - Vegapunk's Message"
    assert _extract_title_suffix("One Piece - 1141 - Come In, World.mkv") == "Come In, World"
    assert _extract_title_suffix("[Subs] One Piece - 1141 [1080p].mkv") == "[1080p]"
    assert _extract_title_suffix("One Piece S22E1100 The Strongest Form.mkv") == "The Strongest Form"


def test_find_best_title_match():
    ep_map = {
        1: EpisodeInfo(absolute=1, season=22, episode=56, title="Reliable Reinforcements! Dorry and Brogy Arrive!"),
        2: EpisodeInfo(absolute=2, season=22, episode=57, title="Come In, World - Vegapunk's Message"),
        3: EpisodeInfo(absolute=3, season=22, episode=58, title="Vegapunk's Secret Plan"),
    }

    # Exact match
    ep, ratio = _find_best_title_match("Come In, World - Vegapunk's Message", ep_map)
    assert ep is not None
    assert ep.episode == 57
    assert ratio == 1.0

    # Slight mismatch / typo
    ep, ratio = _find_best_title_match("Come In World Vegapunks Message", ep_map)
    assert ep is not None
    assert ep.episode == 57
    assert ratio > 0.90

    # Different episode matching
    ep, ratio = _find_best_title_match("Reliable Reinforcements", ep_map)
    assert ep is not None
    assert ep.episode == 56
    assert ratio >= 0.85

    # No match above threshold
    ep, ratio = _find_best_title_match("Random Title That Does Not Match", ep_map)
    assert ratio < 0.85


def test_classify_and_handle_overrides_incorrect_numeric_match(tmp_path):
    """
    Test that _classify_and_handle overrides a wrong numeric match if the
    title suffix in the filename matches a different episode with high similarity.
    """
    cfg = Config(
        tmdb_api_key="fake",
        series_name="One Piece",
        media_dir=tmp_path,
        base_download_path=tmp_path.parent,
        organize_into_folders=False,
    )
    renamer = AnimeRenamer(cfg)

    # Setup maps where original (22, 1141) maps to ep 56 (Reliable Reinforcements)
    # but the filename title matches ep 57 (Come In, World - Vegapunk's Message)
    ep56 = EpisodeInfo(absolute=1140, season=22, episode=56, title="Reliable Reinforcements! Dorry and Brogy Arrive!")
    ep57 = EpisodeInfo(absolute=1141, season=22, episode=57, title="Come In, World - Vegapunk's Message")

    episode_map = {1140: ep56, 1141: ep57}
    specials_map = {}
    season_ep_map = {(22, 56): ep56, (22, 57): ep57}
    original_se_map = {(22, 1141): ep56, (22, 1142): ep57}

    file_path = tmp_path / "One Piece - S22E1141 - Come In, World - Vegapunk's Message.mkv"
    file_path.touch()

    # Call _classify_and_handle
    result, next_special = renamer._classify_and_handle(
        path=file_path,
        episode_map=episode_map,
        specials_map=specials_map,
        season_ep_map=season_ep_map,
        season_offsets={},
        next_auto_special=1,
        dry_run=True,
        session_history={},
        claimed_dests=set(),
        original_se_map=original_se_map,
    )

    assert result.status == RenameResult.Status.SUCCESS
    # S22E1141 (numeric match would be ep 56) must be overridden to ep 57 by title match
    assert result.episode.episode == 57
    assert result.episode.title == "Come In, World - Vegapunk's Message"
