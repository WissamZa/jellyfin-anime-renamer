"""
renamer.mkvtools
================
mkvtoolnix integration: subtitle merging, guided installation, and
cover-image removal.

All commands are built with explicit argv lists and executed via
``subprocess.run`` (no shell), so behaviour is identical on Windows,
macOS, and Linux.

Subtitle merges rewrite the video **in place**: mkvmerge writes to a
temporary ``<video>.merging.mkv`` in the same directory and the original
is atomically replaced only after a successful merge.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from renamer.config import get_logger

log = get_logger(__name__)

# ISO-639-ish language codes accepted from filename tags (".en", ".pt-BR")
_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")

# Image mime types treated as cover art (fonts etc. are never touched)
DEFAULT_IMAGE_MIMES = ("image/jpeg", "image/png")


@dataclass
class MergeResult:
    """Outcome of a single video+subtitle merge."""

    video: Path
    subtitle: Path | None
    ok: bool
    error: str = ""
    backup: Path | None = None


@dataclass
class StripResult:
    """Outcome of one cover-strip attempt on an MKV."""

    video: Path
    ok: bool
    removed: int = 0  # conservative: 1 if command succeeded, 0 otherwise
    error: str = ""


# ---------------------------------------------------------------------------
# Tool discovery & guided installation
# ---------------------------------------------------------------------------


def find_tool(name: str) -> str | None:
    """Return the absolute path of *name* on PATH, or None."""
    return shutil.which(name)


def _install_candidates() -> list[tuple[str, list[str]]]:
    """
    Return ordered ``(package_manager_label, install_command)`` candidates
    for the current platform. Only candidates whose manager binary exists
    (checked by the caller) should be offered.
    """
    if sys.platform == "win32":
        return [
            ("winget", [
                "winget", "install", "--id", "MoritzBunkus.MKVToolNix", "-e",
                "--accept-source-agreements", "--accept-package-agreements",
            ]),
            ("chocolatey", ["choco", "install", "mkvtoolnix", "-y"]),
            ("scoop", ["scoop", "install", "mkvtoolnix"]),
        ]
    if sys.platform == "darwin":
        return [("homebrew", ["brew", "install", "mkvtoolnix"])]

    # Linux / BSD family — distro package managers, all via sudo
    managers = [
        ("apt", ["apt-get", "install", "-y", "mkvtoolnix"]),
        ("dnf", ["dnf", "install", "-y", "mkvtoolnix"]),
        ("pacman", ["pacman", "-S", "--noconfirm", "--needed", "mkvtoolnix"]),
        ("zypper", ["zypper", "install", "-y", "mkvtoolnix"]),
        ("apk", ["apk", "add", "mkvtoolnix"]),
        ("eopkg", ["eopkg", "it", "mkvtoolnix"]),
    ]
    candidates: list[tuple[str, list[str]]] = []
    for label, cmd in managers:
        if shutil.which(cmd[0]):
            sudo = shutil.which("sudo")
            prefix = [sudo] if sudo else []
            candidates.append((label, prefix + cmd))
    return candidates


def ensure_mkvtoolnix(tool: str = "mkvmerge", interactive: bool = True) -> bool:
    """
    Ensure *tool* (mkvmerge/mkvpropedit) is available, offering to install
    mkvtoolnix when it is missing.

    Never installs without explicit user confirmation. When passwordless
    sudo is unavailable on Linux, prints the command for the user's own
    terminal instead of prompting for a password from Python.

    Returns True when the tool is (now) available.
    """
    path = find_tool(tool)
    if path:
        log.debug("mkvtoolnix tool found: %s → %s", tool, path)
        return True

    print(f"\n  '{tool}' is not installed (part of the MKVToolNix suite).")

    candidates = [
        (label, cmd) for label, cmd in _install_candidates() if shutil.which(cmd[0])
    ]

    if not interactive or not candidates:
        for label, cmd in candidates or []:
            print(f"  Install manually ({label}): {' '.join(cmd)}")
        if not candidates:
            print("  No supported package manager detected — install MKVToolNix from")
            print("  https://mkvtoolnix.download/downloads.html")
        return False

    label, cmd = candidates[0]
    print(f"  Detected package manager: {label}")
    print(f"  Command: {' '.join(cmd)}")
    try:
        answer = input(f"  Install MKVToolNix now using {label}? (y/N): ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("  Skipped installation.")
        return False

    # On Linux the command runs through sudo; make sure it won't hang on a
    # password prompt we can't drive from here.
    if sys.platform != "win32" and cmd[0].endswith("sudo"):
        try:
            sudo_ok = (
                subprocess.run(
                    ["sudo", "-n", "true"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            sudo_ok = False
        if not sudo_ok:
            print("\n  sudo needs your password — run this in your terminal instead:")
            print(f"    {' '.join(cmd)}")
            return False

    print(f"\n  Running: {' '.join(cmd)}\n")
    try:
        subprocess.run(cmd, check=False)
    except OSError as exc:
        print(f"  Failed to run installer: {exc}")
        return False

    path = find_tool(tool)
    if path:
        print(f"  ✓ {tool} installed: {path}")
        return True

    print(f"  {tool} still not found after installation.")
    if sys.platform == "win32":
        print("  NOTE: open a NEW terminal so PATH changes take effect, then retry.")
    return False


# ---------------------------------------------------------------------------
# Subtitle merging
# ---------------------------------------------------------------------------


def language_from_tag(tag: str | None, fallback: str) -> str:
    """
    Convert a filename language tag ('.en', '.eng', '.pt-BR') into an
    mkvmerge language code; anything unrecognised falls back to *fallback*.
    """
    if tag:
        code = tag.lstrip(".").strip().lower()
        if _LANG_TAG_RE.match(code):
            return code
    return fallback


def build_merge_cmd(
    video: Path,
    subtitle: Path,
    *,
    output: Path,
    language: str,
    default_track: bool = True,
    forced_track: bool = False,
    track_name: str | None = None,
    delay_ms: int = 0,
    mkvmerge: str = "mkvmerge",
) -> list[str]:
    """
    Build the mkvmerge argv for appending *subtitle* to *video*.

    IMPORTANT: mkvmerge binds per-track options (``--language`` etc.) to
    the input file that FOLLOWS them, so the subtitle options are placed
    immediately before the subtitle path — not before the video.
    """
    cmd: list[str] = [mkvmerge, "--output", str(output)]
    cmd.append(str(video))
    # ── options below bind to the subtitle input ──────────────
    cmd += ["--language", f"0:{language}"]
    cmd += ["--default-track-flag", f"0:{'yes' if default_track else 'no'}"]
    cmd += ["--forced-display-flag", f"0:{'yes' if forced_track else 'no'}"]
    if track_name:
        cmd += ["--track-name", f"0:{track_name}"]
    if delay_ms:
        # Positive delays subtitles later, negative advances them
        cmd += ["--sync", f"0:{delay_ms}"]
    cmd += ["--compression", "0:none"]
    cmd.append(str(subtitle))
    return cmd


def _probe_subtitles(video: Path, mkvmerge: str = "mkvmerge") -> list[dict] | None:
    """
    Inspect *video* with ``mkvmerge -J`` and return its subtitle tracks as
    ``[{"language": "eng", "default": True}, …]``.

    Returns None when the probe fails (tool missing, unreadable file) —
    callers should treat that as "no information" and proceed.
    """
    try:
        proc = subprocess.run(
            [mkvmerge, "-J", str(video)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            return None
        import json

        info = json.loads(proc.stdout)
        return [
            {
                "language": t.get("properties", {}).get("language"),
                "default": bool(t.get("properties", {}).get("default_track")),
            }
            for t in info.get("tracks", [])
            if t.get("type") == "subtitles"
        ]
    except (OSError, ValueError):
        return None


def merge_subtitle(
    video: Path,
    subtitle: Path,
    *,
    language: str = "und",
    default_track: bool = True,
    forced_track: bool = False,
    track_name: str | None = None,
    delay_ms: int = 0,
    keep_backup: bool = False,
    dry_run: bool = False,
    force: bool = False,
    mkvmerge: str = "mkvmerge",
) -> MergeResult:
    """
    Merge *subtitle* into *video* (must be .mkv), replacing it in place.

    The merge writes to ``<video>.merging.mkv`` in the same directory and
    the original is replaced only after a successful run (mkvmerge exit
    code 0 or 1 — exit 1 means warnings, not failure).

    Files that already contain a subtitle track in *language* are skipped
    unless *force* is set, and the new track is only flagged as default
    when the video has no other default subtitle track.
    """
    video = Path(video)
    subtitle = Path(subtitle)

    if video.suffix.lower() != ".mkv":
        return MergeResult(video=video, subtitle=subtitle, ok=False, error="not an .mkv file")

    # Inspect existing subtitle tracks (read-only probe; also used for
    # accurate dry-run previews)
    effective_default = default_track
    existing = _probe_subtitles(video, mkvmerge)
    if existing is not None:
        if not force and any(t["language"] == language for t in existing):
            return MergeResult(
                video=video,
                subtitle=subtitle,
                ok=False,
                error=f"skipped: already contains a '{language}' subtitle track",
            )
        if default_track and any(t["default"] for t in existing):
            log.info(
                "%s already has a default subtitle track — merged track will "
                "not be flagged as default",
                video.name,
            )
            effective_default = False

    tmp = video.with_name(video.stem + ".merging" + video.suffix)

    cmd = build_merge_cmd(
        video,
        subtitle,
        output=tmp,
        language=language,
        default_track=effective_default,
        forced_track=forced_track,
        track_name=track_name,
        delay_ms=delay_ms,
        mkvmerge=mkvmerge,
    )

    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(cmd))
        return MergeResult(video=video, subtitle=subtitle, ok=True)

    # Clean stale temp files from a previous crashed run
    if tmp.exists():
        tmp.unlink()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return MergeResult(video=video, subtitle=subtitle, ok=False, error=str(exc))

    if proc.returncode >= 2:
        if tmp.exists():
            tmp.unlink()
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return MergeResult(
            video=video,
            subtitle=subtitle,
            ok=False,
            error="; ".join(tail) or f"mkvmerge exited {proc.returncode}",
        )

    # Sanity checks before replacing the original
    try:
        if not tmp.exists() or tmp.stat().st_size < video.stat().st_size * 0.5:
            if tmp.exists():
                tmp.unlink()
            return MergeResult(
                video=video,
                subtitle=subtitle,
                ok=False,
                error="merged output suspiciously small — refusing to replace",
            )
    except OSError as exc:
        return MergeResult(video=video, subtitle=subtitle, ok=False, error=str(exc))

    if proc.returncode == 1:
        # Exit 1 = warnings (e.g. non-default aspect ratio) — acceptable
        log.warning("mkvmerge warnings while merging %s: %s", video.name,
                    (proc.stderr or "").strip()[:200])

    try:
        backup: Path | None = None
        if keep_backup:
            backup = video.with_name(video.name + ".bak")
            os.replace(video, backup)
        os.replace(tmp, video)
        return MergeResult(video=video, subtitle=subtitle, ok=True, backup=backup)
    except OSError as exc:
        if tmp.exists():
            tmp.unlink()
        return MergeResult(video=video, subtitle=subtitle, ok=False, error=str(exc))


def merge_subtitles_batch(
    pairs: list[tuple[Path, Path, str | None]],
    *,
    language: str = "und",
    delay_ms: int = 0,
    default_track: bool = True,
    keep_backup: bool = False,
    dry_run: bool = False,
    force: bool = False,
    per_pair_language: bool = True,
    mkvmerge: str = "mkvmerge",
) -> list[MergeResult]:
    """
    Merge many (video, subtitle, language_tag) triples.

    When *per_pair_language* is set, each pair's language comes from its
    filename tag (validated against ISO-639-ish patterns); invalid or
    missing tags use *language* instead.  Videos that already contain a
    subtitle track in the target language are skipped unless *force*.
    """
    results: list[MergeResult] = []
    for video, sub, tag in pairs:
        lang = language
        if per_pair_language:
            lang = language_from_tag(tag, fallback=language)
        results.append(
            merge_subtitle(
                video,
                sub,
                language=lang,
                delay_ms=delay_ms,
                default_track=default_track,
                keep_backup=keep_backup,
                dry_run=dry_run,
                force=force,
                mkvmerge=mkvmerge,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Cover removal
# ---------------------------------------------------------------------------


def strip_covers(
    directory: Path,
    *,
    recursive: bool = False,
    dry_run: bool = False,
    mime_types: tuple[str, ...] = DEFAULT_IMAGE_MIMES,
    mkvpropedit: str = "mkvpropedit",
) -> list[StripResult]:
    """
    Delete cover-art attachments from every *.mkv under *directory*.

    Equivalent to::

        mkvpropedit <file> --delete-attachment mime-type:image/jpeg \\
                          --delete-attachment mime-type:image/png

    mkvpropedit edits the header in place (fast, no remux). Exit code 1
    means warnings only (e.g. no attachment of one of the mime types
    existed) and the edit was still written — reported as success; only
    exit >= 2 is a real failure. Only image attachments are touched;
    fonts used for subtitle rendering survive.
    """
    directory = Path(directory)
    pattern = "**/*.mkv" if recursive else "*.mkv"
    files = sorted(directory.glob(pattern))

    results: list[StripResult] = []
    for f in files:
        cmd: list[str] = [mkvpropedit, str(f)]
        for mt in mime_types:
            cmd += ["--delete-attachment", f"mime-type:{mt}"]

        if dry_run:
            log.info("[dry-run] would run: %s", " ".join(cmd))
            results.append(StripResult(video=f, ok=True, removed=0))
            continue

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            results.append(StripResult(video=f, ok=False, error=str(exc)))
            continue

        if proc.returncode <= 1:
            # rc 0 = clean; rc 1 = warnings (e.g. "No attachment matched the
            # spec" when only one of the mime types is present) but the edit
            # was still written — treat as success
            if proc.returncode == 1:
                log.debug("mkvpropedit warnings for %s: %s", f.name,
                          (proc.stdout or "").strip()[:200])
            results.append(StripResult(video=f, ok=True, removed=1))
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-2:]
            results.append(
                StripResult(
                    video=f,
                    ok=False,
                    error="; ".join(tail) or f"mkvpropedit exited {proc.returncode}",
                )
            )
    return results
