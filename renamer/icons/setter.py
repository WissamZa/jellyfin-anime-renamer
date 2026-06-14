"""
renamer.icons.setter
====================
Download a poster image and write the ``.directory`` file that makes
Dolphin / Nautilus / Thunar display it as the folder icon.

Linux file-manager compatibility
--------------------------------
* **Dolphin (KDE)** — reads ``.directory`` in every folder.
* **Nautilus (GNOME)** — reads ``.directory`` for custom folder icons.
* **Thunar (XFCE)** — reads ``.directory`` for custom folder icons.
* **PCManFM / others** — most freedesktop.org-compliant managers support it.

The ``.directory`` file format::

    [Desktop Entry]
    Type=Directory
    Icon=/absolute/path/to/anime/folder/.folder_icon.png

The poster image is saved as ``.folder_icon.png`` (hidden file) inside
the anime folder so it travels with the folder and stays self-contained.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from typing import TYPE_CHECKING

import requests

from renamer.config import Config, get_logger
from renamer.icons.fetcher import fetch_poster

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)

# Filename used for the saved poster inside the anime folder
_ICON_FILENAME = ".folder_icon.png"
# Filename for the .directory Desktop Entry
_DIRECTORY_FILENAME = ".directory"


def set_folder_icon(
    folder: Path,
    cfg: Config,
    poster_url: str | None = None,
) -> bool:
    """
    Set a custom folder icon for *folder* by downloading a poster image
    and writing a ``.directory`` file.

    Parameters
    ----------
    folder:
        The anime series folder to set the icon on.
    cfg:
        Configuration (used to look up the poster URL if *poster_url*
        is not provided).
    poster_url:
        An explicit poster URL to download.  If ``None``, the poster
        URL is resolved automatically from the configured provider(s).

    Returns
    -------
    bool
        ``True`` if the icon was set successfully, ``False`` otherwise.
    """
    if not folder.is_dir():
        log.error("Icon target is not a directory: %s", folder)
        return False

    # Resolve poster URL
    if not poster_url:
        result = fetch_poster(cfg)
        if not result:
            log.warning("No poster URL found for '%s' — icon not set.", cfg.series_name)
            return False
        poster_url = result.url

    # Download the image
    icon_path = folder / _ICON_FILENAME
    if not _download_image(poster_url, icon_path):
        return False

    # Write the .directory file
    directory_path = folder / _DIRECTORY_FILENAME
    return _write_directory_file(directory_path, icon_path)


def set_folder_icon_batch(
    folders: list[tuple[Path, Config]],
) -> tuple[int, int]:
    """
    Set folder icons for a batch of (folder, config) pairs.

    Returns (success_count, failure_count).
    """
    success = 0
    failure = 0
    for folder, cfg in folders:
        try:
            if set_folder_icon(folder, cfg):
                success += 1
            else:
                failure += 1
        except Exception as exc:
            log.error("Icon setting failed for '%s': %s", folder.name, exc)
            failure += 1
    return success, failure


def remove_folder_icon(folder: Path) -> bool:
    """
    Remove the custom folder icon from *folder* by deleting the
    ``.directory`` and ``.folder_icon.png`` files.

    Returns ``True`` if any files were removed.
    """
    removed = False
    for name in (_DIRECTORY_FILENAME, _ICON_FILENAME):
        path = folder / name
        try:
            if path.exists():
                path.unlink()
                log.info("Removed: %s", path)
                removed = True
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)
    return removed


def has_folder_icon(folder: Path) -> bool:
    """Check whether *folder* already has a custom icon set."""
    return (folder / _DIRECTORY_FILENAME).exists() and (folder / _ICON_FILENAME).exists()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _download_image(url: str, dest: Path) -> bool:
    """
    Download an image from *url* and save it to *dest*.

    Writes to a temporary file first and then atomically renames it
    to avoid leaving a corrupt file on disk if the download is
    interrupted.
    """
    try:
        log.info("Downloading poster: %s", url)
        r = requests.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            log.warning("Poster download failed — HTTP %s for %s", r.status_code, url)
            return False

        # Write to a temp file first, then rename atomically
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".png",
            dir=str(dest.parent),
            prefix=".icon_tmp_",
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            os.rename(tmp_path, str(dest))
        except Exception:
            # Clean up temp file on error
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        log.info("Saved poster to: %s", dest)
        return True

    except requests.exceptions.RequestException as exc:
        log.error("Poster download error: %s", exc)
        return False


def _write_directory_file(directory_path: Path, icon_path: Path) -> bool:
    """
    Write a freedesktop.org ``.directory`` file that points to *icon_path*.

    The icon path is stored as an absolute path so it works regardless
    of the file manager's current working directory.
    """
    # Resolve to absolute path for maximum compatibility
    abs_icon = icon_path.resolve()

    content = f"[Desktop Entry]\nType=Directory\nIcon={abs_icon}\n"

    try:
        directory_path.write_text(content, encoding="utf-8")
        log.info("Wrote .directory: %s -> %s", directory_path, abs_icon)
        return True
    except OSError as exc:
        log.error("Could not write .directory file: %s", exc)
        return False
