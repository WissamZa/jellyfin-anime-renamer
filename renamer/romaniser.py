"""
romaniser — Japanese-to-romaji conversion and smart anime title-casing.
"""

import json
import re
from pathlib import Path

from renamer.config import get_logger

log = get_logger()

# ─────────────────── SMART TITLE-CASE ──────────────────
PARTICLES_FILE = Path(__file__).resolve().parent.parent / "title_case_particles.json"


def _load_lowercase_words() -> set[str]:
    """
    Load the union of all word lists from title_case_particles.json.
    Returns a set of lowercase strings that should NOT be capitalised
    when they appear mid-title.
    """
    try:
        data = json.loads(PARTICLES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "Could not load %s (%s) — using built-in fallback.",
            PARTICLES_FILE.name, exc,
        )
        return {
            "no", "ni", "wa", "ga", "wo", "to", "de", "ka", "na", "mo",
            "ya", "e", "a", "an", "the", "at", "by", "for", "in", "of",
            "on", "or", "and",
        }
    words: set[str] = set()
    for key, lst in data.items():
        if key.startswith("_"):
            continue
        if isinstance(lst, list):
            words.update(
                w.lower().strip() for w in lst if isinstance(w, str)
            )
    return words


def anime_title_case(text: str) -> str:
    """
    Title-case a romanised anime title while keeping Japanese particles
    and short English prepositions in lowercase (matching AniList style).

    Rules:
      - First word is always capitalised.
      - Words in the particles JSON are kept lowercase unless they are first.
      - All other words are capitalised.

    Example:
        "honzuki no gekokujou shisho ni naru tame ni wa"
        -> "Honzuki no Gekokujou Shisho ni Naru Tame ni wa"
    """
    lowercase_words = _load_lowercase_words()
    words = text.split()
    result = []
    for i, word in enumerate(words):
        bare = word.lstrip("([{").rstrip(")]},.:;!?")
        if i == 0 or bare.lower() not in lowercase_words:
            result.append(word[0].upper() + word[1:] if word else word)
        else:
            result.append(word.lower())
    return " ".join(result)


# ══════════════════════ ROMANISER ════════════════════════
class Romaniser:
    """
    Converts Japanese text (hiragana, katakana, kanji) to Hepburn romaji.
    Uses pykakasi which handles all three scripts including kanji.

    If the input is already ASCII/Latin, it is returned unchanged so
    titles like "One Piece" are never mangled.
    """

    _JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

    def __init__(self) -> None:
        try:
            import pykakasi
            self._kks = pykakasi.kakasi()
            self._available = True
        except ImportError:
            log.warning(
                "pykakasi not installed — romaji conversion disabled. "
                "Run: uv add pykakasi"
            )
            self._kks = None  # type: ignore
            self._available = False

    def is_japanese(self, text: str) -> bool:
        return bool(self._JAPANESE.search(text))

    def to_romaji(self, text: str) -> str:
        """
        Romanise text. Returns original string if:
          - no Japanese characters detected
          - pykakasi is not installed
        """
        if not self._available or self._kks is None or not self.is_japanese(text):
            return text

        result = self._kks.convert(text)
        parts = []
        for item in result:
            hep = item["hepburn"].strip()
            orig = item["orig"]
            if hep:
                parts.append(hep)
            else:
                parts.append(orig)

        romanised = " ".join(p for p in parts if p.strip())
        romanised = re.sub(r" {2,}", " ", romanised)
        romanised = re.sub(r" ([!?.,\'\"])", r"\1", romanised)
        romanised = anime_title_case(romanised)
        log.debug("Romanised: '%s' -> '%s'", text, romanised)
        return romanised.strip()


# Singleton — shared across all fetchers
_romaniser = Romaniser()


def get_romaniser() -> Romaniser:
    """Return the shared Romaniser singleton."""
    return _romaniser
