"""
renamer.providers.local
=======================
Fallback provider that builds episode metadata from local filenames.

Used when no provider API can resolve the series — allows renaming to
proceed with the cleaned folder name as the series title and episode
numbers extracted from filenames.  No HTTP calls are made.
"""

from __future__ import annotations

from renamer.config import Config, get_logger
from renamer.providers.base import EpisodeFetcher, EpisodeInfo

log = get_logger(__name__)


class LocalFetcher(EpisodeFetcher):
    """
    Fallback provider that builds episode info from video filenames.

    When the primary provider (TMDB, AniList, Kitsu) cannot resolve a
    series — typically because the folder name does not match any known
    title — this fetcher scans the media directory, extracts episode
    numbers from filenames using :class:`EpisodeNumberParser`, and
    generates placeholder :class:`EpisodeInfo` entries.

    The series name is taken from ``cfg.series_name`` (which should be
    the cleaned folder name by the time this fetcher is created).
    Episode titles are simple placeholders (``"Episode 1"``, etc.)
    because no API metadata is available.

    This ensures that renaming can still proceed, producing clean
    filenames like::

        Series Name - S01E01 - Episode 1.mkv
    """

    name = "Local"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self._episode_map: dict[int, EpisodeInfo] | None = None
        self._specials_map: dict[int, EpisodeInfo] | None = None

    # ── Public API ──────────────────────────────────────────────

    def fetch(self) -> dict[int, EpisodeInfo] | None:
        """Build episode map from video filenames in the media directory."""
        if self._episode_map is not None:
            return self._episode_map

        from renamer.parsers import EpisodeNumberParser, SpecialParser

        ep_parser = EpisodeNumberParser()
        sp_parser = SpecialParser()

        files = sorted(
            p for p in self._cfg.media_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in self._cfg.video_extensions
        )

        mapping: dict[int, EpisodeInfo] = {}

        for path in files:
            # Skip specials — handled by fetch_specials()
            if sp_parser.parse(path.name) is not None:
                continue

            # Try SxxExx pattern first
            season_num, ep_num = ep_parser.parse_season_episode(path.name)
            if season_num is not None and ep_num is not None and season_num > 0:
                abs_num = _allocate_absolute(mapping, season_num, ep_num)
                mapping[abs_num] = EpisodeInfo(
                    absolute=abs_num,
                    season=season_num,
                    episode=ep_num,
                    title=f"Episode {ep_num}",
                    source=self.name,
                    is_special=False,
                )
                continue

            # Try absolute episode number
            abs_num = ep_parser.parse(path.name)
            if abs_num is not None:
                mapping[abs_num] = EpisodeInfo(
                    absolute=abs_num,
                    season=1,
                    episode=abs_num,
                    title=f"Episode {abs_num}",
                    source=self.name,
                    is_special=False,
                )

        if not mapping:
            log.warning("Local: could not extract any episode numbers from filenames.")
            return None

        self._episode_map = mapping
        log.info("Local: built episode map from %d file(s).", len(mapping))
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """Build specials map from video filenames in the media directory."""
        if self._specials_map is not None:
            return self._specials_map

        from renamer.parsers import SpecialParser

        sp_parser = SpecialParser()

        files = sorted(
            p for p in self._cfg.media_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in self._cfg.video_extensions
        )

        specials: dict[int, EpisodeInfo] = {}
        sp_counter = 1

        for path in files:
            sp_num = sp_parser.parse(path.name)
            if sp_num is None:
                continue
            if sp_num == 0:
                sp_num = sp_counter
                sp_counter += 1
            else:
                sp_counter = max(sp_counter, sp_num + 1)

            specials[sp_num] = EpisodeInfo(
                absolute=sp_num,
                season=0,
                episode=sp_num,
                title=f"Special {sp_num}",
                source=self.name,
                is_special=True,
            )

        self._specials_map = specials
        if specials:
            log.info("Local: found %d special(s) from filenames.", len(specials))
        return specials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allocate_absolute(
    existing: dict[int, EpisodeInfo],
    season: int,
    episode: int,
) -> int:
    """
    Compute an absolute episode number for a season/episode pair.

    The strategy is simple: the absolute number is the next available
    after all existing entries.  This keeps the mapping monotonic even
    when files from multiple seasons are present.
    """
    if not existing:
        return episode
    return max(existing.keys()) + 1
