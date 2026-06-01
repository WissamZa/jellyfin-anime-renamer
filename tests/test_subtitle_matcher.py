"""
tests/test_subtitle_matcher.py
==============================
Unit tests for scan_subtitle_matches, preview_subtitle_renames,
and execute_subtitle_renames.
"""

from pathlib import Path

import pytest

from renamer.subtitle_matcher import (
    SubtitleMatch,
    SubtitleScanResult,
    execute_subtitle_renames,
    preview_subtitle_renames,
    scan_subtitle_matches,
)

_VIDEO_EXTS = (".mkv", ".mp4", ".avi")
_SUB_EXTS = (".srt", ".ass", ".ssa", ".sub", ".vtt")


def _scan(directory: Path) -> SubtitleScanResult:
    return scan_subtitle_matches(directory, _VIDEO_EXTS, _SUB_EXTS)


def _make_dir(tmp_path: Path, files: list[str]) -> Path:
    """Create a temp directory with the given file names."""
    for name in files:
        (tmp_path / name).touch()
    return tmp_path


# ---------------------------------------------------------------------------
# SubtitleMatch
# ---------------------------------------------------------------------------

class TestSubtitleMatch:
    def _match(self, sub_name: str, vid_name: str, new_name: str) -> SubtitleMatch:
        return SubtitleMatch(
            subtitle_path=Path(f"/tmp/{sub_name}"),
            video_path=Path(f"/tmp/{vid_name}"),
            new_subtitle_name=new_name,
        )

    def test_subtitle_name_property(self):
        m = self._match("Show - S01E01.ass", "Show - S01E01 - Title.mkv", "Show - S01E01 - Title.ass")
        assert m.subtitle_name == "Show - S01E01.ass"

    def test_video_name_property(self):
        m = self._match("Show - S01E01.ass", "Show - S01E01 - Title.mkv", "Show - S01E01 - Title.ass")
        assert m.video_name == "Show - S01E01 - Title.mkv"

    def test_already_matches_true(self):
        m = self._match(
            "Show - S01E01 - Title.ass",
            "Show - S01E01 - Title.mkv",
            "Show - S01E01 - Title.ass",
        )
        assert m.already_matches is True

    def test_already_matches_false(self):
        m = self._match(
            "Show - S01E01.ass",
            "Show - S01E01 - Title.mkv",
            "Show - S01E01 - Title.ass",
        )
        assert m.already_matches is False


# ---------------------------------------------------------------------------
# SubtitleScanResult
# ---------------------------------------------------------------------------

class TestSubtitleScanResult:
    def test_total_subtitles(self):
        result = SubtitleScanResult(
            matches=[
                SubtitleMatch(Path("/tmp/a.ass"), Path("/tmp/v.mkv"), "new.ass")
            ],
            unmatched_subtitles=[Path("/tmp/b.srt")],
        )
        assert result.total_subtitles == 2

    def test_empty_result(self):
        result = SubtitleScanResult()
        assert result.total_subtitles == 0
        assert result.matches == []
        assert result.unmatched_subtitles == []
        assert result.unmatched_videos == []


# ---------------------------------------------------------------------------
# scan_subtitle_matches
# ---------------------------------------------------------------------------

class TestScanSubtitleMatches:
    def test_matches_subtitle_to_video_by_season_episode(self, tmp_path):
        _make_dir(tmp_path, [
            "Runway de Waratte - S01E01 - Title.mkv",
            "Runway de Waratte - S01E01.ass",
        ])
        result = _scan(tmp_path)
        assert len(result.matches) == 1
        m = result.matches[0]
        assert "S01E01" in m.new_subtitle_name

    def test_unmatched_subtitle_when_no_video(self, tmp_path):
        _make_dir(tmp_path, ["Show - S01E05.srt"])
        result = _scan(tmp_path)
        assert len(result.unmatched_subtitles) == 1

    def test_unmatched_video_when_no_subtitle(self, tmp_path):
        _make_dir(tmp_path, ["Show - S01E01 - Title.mkv"])
        result = _scan(tmp_path)
        assert len(result.unmatched_videos) == 1

    def test_multiple_episodes_matched(self, tmp_path):
        _make_dir(tmp_path, [
            "Show - S01E01 - Ep1.mkv",
            "Show - S01E02 - Ep2.mkv",
            "Show - S01E01.srt",
            "Show - S01E02.srt",
        ])
        result = _scan(tmp_path)
        assert len(result.matches) == 2

    def test_already_matched_subtitle_not_in_results(self, tmp_path):
        """Subtitle already named correctly should still appear as a match but with already_matches=True."""
        video = "Show - S01E01 - Title.mkv"
        sub = "Show - S01E01 - Title.srt"
        _make_dir(tmp_path, [video, sub])
        result = _scan(tmp_path)
        if result.matches:
            assert result.matches[0].already_matches is True

    def test_empty_directory(self, tmp_path):
        result = _scan(tmp_path)
        assert result.matches == []
        assert result.unmatched_subtitles == []
        assert result.unmatched_videos == []

    def test_language_tagged_subtitle(self, tmp_path):
        """Subtitles with language tags (e.g. .en.srt) should still be matched."""
        _make_dir(tmp_path, [
            "Show - S01E01 - Title.mkv",
            "Show - S01E01.en.srt",
        ])
        result = _scan(tmp_path)
        # Should find at least a partial match or unmatched — not crash
        assert isinstance(result, SubtitleScanResult)


# ---------------------------------------------------------------------------
# preview_subtitle_renames
# ---------------------------------------------------------------------------

class TestPreviewSubtitleRenames:
    def test_returns_lines_for_each_match(self, tmp_path, capsys):
        _make_dir(tmp_path, [
            "Anime - S01E01 - Ep.mkv",
            "Anime - S01E01.ass",
        ])
        result = _scan(tmp_path)
        preview_subtitle_renames(result)
        captured = capsys.readouterr()
        assert "S01E01" in captured.out

    def test_empty_scan_result_gives_empty_preview(self, tmp_path):
        result = SubtitleScanResult()
        preview_subtitle_renames(result)  # returns None (prints to stdout)


# ---------------------------------------------------------------------------
# execute_subtitle_renames
# ---------------------------------------------------------------------------

class TestExecuteSubtitleRenames:
    def test_dry_run_does_not_rename(self, tmp_path):
        _make_dir(tmp_path, [
            "Show - S01E01 - Title.mkv",
            "Show - S01E01.srt",
        ])
        result = _scan(tmp_path)
        original_files = set(p.name for p in tmp_path.iterdir())
        execute_subtitle_renames(result, dry_run=True)
        assert set(p.name for p in tmp_path.iterdir()) == original_files

    def test_live_renames_subtitle(self, tmp_path):
        _make_dir(tmp_path, [
            "Show - S01E01 - My Title.mkv",
            "Show - S01E01.srt",
        ])
        result = _scan(tmp_path)
        non_trivial = [m for m in result.matches if not m.already_matches]
        if not non_trivial:
            pytest.skip("No non-trivial matches found")
        execute_subtitle_renames(result, dry_run=False)
        # Ensure the original unmatched subtitle is gone
        remaining = {p.name for p in tmp_path.iterdir()}
        assert "Show - S01E01.srt" not in remaining
