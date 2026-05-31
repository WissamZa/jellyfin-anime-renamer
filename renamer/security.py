"""
renamer.security
================
Security utilities for the Jellyfin Anime Renamer.

Checks and hardening:
  * File permission warnings for sensitive files (.env, cache DB)
  * API key masking in logs and display
  * Path traversal validation
  * Input sanitization for API queries
  * Auto-fix permissions on sensitive files
  * Log redaction for credentials
  * Database file permission hardening
"""

from __future__ import annotations

import os
import re
import stat
from typing import TYPE_CHECKING

from renamer.config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)


def check_file_permissions(path: Path, description: str = "") -> list[str]:
    """
    Check if a file has overly-permissive access and return warnings.

    Warns if the file is world-readable or world-writable on Unix systems.
    Returns a list of warning messages (empty if no issues).
    """
    warnings: list[str] = []
    if not path.exists():
        return warnings

    try:
        st = path.stat()
        mode = st.st_mode

        if mode & stat.S_IROTH:
            msg = f"{description or path.name} is world-readable — recommend chmod 600"
            warnings.append(msg)
            log.warning("Security: %s", msg)

        if mode & stat.S_IWOTH:
            msg = f"{description or path.name} is world-writable — recommend chmod 600"
            warnings.append(msg)
            log.warning("Security: %s", msg)

    except OSError as exc:
        log.debug("Could not check permissions for %s: %s", path, exc)

    return warnings


def check_sensitive_files(base_dir: Path) -> list[str]:
    """
    Check permissions on all sensitive files in the project directory.

    Returns a list of warning messages.
    """
    warnings: list[str] = []
    sensitive_files = [
        (base_dir / ".env", "Environment file (.env)"),
        (base_dir / "anidb_cache.db", "AniDB cache database"),
        (base_dir / "anime_library.db", "Library database"),
        (base_dir / "rename_history.json", "Rename history"),
    ]

    for path, desc in sensitive_files:
        warnings.extend(check_file_permissions(path, desc))

    return warnings


def mask_api_key(key: str, visible_chars: int = 4) -> str:
    """
    Mask an API key or password for safe display in logs.

    Example:
        mask_api_key("abcd1234efgh5678") → "abcd************"
    """
    if not key or len(key) <= visible_chars:
        return "***" if key else "(empty)"
    return key[:visible_chars] + "*" * (len(key) - visible_chars)


def validate_path_containment(path: Path, base: Path) -> bool:
    """
    Verify that *path* is contained within *base* directory.

    Prevents path traversal attacks (CWE-22). Resolves symlinks
    and normalizes the path before comparison.

    Returns True if path is safely within base, False otherwise.
    """
    try:
        resolved = path.resolve()
        base_resolved = base.resolve()
        resolved.relative_to(base_resolved)
        return True
    except ValueError:
        return False


def sanitize_search_query(query: str) -> str:
    """
    Sanitize a user-provided search query before sending to an API.

    Removes potentially dangerous characters while preserving
    valid search terms.
    """
    # Remove control characters
    sanitized = re.sub(r"[\x00-\x1f\x7f]", "", query)
    # Limit length
    sanitized = sanitized[:200]
    # Strip leading/trailing whitespace
    sanitized = sanitized.strip()
    return sanitized


def fix_file_permissions(path: Path, mode: int = 0o600) -> bool:
    """
    Attempt to fix file permissions to be owner-only readable.

    Returns True if successful, False otherwise.
    """
    try:
        os.chmod(path, mode)
        log.info("Fixed permissions on %s to %o", path, mode)
        return True
    except OSError as exc:
        log.warning("Could not fix permissions on %s: %s", path, exc)
        return False


def auto_fix_sensitive_permissions(base_dir: Path) -> int:
    """
    Automatically fix permissions on all sensitive files to owner-only (0o600).

    Returns the number of files that were fixed.
    """
    sensitive_files = [
        base_dir / ".env",
        base_dir / "anidb_cache.db",
        base_dir / "anime_library.db",
        base_dir / "rename_history.json",
    ]

    fixed = 0
    for path in sensitive_files:
        if not path.exists():
            continue

        try:
            st = path.stat()
            mode = st.st_mode
            # Check if file is readable/writable by group or others
            if ((mode & stat.S_IRGRP) or (mode & stat.S_IROTH) or \
               (mode & stat.S_IWGRP) or (mode & stat.S_IWOTH)) and fix_file_permissions(path):
                fixed += 1
        except OSError:
            pass

    if fixed > 0:
        log.info("Auto-fixed permissions on %d sensitive file(s)", fixed)

    return fixed


def redact_log_message(message: str) -> str:
    """
    Redact sensitive data from log messages.

    Masks:
      - AniDB session keys: s=ABCDEF1234 → s=****1234
      - Passwords in AUTH: password=xxx → password=****
      - API keys: api_key=xxx → api_key=****
    """
    # Redact AniDB session key
    message = re.sub(
        r's=[A-Za-z0-9]{4,}',
        lambda m: f's=****{m.group()[-4:]}',
        message,
    )
    # Redact password in AUTH
    message = re.sub(
        r'password=[^\s&]+',
        'password=****',
        message,
    )
    # Redact API keys
    message = re.sub(
        r'api_key=[^\s&]+',
        'api_key=****',
        message,
    )
    # Redact pass= parameters
    message = re.sub(
        r'pass=[^\s&]+',
        'pass=****',
        message,
    )
    return message


def sanitize_filename(name: str) -> str:
    """
    Sanitize a filename to prevent directory traversal and illegal characters.

    Strips:
      - Control characters
      - Path separators
      - DOS reserved names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
      - Leading/trailing dots and spaces
      - Limits length to 200 characters
    """
    # Remove null bytes and control characters
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)

    # Remove path separators
    name = name.replace('/', '').replace('\\', '')

    # Remove leading/trailing dots and spaces
    name = name.strip('. ')

    # Check for DOS reserved names
    dos_reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
    }
    stem = name.rsplit('.', 1)[0].upper() if '.' in name else name.upper()
    if stem in dos_reserved:
        name = f'_{name}'

    # Block path traversal attempts
    if '..' in name:
        name = name.replace('..', '')

    # Limit length
    if len(name) > 200:
        # Preserve extension if present
        if '.' in name:
            base, ext = name.rsplit('.', 1)
            name = f'{base[:200 - len(ext) - 1]}.{ext}'
        else:
            name = name[:200]

    return name or '_'
