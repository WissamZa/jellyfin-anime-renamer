"""
romaji_resolver.py — Cross-reference romaji titles across providers.

Determines the best romaji series name by checking:
  1. AniList romaji (human-curated, most accurate)
  2. Kitsu romaji (free API, good anime coverage)
  3. pykakasi romanisation of TMDB Japanese title (always available)
  4. TMDB English title (last resort)
"""

from renamer.config import log


class RomajiResolver:
    """
    Determines the best romaji series name by cross-referencing
    TMDB (Japanese title -> pykakasi) against AniList/Kitsu (native romaji).

    Priority:
      1. AniList romaji  — human-curated, most accurate
      2. Kitsu romaji    — free API, good anime coverage
      3. pykakasi romaji — mechanical but handles all kanji
      4. TMDB English    — last resort fallback
    """

    def __init__(self) -> None:
        # Lazy imports to avoid circular dependency at module level
        self._anilist = None
        self._kitsu = None

    def _get_anilist(self):
        if self._anilist is None:
            from renamer.providers.anilist import AniListFetcher
            self._anilist = AniListFetcher()
        return self._anilist

    def _get_kitsu(self):
        if self._kitsu is None:
            from renamer.providers.kitsu import KitsuFetcher
            self._kitsu = KitsuFetcher()
        return self._kitsu

    def resolve(
        self,
        search_name: str,
        tmdb_romaji: str,
        english_name: str,
    ) -> str:
        # ── 1. Try AniList (human-curated, highest quality) ──────────
        anilist_romaji = self._get_anilist().find_romaji(search_name)

        if anilist_romaji:
            similarity = self._similarity(tmdb_romaji, anilist_romaji)
            log.info(
                "Romaji comparison — TMDB: '%s'  AniList: '%s'  similarity: %.0f%%",
                tmdb_romaji, anilist_romaji, similarity * 100,
            )
            if similarity >= 0.80:
                log.info("High similarity — using AniList romaji: '%s'", anilist_romaji)
            else:
                log.info(
                    "Low similarity (%.0f%%) — preferring AniList: '%s'  (TMDB was: '%s')",
                    similarity * 100, anilist_romaji, tmdb_romaji,
                )
            return anilist_romaji

        log.info("AniList returned nothing for '%s' — trying Kitsu …", search_name)

        # ── 2. Try Kitsu (free API, good anime coverage) ───
        kitsu_romaji = self._get_kitsu().find_romaji(search_name)
        if kitsu_romaji:
            log.info("Using Kitsu romaji: '%s'", kitsu_romaji)
            return kitsu_romaji

        # ── 3. Fall back to pykakasi romanisation ──────────
        log.info(
            "Kitsu also found nothing — falling back to TMDB romaji: '%s'",
            tmdb_romaji,
        )
        return tmdb_romaji

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
