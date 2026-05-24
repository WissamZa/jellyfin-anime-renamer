"""
renamer.providers — Plugin-based provider system.

Adding a new provider only requires:
  1. Create a new file (e.g. renamer/providers/tvdb.py)
  2. Subclass EpisodeFetcher from renamer.providers.base
  3. Implement: fetch(), fetch_specials(), and optionally find_series()
  4. Register it in ProviderRegistry._PROVIDERS below
  5. Add the enum value to Provider in renamer.config

That's it — the rest of the codebase discovers providers through
the registry automatically.
"""

from renamer.providers.base import EpisodeFetcher, SeriesSearchResult
from renamer.providers.registry import ProviderRegistry

__all__ = [
    "EpisodeFetcher",
    "SeriesSearchResult",
    "ProviderRegistry",
]
