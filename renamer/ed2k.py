"""
renamer.ed2k
============
ED2K hash computation for AniDB file identification.

The ED2K hash is the standard hash used by AniDB's FILE command to
uniquely identify a file.  The algorithm works as follows:

1. Split the file into 9,728,000-byte chunks (the eDonkey standard).
2. Compute MD4 of each chunk.
3. If there is only one chunk, the ED2K hash = that chunk's MD4.
4. If there are multiple chunks, the ED2K hash = MD4(concat of all chunk MD4s).

Performance:
  * Files < 50 MB: sequential f.read() per chunk.
  * Files >= 50 MB: mmap + memoryview slicing for zero-copy hashing.

Requires: pycryptodome (for Crypto.Hash.MD4)
"""

from __future__ import annotations

import mmap
from pathlib import Path
from typing import TYPE_CHECKING

from renamer.config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# eDonkey protocol chunk size (9.28 MB)
ED2K_CHUNK_SIZE = 9_728_000

# Threshold above which mmap is used instead of f.read()
_MMAP_THRESHOLD = 50 * 1024 * 1024  # 50 MB


def _md4_hash(data: bytes) -> bytes:
    """Compute raw MD4 digest of *data*."""
    try:
        from Crypto.Hash.MD4 import MD4Hash
    except ImportError:
        # Fallback: try hashlib if available (Python 3.9+ removed md4)
        import hashlib
        try:
            return hashlib.new("md4", data).digest()
        except ValueError as exc:
            raise ImportError(
                "MD4 hash not available. Install pycryptodome: uv add pycryptodome"
            ) from exc
    h = MD4Hash()
    h.update(data)
    return h.digest()


def compute_ed2k(
    path: str | Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str:
    """
    Compute the ED2K hash of a file.

    Parameters
    ----------
    path:
        Path to the file to hash.
    progress_callback:
        Optional callback ``callback(bytes_done, total_bytes)`` called
        after each chunk is processed.  Useful for progress bars.

    Returns
    -------
    str
        Uppercase hex string of the ED2K hash (32 characters).

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ImportError
        If pycryptodome is not installed (MD4 is required).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    total_size = path.stat().st_size
    if total_size == 0:
        # Empty file: MD4 of empty bytes
        return _md4_hash(b"").hex().upper()

    use_mmap = total_size >= _MMAP_THRESHOLD
    hashes: list[bytes] = []

    if use_mmap:
        log.debug("ED2K: using mmap for large file (%d bytes): %s", total_size, path.name)
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            offset = 0
            while offset < total_size:
                end = min(offset + ED2K_CHUNK_SIZE, total_size)
                chunk = mm[offset:end]
                hashes.append(_md4_hash(chunk))
                offset = end
                if progress_callback:
                    progress_callback(offset, total_size)
    else:
        log.debug("ED2K: using read() for file (%d bytes): %s", total_size, path.name)
        with open(path, "rb") as f:
            bytes_done = 0
            while True:
                chunk = f.read(ED2K_CHUNK_SIZE)
                if not chunk:
                    break
                hashes.append(_md4_hash(chunk))
                bytes_done += len(chunk)
                if progress_callback:
                    progress_callback(bytes_done, total_size)

    # Single-chunk file: hash = MD4(chunk)
    # Multi-chunk file: hash = MD4(concat of all chunk digests)
    if len(hashes) == 1:
        return hashes[0].hex().upper()

    combined = b"".join(hashes)
    return _md4_hash(combined).hex().upper()


def get_file_size(path: str | Path) -> int:
    """Return file size in bytes (fast stat, no read)."""
    return Path(path).stat().st_size
