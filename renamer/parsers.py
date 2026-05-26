"""
parsers — EpisodeNumberParser and SpecialParser.
"""

import json
import re
from pathlib import Path
from typing import Optional

from renamer.config import get_logger

log = get_logger()


class SpecialParser:
    """
    Detects special-episode patterns in a filename.

    Built-in patterns cover the most common conventions (SP, OVA, OAD,
    Special, Pilot, NCOP, NCED, PV, CM, etc.).

    Returns from parse():
      - None  : not a special
      - 0     : special but number unknown (auto-assign later)
      - int>0 : specific special number
    """

    # Patterns with a capture group -> the number is the special episode index.
    NUMERIC_PATTERNS: list[str] = [
        r"[Ss][Pp]\s*(\d+)",
        r"\bOVA\s*(\d+)\b",
        r"\bOAD\s*(\d+)\b",
        r"\b[Ss]pecial\s*(\d+)\b",
        r"\bOpening\s*(\d+)\b",
        r"\bEnding\s*(\d+)\b",
    ]

    # Named specials that have NO episode number -> return 0.
    UNNUMBERED_KEYWORDS: list[str] = [
        "OVA", "OAD", "Special", "Pilot",
        "NCOP", "NCED", "PV", "CM",
        "Preview", "Trailer", "Fan Letter",
    ]

    _NAMED_SPECIALS_FILE = (
        Path(__file__).resolve().parent.parent / "title_case_particles.json"
    )

    @classmethod
    def _load_named_specials(cls) -> list[str]:
        """Load named-special keywords from title_case_particles.json."""
        try:
            data = json.loads(
                cls._NAMED_SPECIALS_FILE.read_text(encoding="utf-8")
            )
            named = data.get("named_specials", [])
            if isinstance(named, list):
                return [
                    str(s).strip()
                    for s in named
                    if isinstance(s, str) and s.strip()
                ]
        except (OSError, json.JSONDecodeError) as exc:
            log.debug(
                "Could not load named_specials (%s) — using fallback.", exc
            )
        return ["Fan Letter", "Pilot"]

    @classmethod
    def parse(
        cls,
        filename: str,
        extra_named_specials: Optional[list[str]] = None,
    ) -> Optional[int]:
        """
        Parse filename for special-episode indicators.

        Returns:
          None — not a special
          0    — special with unknown number
          int  — specific special number
        """
        stem = Path(filename).stem

        # 1. Numeric patterns (SP42, OVA1, Special 3, OAD 2, Opening 1, Ending 1 …)
        for pattern in cls.NUMERIC_PATTERNS:
            m = re.search(pattern, stem, re.IGNORECASE)
            if m:
                return int(m.group(1)) if m.lastindex else 0

        # 2. Unnumbered keywords (OVA, OAD, Special, Pilot, NCOP, NCED, PV, CM, …)
        all_keywords = cls.UNNUMBERED_KEYWORDS + cls._load_named_specials()
        if extra_named_specials:
            all_keywords = all_keywords + extra_named_specials

        for keyword in all_keywords:
            if re.search(re.escape(keyword), stem, re.IGNORECASE):
                return 0

        return None


class EpisodeNumberParser:
    PATTERNS: list[tuple[str, str]] = [
        ("SxxExx",           r"[Ss]\d+[Ee](\d{1,4})"),
        ("Explicit keyword", r"(?:ep|episode)[.\s_-]*(\d{1,4})\b"),
        ("Brackets [NNN]",   r"\[(\d{2,4})\]"),
        ("Dash-space NNN",   r"[-–]\s*(\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
        ("Trailing number",  r"[\s._](\d{2,4})(?:v\d+)?(?:\s|$|\.)"),
    ]

    # Expanded patterns to match explicit Season and Episode together
    SEASON_EP_PATTERNS = [
        # SxxExx or SxxEPxx or Sxx Ep xx
        re.compile(r"[Ss](\d{1,2})\s*[Ee][Pp]?\s*(\d{1,4})", re.IGNORECASE),
        # Sxx - xx or Season xx - xx or Sxx_xx
        re.compile(
            r"\b(?:Season|S)(\d{1,2})\s*[-–_]\s*(\d{1,4})\b",
            re.IGNORECASE,
        ),
        # xxExx or xx_xx like 2x08
        re.compile(r"\b(\d{1,2})x(\d{1,4})\b", re.IGNORECASE),
    ]

    @classmethod
    def parse_season_episode(
        cls, filename: str
    ) -> tuple[Optional[int], Optional[int]]:
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
