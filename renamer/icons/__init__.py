"""
renamer.icons
=============
Folder-icon support for anime series folders.

Downloads poster/cover images from metadata providers and creates
``.directory`` files so that Dolphin, Nautilus, Thunar, and other
freedesktop.org-compliant file managers display the poster as the
folder icon.

Public API
----------
* :func:`set_folder_icon`    — set icon on a single folder
* :func:`set_folder_icon_batch` — set icons on many folders
* :func:`remove_folder_icon` — remove a folder icon
* :func:`has_folder_icon`    — check if a folder has an icon
* :func:`fetch_poster`       — resolve a poster URL from providers
* :class:`PosterResult`      — poster URL + metadata
"""

from renamer.icons.fetcher import PosterResult, fetch_poster
from renamer.icons.setter import (
    has_folder_icon,
    remove_folder_icon,
    set_folder_icon,
    set_folder_icon_batch,
)

__all__ = [
    "PosterResult",
    "fetch_poster",
    "has_folder_icon",
    "remove_folder_icon",
    "set_folder_icon",
    "set_folder_icon_batch",
]
