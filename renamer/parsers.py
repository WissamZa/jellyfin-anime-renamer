"""
parsers.py — Episode number and special episode parsers.
"""

import re
from pathlib import Path
from typing import Optional


class SpecialParser:
    """Detects special/OVA episodes from filenames."""

    # Patterns with a capture group (\d+) return the captured number.
    # Patterns WITHOUT a capture group return 0 (meaning "special, number unknown").
    PATTERNS: list[str] = [
        # ── Numbered specials ──────────────────────────────────
        r"[Ss][Pp]\s*(\d+)",
        r"\bOVA\s*(\d+)\b",
        r"\bOAD\s*(\d+)\b",
        r"\b[Ss]pecial\s*(\d+)\b",
        r"\b[Oo]pening\s*(\d+)\b",
        r"\b[Ee]nding\s*(\d+)\b",
        # ── Unnumbered specials (return 0 = "special exists, number TBD") ──
        r"\bOVA\b",
        r"\bOAD\b",
        r"\b[Ss]pecial\b",
        r"\bPilot\b",
        r"\bNCOP\b",                          # creditless OP
        r"\bNCED\b",                          # creditless ED
        r"\bPV\b",                            # promo video
        r"\bCM\b",                            # commercial
        r"\bPreview\b",
        r"\bTrailer\b",
        r"Fan\s+Letter",
    ]

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        """
        Return the special episode number, or 0 if it's a special
        but the exact number couldn't be determined from the filename.
        Returns None if the file doesn't look like a special at all.
        """
        stem = Path(filename).stem
        for pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                # Only try int conversion if the group is purely digits
                if m.lastindex and m.group(1).isdigit():
                    return int(m.group(1))
                return 0
        return None


class EpisodeNumberParser:
    """Extracts episode numbers from anime filenames."""

    PATTERNS: list[tuple[str, str]] = [
        ("SxxExx", r"[Ss]\d+[Ee](\d{1,4})"),
        ("Explicit keyword", r"(?:ep|episode)[.\s_-]*(\d{1,4})\b"),
        ("Brackets [NNN]", r"\[(\d{2,4})\]"),
        ("Dash-space NNN", r"[-–]\s*(\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
        ("Trailing number", r"[\s._](\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
    ]

    # Expanded patterns to match explicit Season and Episode together
    SEASON_EP_PATTERNS = [
        # SxxExx or SxxEPxx or Sxx Ep xx
        re.compile(r"[Ss](\d{1,2})\s*[Ee][Pp]?\s*(\d{1,4})", re.IGNORECASE),
        # Sxx - xx or Season xx - xx or Sxx_xx
        re.compile(r"\b(?:Season|S)(\d{1,2})\s*[-–_]\s*(\d{1,4})\b", re.IGNORECASE),
        # xxExx or xx_xx like 2x08
        re.compile(r"\b(\d{1,2})x(\d{1,4})\b", re.IGNORECASE),
        # Ordinal seasons - 1st Season, 2nd Season, 3rd Season, etc.
        re.compile(
            r"\b(\d{1,2})(?:st|nd|rd|th)\s+Season\s*[-–_]\s*(\d{1,4})\b",
            re.IGNORECASE,
        ),
    ]

    @classmethod
    def parse_season_episode(cls, filename: str) -> tuple[Optional[int], Optional[int]]:
        stem = Path(filename).stem

        for pattern in cls.SEASON_EP_PATTERNS:
            m = pattern.search(stem)
            if m:
                return int(m.group(1)), int(m.group(2))

        return None, None

    @classmethod
    def parse(cls, filename: str) -> Optional[int]:
        stem = Path(filename).stem
        for _label, pattern in cls.PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None
