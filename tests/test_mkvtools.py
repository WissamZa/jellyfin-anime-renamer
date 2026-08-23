"""
tests/test_mkvtools.py
======================
Offline tests for renamer.mkvtools — installer selection, subtitle
merging (argv construction + atomic replacement), and cover stripping.

subprocess and shutil.which are patched; no real tools are invoked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from renamer import mkvtools
from renamer.mkvtools import (
    build_merge_cmd,
    ensure_mkvtoolnix,
    find_tool,
    language_from_tag,
    merge_subtitle,
    merge_subtitles_batch,
    strip_covers,
)

# ---------------------------------------------------------------------------
# Tool discovery & installer
# ---------------------------------------------------------------------------


class TestFindTool:
    def test_returns_none_when_missing(self):
        with patch("renamer.mkvtools.shutil.which", return_value=None):
            assert find_tool("mkvmerge") is None

    def test_returns_path_when_present(self):
        with patch("renamer.mkvtools.shutil.which", return_value="/usr/bin/mkvmerge"):
            assert find_tool("mkvmerge") == "/usr/bin/mkvmerge"


class TestInstallCandidates:
    def test_linux_candidates_use_detected_managers(self):
        with patch("sys.platform", "linux"), patch(
            "renamer.mkvtools.shutil.which",
            side_effect=lambda n: {
                "apt-get": "/usr/bin/apt-get",
                "sudo": "/usr/bin/sudo",
            }.get(n),
        ):
            candidates = mkvtools._install_candidates()
        labels = [label for label, _ in candidates]
        assert labels == ["apt"]
        cmd = candidates[0][1]
        assert cmd[:3] == ["/usr/bin/sudo", "apt-get", "install"]

    def test_windows_candidates(self):
        with patch("sys.platform", "win32"):
            candidates = mkvtools._install_candidates()
        assert candidates[0][0] == "winget"
        assert "MoritzBunkus.MKVToolNix" in candidates[0][1]

    def test_macos_candidates(self):
        with patch("sys.platform", "darwin"):
            candidates = mkvtools._install_candidates()
        assert candidates[0] == ("homebrew", ["brew", "install", "mkvtoolnix"])


class TestEnsureMkvtoolnix:
    def test_true_when_tool_exists(self):
        with patch("renamer.mkvtools.find_tool", return_value="/usr/bin/mkvmerge"):
            assert ensure_mkvtoolnix() is True

    def test_false_when_non_interactive_and_missing(self):
        with patch("renamer.mkvtools.find_tool", return_value=None):
            assert ensure_mkvtoolnix(interactive=False) is False

    def test_no_confirmation_means_no_subprocess(self):
        with (
            patch("renamer.mkvtools.find_tool", return_value=None),
            patch("renamer.mkvtools._install_candidates", return_value=[]),
            patch("renamer.mkvtools.subprocess.run") as run,
        ):
            assert ensure_mkvtoolnix(interactive=False) is False
            run.assert_not_called()

    def test_declined_confirmation_never_installs(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        with (
            patch("renamer.mkvtools.find_tool", return_value=None),
            patch("renamer.mkvtools._install_candidates") as cands,
            patch("renamer.mkvtools.subprocess.run") as run,
        ):
            cands.return_value = [("apt", ["sudo", "apt-get", "install", "-y", "mkvtoolnix"])]
            assert ensure_mkvtoolnix() is False
            run.assert_not_called()

    def test_sudo_password_required_prints_command_only(self, monkeypatch, capsys):
        """Without passwordless sudo we must not run the installer."""
        monkeypatch.setattr("builtins.input", lambda *_: "y")
        fail = MagicMock(returncode=1)
        with (
            patch("renamer.mkvtools.find_tool", side_effect=[None, None]),
            patch("renamer.mkvtools._install_candidates") as cands,
            patch("renamer.mkvtools.subprocess.run", return_value=fail) as run,
        ):
            cands.return_value = [("apt", ["sudo", "apt-get", "install", "-y", "mkvtoolnix"])]
            assert ensure_mkvtoolnix() is False
        # only the `sudo -n true` probe ran — never the install command
        assert all(call.args[0][1:] != ["apt-get", "install"] for call in run.call_args_list)
        out = capsys.readouterr().out
        assert "sudo apt-get install" in out or "apt-get install" in out


# ---------------------------------------------------------------------------
# Language handling
# ---------------------------------------------------------------------------


class TestLanguageFromTag:
    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("en", "en"),
            (".eng", "eng"),
            (".pt-BR", "pt-br"),
            ("jpn", "jpn"),
        ],
    )
    def test_valid_tags_pass_through(self, tag: str, expected: str):
        assert language_from_tag(tag, fallback="und") == expected

    @pytest.mark.parametrize("tag", [None, "", ".xyz12", ".notalang"])
    def test_invalid_tags_fall_back(self, tag: str | None):
        assert language_from_tag(tag, fallback="fre") == "fre"


# ---------------------------------------------------------------------------
# Merge command construction
# ---------------------------------------------------------------------------


class TestBuildMergeCmd:
    def test_full_command(self, tmp_path: Path):
        video = tmp_path / "show.mkv"
        sub = tmp_path / "show.ass"
        out = tmp_path / "show.merging.mkv"
        cmd = build_merge_cmd(
            video,
            sub,
            output=out,
            language="eng",
            delay_ms=-500,
            track_name="Full",
            mkvmerge="/opt/mkvmerge",
        )
        assert cmd == [
            "/opt/mkvmerge",
            "--output",
            str(out),
            str(video),
            # per-track options must sit directly before the SUBTITLE input
            "--language",
            "0:eng",
            "--default-track-flag",
            "0:yes",
            "--forced-display-flag",
            "0:no",
            "--track-name",
            "0:Full",
            "--sync",
            "0:-500",
            "--compression",
            "0:none",
            str(sub),
        ]

    def test_subtitle_options_bind_to_subtitle_not_video(self, tmp_path: Path):
        """Regression: options before the video applied to the wrong track."""
        video = tmp_path / "a.mkv"
        sub = tmp_path / "a.srt"
        cmd = build_merge_cmd(video, sub, output=tmp_path / "o.mkv", language="ara")
        # The whole per-track options block must sit AFTER the video input
        # and BEFORE the subtitle input (the file they bind to).
        assert cmd.index("--language") > cmd.index(str(video))
        assert cmd[-1] == str(sub)

    def test_no_sync_flag_without_delay(self, tmp_path: Path):
        cmd = build_merge_cmd(
            tmp_path / "a.mkv",
            tmp_path / "a.srt",
            output=tmp_path / "out.mkv",
            language="ara",
        )
        assert "--sync" not in cmd
        assert "--track-name" not in cmd


# ---------------------------------------------------------------------------
# merge_subtitle behaviour
# ---------------------------------------------------------------------------


def _run_proc(returncode: int) -> MagicMock:
    p = MagicMock(returncode=returncode)
    p.stderr = ""
    p.stdout = ""
    return p


def _fake_mkvmerge(
    merge_rc: int = 0,
    sub_tracks: list[tuple[str, bool]] | None = None,
    payload: bytes = b"merged" * 200,
):
    """
    Build a subprocess.run stand-in that answers both call shapes:
    ``mkvmerge -J <video>`` probes (JSON on stdout) and real merges
    (writes the temp output). *sub_tracks* is a list of
    (language, default_flag) tuples the probed video already contains.
    """
    import json as _json

    tracks = [
        {"type": "subtitles",
         "properties": {"language": lang, "default_track": default}}
        for lang, default in sub_tracks or []
    ]

    def run(cmd, **kwargs):
        if "-J" in cmd:
            p = _run_proc(0)
            p.stdout = _json.dumps({"tracks": tracks})
            return p
        Path(cmd[2]).write_bytes(payload)
        return _run_proc(merge_rc)

    return run


@pytest.fixture
def video_and_sub(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "show - S01E01.mkv"
    video.write_bytes(b"x" * 1000)
    sub = tmp_path / "show - S01E01.ass"
    sub.write_bytes(b"[Script Info]")
    return video, sub


class TestMergeSubtitle:
    def test_rejects_non_mkv_video(self, tmp_path: Path):
        mp4 = tmp_path / "a.mp4"
        mp4.write_bytes(b"x")
        result = merge_subtitle(mp4, tmp_path / "a.srt")
        assert result.ok is False
        assert "mkv" in result.error

    def test_dry_run_only_probes(self, video_and_sub):
        """Dry-run performs the read-only -J probe but no merge."""
        video, sub = video_and_sub
        calls: list[list[str]] = []
        with patch("renamer.mkvtools.subprocess.run", side_effect=lambda c, **k: (
            calls.append(list(c)) or _fake_mkvmerge()(c, **k)
        )):
            result = merge_subtitle(video, sub, dry_run=True)
            assert result.ok is True
            assert len(calls) == 1
            assert "-J" in calls[0]
        assert video.read_bytes() == b"x" * 1000  # original untouched

    def test_skips_video_already_having_language(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(sub_tracks=[("eng", True)])):
            result = merge_subtitle(video, sub, language="eng")
        assert result.ok is False
        assert "already contains" in result.error
        assert video.read_bytes() == b"x" * 1000  # nothing was written

    def test_force_overrides_duplicate_skip(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(sub_tracks=[("eng", True)])):
            result = merge_subtitle(video, sub, language="eng", force=True)
        assert result.ok is True

    def test_default_flag_suppressed_when_default_exists(self, video_and_sub):
        video, sub = video_and_sub
        captured: list[list[str]] = []

        def spy(cmd, **kwargs):
            if "-J" not in cmd:
                captured.append(list(cmd))
            return _fake_mkvmerge(sub_tracks=[("jpn", True), ("ara", False)])(
                cmd, **kwargs
            )

        with patch("renamer.mkvtools.subprocess.run", side_effect=spy):
            result = merge_subtitle(video, sub, language="ger", default_track=True)

        assert result.ok is True
        idx = captured[0].index("--default-track-flag")
        assert captured[0][idx + 1] == "0:no"

    def test_default_flag_kept_when_no_existing_defaults(self, video_and_sub):
        video, sub = video_and_sub
        captured: list[list[str]] = []

        def spy(cmd, **kwargs):
            if "-J" not in cmd:
                captured.append(list(cmd))
            return _fake_mkvmerge(sub_tracks=[("eng", False)])(cmd, **kwargs)

        with patch("renamer.mkvtools.subprocess.run", side_effect=spy):
            result = merge_subtitle(video, sub, language="ger", default_track=True)

        assert result.ok is True
        idx = captured[0].index("--default-track-flag")
        assert captured[0][idx + 1] == "0:yes"

    def test_probe_failure_falls_back_to_merge(self, video_and_sub):
        """When -J fails we lose the info but still merge (old behaviour)."""
        video, sub = video_and_sub

        def fail_probe(cmd, **kwargs):
            if "-J" in cmd:
                return _run_proc(2)  # probe error
            Path(cmd[2]).write_bytes(b"m" * 600)
            return _run_proc(0)

        with patch("renamer.mkvtools.subprocess.run", side_effect=fail_probe):
            result = merge_subtitle(video, sub, language="eng")
        assert result.ok is True

    def test_success_replaces_video_in_place(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(payload=b"merged" * 200)):
            result = merge_subtitle(video, sub)

        assert result.ok is True
        assert video.read_bytes() == b"merged" * 200  # content swapped in
        tmp = video.with_name(video.stem + ".merging.mkv")
        assert not tmp.exists()  # temp consumed by the swap

    def test_warning_exit_code_counts_as_success(self, video_and_sub):
        video, sub = video_and_sub

        def fake_run(cmd, **kwargs):
            if "-J" in cmd:
                return _fake_mkvmerge()(cmd, **kwargs)
            Path(cmd[2]).write_bytes(b"m" * 600)
            p = _run_proc(1)
            p.stderr = "Warning: non-default aspect ratio"
            return p

        with patch("renamer.mkvtools.subprocess.run", side_effect=fake_run):
            result = merge_subtitle(video, sub)
        assert result.ok is True

    def test_error_removes_temp_output(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(merge_rc=2)):
            result = merge_subtitle(video, sub)
        assert result.ok is False
        tmp = video.with_name(video.stem + ".merging.mkv")
        assert not tmp.exists()
        assert video.read_bytes() == b"x" * 1000  # original intact

    def test_tiny_output_refuses_replacement(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(payload=b"tiny")):
            result = merge_subtitle(video, sub)
        assert result.ok is False
        assert "small" in result.error

    def test_keep_backup_preserves_original(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(payload=b"new-content" * 100)):
            result = merge_subtitle(video, sub, keep_backup=True)

        assert result.ok is True
        backup = video.with_name(video.name + ".bak")
        assert backup.read_bytes() == b"x" * 1000
        assert video.read_bytes() == b"new-content" * 100


# ---------------------------------------------------------------------------
# Batch merging
# ---------------------------------------------------------------------------


class TestMergeSubtitlesBatch:
    def test_batch_skips_non_mkv_videos(self, tmp_path: Path):
        mp4 = tmp_path / "a.mp4"
        mp4.write_bytes(b"x")
        sub = tmp_path / "a.srt"
        sub.write_bytes(b"s")
        results = merge_subtitles_batch([(mp4, sub, None)], dry_run=True)
        assert len(results) == 1
        assert results[0].ok is False

    def test_batch_uses_filename_language_tag(self, video_and_sub):
        video, sub = video_and_sub
        captured: list[list[str]] = []
        fake = _fake_mkvmerge(payload=b"z" * 500)

        def spy(cmd, **kwargs):
            if "-J" not in cmd:
                captured.append(list(cmd))
            return fake(cmd, **kwargs)

        with patch("renamer.mkvtools.subprocess.run", side_effect=spy):
            results = merge_subtitles_batch([(video, sub, ".pt-BR")])

        assert results[0].ok is True
        idx = captured[0].index("--language")
        assert captured[0][idx + 1] == "0:pt-br"

    def test_batch_skips_already_merged_language(self, video_and_sub):
        """Idempotency: re-running a batch on merged files does nothing."""
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(sub_tracks=[("pt-br", True)])):
            results = merge_subtitles_batch([(video, sub, ".pt-BR")])
        assert results[0].ok is False
        assert "already contains" in results[0].error

    def test_batch_force_overrides_skip(self, video_and_sub):
        video, sub = video_and_sub
        with patch("renamer.mkvtools.subprocess.run",
                   side_effect=_fake_mkvmerge(sub_tracks=[("pt-br", True)])):
            results = merge_subtitles_batch(
                [(video, sub, ".pt-BR")], force=True
            )
        assert results[0].ok is True

    def test_batch_falls_back_to_default_language(self, video_and_sub):
        video, sub = video_and_sub
        captured: list[list[str]] = []
        fake = _fake_mkvmerge(payload=b"z" * 500)

        def spy(cmd, **kwargs):
            if "-J" not in cmd:
                captured.append(list(cmd))
            return fake(cmd, **kwargs)

        with patch("renamer.mkvtools.subprocess.run", side_effect=spy):
            merge_subtitles_batch([(video, sub, None)], language="ger")

        idx = captured[0].index("--language")
        assert captured[0][idx + 1] == "0:ger"


# ---------------------------------------------------------------------------
# Cover stripping
# ---------------------------------------------------------------------------


class TestStripCovers:
    def test_exact_argv_per_file(self, tmp_path: Path):
        a = tmp_path / "a.mkv"
        b = tmp_path / "b.mkv"
        for f in (a, b):
            f.write_bytes(b"x")

        cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            cmds.append(list(cmd))
            return _run_proc(0)

        with patch("renamer.mkvtools.subprocess.run", side_effect=fake_run):
            results = strip_covers(tmp_path)

        assert len(results) == 2
        expected_tail = [
            "--delete-attachment",
            "mime-type:image/jpeg",
            "--delete-attachment",
            "mime-type:image/png",
        ]
        assert cmds[0][-4:] == expected_tail
        assert cmds[0][0] == "mkvpropedit"

    def test_recursive_vs_flat_glob(self, tmp_path: Path):
        flat = tmp_path / "a.mkv"
        nested_dir = tmp_path / "sub"
        nested_dir.mkdir()
        nested = nested_dir / "b.mkv"
        flat.write_bytes(b"x")
        nested.write_bytes(b"x")

        with patch("renamer.mkvtools.subprocess.run", return_value=_run_proc(0)):
            flat_results = strip_covers(tmp_path)
            deep_results = strip_covers(tmp_path, recursive=True)

        assert [r.video for r in flat_results] == [flat]
        assert {r.video for r in deep_results} == {flat, nested}

    def test_warning_exit_code_counts_as_success(self, tmp_path: Path):
        """rc 1 = warnings only (no attachment of one mime type) — success."""
        f = tmp_path / "a.mkv"
        f.write_bytes(b"x")
        p = _run_proc(1)
        p.stdout = "Warning: No attachment matched the spec 'mime-type:image/png'."
        with patch("renamer.mkvtools.subprocess.run", return_value=p):
            results = strip_covers(tmp_path)
        assert results[0].ok is True

    def test_failure_reported_with_error(self, tmp_path: Path):
        f = tmp_path / "broken.mkv"
        f.write_bytes(b"x")
        p = _run_proc(2)
        p.stderr = "Error: file not openable"
        with patch("renamer.mkvtools.subprocess.run", return_value=p):
            results = strip_covers(tmp_path)
        assert results[0].ok is False
        assert "Error" in results[0].error

    def test_dry_run_invokes_nothing(self, tmp_path: Path):
        f = tmp_path / "a.mkv"
        f.write_bytes(b"x")
        with patch("renamer.mkvtools.subprocess.run") as run:
            results = strip_covers(tmp_path, dry_run=True)
            run.assert_not_called()
        assert len(results) == 1
        assert results[0].ok is True
