"""
providers.romaji_resolver — RomajiResolver (AniList primary, TMDB romaji fallback).

No AniDB step — removed in v5.
"""

from typing import Optional

from renamer.config import get_logger
from renamer.romaniser import get_romaniser

log = get_logger()


class RomajiResolver:
    """
    Determines the best romaji series name by cross-referencing
    TMDB (Japanese title -> pykakasi) against AniList (native romaji).

    Priority:
      1. AniList romaji  — human-curated, most accurate
      2. pykakasi romaji — mechanical but handles all kanji
      3. TMDB English    — last resort fallback
    """

    def __init__(self) -> None:
        from renamer.providers.anilist import AniListFetcher
        self._anilist = AniListFetcher()

    def resolve(
        self,
        search_name: str,
        tmdb_romaji: str,
        english_name: str,
    ) -> str:
        # 1. Try AniList (human-curated, highest quality)
        anilist_romaji = self._anilist.find_romaji(search_name)

        if anilist_romaji:
            similarity = self._similarity(tmdb_romaji, anilist_romaji)
            log.info(
                "Romaji comparison — TMDB: '%s'  AniList: '%s'  similarity: %.0f%%",
                tmdb_romaji, anilist_romaji, similarity * 100,
            )
            if similarity >= 0.80:
                log.info(
                    "High similarity — using AniList romaji: '%s'",
                    anilist_romaji,
                )
            else:
                log.info(
                    "Low similarity (%.0f%%) — preferring AniList: '%s'  "
                    "(TMDB was: '%s')",
                    similarity * 100, anilist_romaji, tmdb_romaji,
                )
            return anilist_romaji

        log.info(
            "AniList returned nothing for '%s' — falling back to TMDB romaji.",
            search_name,
        )

        # 2. Fall back to TMDB romaji (pykakasi or English)
        if tmdb_romaji and tmdb_romaji != english_name:
            return tmdb_romaji

        # 3. Last resort: English name
        return english_name

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Simple character-level similarity ratio (no extra deps)."""
        a, b = a.lower().strip(), b.lower().strip()
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        longer = max(len(a), len(b))
        matches = sum(c1 == c2 for c1, c2 in zip(a, b))
        return matches / longer
