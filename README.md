# Jellyfin Anime Renamer

> Automatically rename and organise anime episodes for Jellyfin, with support for TMDB, AniList, and Kitsu. Hooks into qBittorrent so your seeding continues uninterrupted after a rename.

[![CI](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Interactive CLI](#interactive-cli)
  - [Non-interactive / scripted](#non-interactive--scripted)
  - [qBittorrent hook](#qbittorrent-hook)
- [Output Format](#output-format)
- [Metadata Providers](#metadata-providers)
- [Episode Ordering Modes](#episode-ordering-modes)
- [Undo](#undo)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

---

## Features

| Feature | Details |
|---------|---------|
| **Multi-provider** | TMDB, AniList, Kitsu — switch at any time without re-scanning |
| **Episode Groups** | Use TMDB's alternative orderings (Absolute, DVD, Story Arc…) |
| **Romaji resolution** | AniList human-curated titles with pykakasi fallback |
| **Smart filename parsing** | Handles `SxxExx`, `2x08`, `[1105]`, `ep 42`, bare numbers, OVAs, specials |
| **Folder organisation** | Moves files into `Season 01 / …`, `Season 02 / …`, `Specials / …` |
| **Continuing numbering** | S2E01 becomes S02E13 when S1 had 12 episodes |
| **Absolute numbering** | For long-runners like One Piece: `S23E1163` instead of `S23E28` |
| **qBittorrent-safe moves** | Renames via the qBit API — seeding never breaks |
| **Undo log** | Every session is recorded; one command reverts all renames |
| **Interactive picker** | fzf-style arrow-key / number selection in the terminal |
| **Library scan** | Indexes your entire anime folder into a SQLite database |
| **Dry run** | Preview every rename before touching a single file |

---

## How It Works

```
                ┌─────────────┐
                │  .env file  │  API keys, paths, provider
                └──────┬──────┘
                       │
          ┌────────────▼───────────────┐
          │      jellyfin_renamer.py   │  Interactive menu / CLI flags
          └────────────┬───────────────┘
                       │ Config
          ┌────────────▼───────────────┐
          │        AnimeRenamer        │  Core engine
          │  • load/save series cache  │
          │  • auto-search provider    │
          │  • parse filenames         │
          │  • format new names        │
          │  • rename / move files     │
          └──┬──────────┬──────────────┘
             │          │
   ┌─────────▼──┐  ┌────▼──────────┐
   │ TMDBFetcher│  │ AniListFetcher│  KitsuFetcher …
   │ (episodes) │  │  (episodes)   │
   └────────────┘  └───────────────┘
```

---

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (recommended) **or** pip
- A free [TMDB API key](https://www.themoviedb.org/settings/api) for TMDB (not needed for AniList / Kitsu)

---

## Installation

### With uv (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/jellyfin-anime-renamer.git
cd jellyfin-anime-renamer
uv sync
```

### With pip

```bash
git clone https://github.com/YOUR_USERNAME/jellyfin-anime-renamer.git
cd jellyfin-anime-renamer
pip install -e .
```

---

## Configuration

Copy the example and fill in your values:

```bash
cp .env.example .env
```

```ini
# .env

# ── TMDB (required for the TMDB provider) ────────────────
TMDB_API_KEY=your_tmdb_api_key_here

# ── Provider: tmdb | anilist | kitsu ─────────────────────
PROVIDER=tmdb

# ── Series (optional — auto-detected from folder name) ───
SERIES_NAME=
TMDB_SERIES_ID=
ANILIST_ID=
KITSU_ID=

# ── Paths ─────────────────────────────────────────────────
MEDIA_DIR=/path/to/your/anime/show

# qBittorrent hook only
BASE_DOWNLOAD_PATH=/mnt/D/Torrent
QBIT_URL=http://localhost:8080
QBIT_USERNAME=admin
QBIT_PASSWORD=your_password

# ── Behaviour ─────────────────────────────────────────────
ORGANIZE_INTO_FOLDERS=true
ABSOLUTE_NUMBERING=false
EPISODE_START_MODE=per_season   # or: continuing

# ── TMDB Episode Group (optional) ────────────────────────
EPISODE_GROUP_ID=
```

> **Tip:** `SERIES_NAME` and IDs are cached per-folder in `.series_cache.json` after the first successful run. You can leave them blank and let auto-search fill them in.

---

## Usage

### Interactive CLI

```bash
uv run jellyfin_renamer.py
```

The main menu:

```
+======================================================+
|       JELLYFIN ANIME RENAMER  v5.7.0                 |
|  TMDB + AniList + Kitsu | Episode Groups | Undo-safe  |
+======================================================+

 > Dry Run  (preview only)
   Live Rename + Organise
   Undo previous renames
   Show config
   Switch provider
   Clear series cache
   Set title / provider ID manually
   Select episode ordering (group / default)
   Select series title (TMDB alt titles + AniList)
   Scan library to SQLite database
   Set episode numbering mode (per-season / continuing)
   Exit
```

Use **↑ ↓** or **j k** to navigate, **Enter** to select, **q** to cancel.

**Always run a Dry Run first** — it shows exactly what would change before any files are touched.

---

### Non-interactive / scripted

```bash
# Dry run — print what would be renamed
uv run jellyfin_renamer.py --dry-run

# Override series and provider via flags
uv run jellyfin_renamer.py --title "One Piece" --provider tmdb --tmdb-id 37854

# Use an absolute-order episode group
uv run jellyfin_renamer.py --episode-group 5f5a11c5c4b3c7002196453 --absolute

# AniList provider (no API key needed)
uv run jellyfin_renamer.py --provider anilist --anilist-id 21

# All flags
uv run jellyfin_renamer.py --help
```

---

### qBittorrent hook

The hook is triggered automatically when a torrent finishes downloading. It:

1. Looks up the real series name via Nyaa (from the torrent hash)
2. Searches TMDB / Kitsu for the series ID
3. Moves the torrent to `BASE_DOWNLOAD_PATH/<series>/` via the qBit API
4. Renames the files — through qBit's API so seeding is never broken

**Setup in qBittorrent** → Settings → Downloads → Run external program on torrent completion:

```
/path/to/.venv/bin/python /path/to/qbit_hook.py "%I" "%N" "%F"
```

Or with uv:

```
uv run /path/to/qbit_hook.py "%I" "%N" "%F"
```

---

## Output Format

Files are renamed using this template (customisable in `Config`):

```
{series} - S{season:02d}E{episode:02d} - {title}.{ext}
```

Examples:

| Input | Output |
|-------|--------|
| `[SubGroup] One Piece - 1105 [1080p].mkv` | `One Piece - S23E28 - The Hero's Name.mkv` |
| `Frieren.S01E05.mkv` | `Frieren Beyond Journey's End - S01E05 - The Journey Begins.mkv` |
| `[GJM] Violet Evergarden OVA.mkv` | `Violet Evergarden - S00E01 - Eternity and the Auto Memory Doll.mkv` |

Specials use:

```
{series} - S00E{episode:02d} - {title}.{ext}
```

Season folders:

```
Anime Title/
├── Season 01/
│   ├── Anime Title - S01E01 - Episode Title.mkv
│   └── …
├── Season 02/
│   └── …
└── Specials/
    └── Anime Title - S00E01 - OVA Title.mkv
```

---

## Metadata Providers

### TMDB
- Requires a free API key
- Best coverage for seasonal anime
- Supports **Episode Groups** for alternative orderings (Absolute, DVD, Story Arc…)
- Use option **8** in the menu to browse and select an episode group

### AniList
- No API key required (free GraphQL API)
- Official romaji titles — highest quality for Japanese titles
- Used automatically as a romaji cross-reference by the TMDB provider

### Kitsu
- No API key required
- Walks sequel chains to build correct season/episode mappings
- Good fallback for older or niche titles

---

## Episode Ordering Modes

| Mode | Description | Example |
|------|-------------|---------|
| `per_season` (default) | Each season starts at E01 | S02E01 |
| `continuing` | Episodes continue across seasons | S02E13 if S1 had 12 eps |
| Absolute numbering | Uses absolute episode number in the SxxExx slot | S23E1163 for One Piece |
| TMDB Episode Group | Uses a custom TMDB ordering (Absolute Order, DVD Order…) | Any ordering TMDB lists |

Switch modes via the interactive menu (option **11** for continuing/per-season, option **8** for episode groups).

---

## Undo

Every live rename session is recorded in `rename_history.json` inside the media folder. To revert:

```
Main menu → Undo previous renames
```

Or programmatically:

```python
from renamer import AnimeRenamer, Config
from pathlib import Path

cfg = Config.from_env()
AnimeRenamer(cfg).undo()
```

---

## Development

```bash
# Install all dev dependencies
uv sync --group dev

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=renamer --cov-report=term-missing

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run pyright renamer/
```

### Project Structure

```
jellyfin-anime-renamer/
├── renamer/                    # Core library
│   ├── __init__.py             # Public API surface
│   ├── config.py               # Config dataclass + Provider enum
│   ├── renamer.py              # AnimeRenamer engine
│   ├── parsers.py              # Filename parsers
│   ├── cache.py                # Per-folder .series_cache.json
│   ├── history.py              # Undo log
│   ├── picker.py               # Interactive terminal picker
│   ├── romaniser.py            # Japanese → romaji conversion
│   └── providers/
│       ├── base.py             # EpisodeFetcher ABC + dataclasses
│       ├── registry.py         # Provider plugin registry
│       ├── tmdb.py             # TMDB fetcher + search
│       ├── anilist.py          # AniList fetcher
│       ├── kitsu.py            # Kitsu fetcher
│       └── romaji_resolver.py  # AniList × TMDB romaji selection
├── tests/
│   ├── test_core.py            # Unit tests (no network)
│   └── test_rename_pipeline.py # Integration tests (tmp files)
├── jellyfin_renamer.py         # Interactive CLI entry point
├── qbit_hook.py                # qBittorrent completion hook
├── qbit_delete_hook.py         # qBittorrent deletion hook
├── .env.example                # Environment variable template
├── .github/
│   └── workflows/
│       ├── ci.yml              # Lint + test on every push/PR
│       └── release.yml         # Build + publish on tag push
└── pyproject.toml
```

### Adding a new provider

1. Create `renamer/providers/myprovider.py` with a class that extends `EpisodeFetcher`.
2. Implement `fetch()` and `fetch_specials()`.
3. Register it in `renamer/providers/registry.py`:

```python
def _myprovider_factory(cfg: Config) -> EpisodeFetcher:
    from renamer.providers.myprovider import MyProviderFetcher
    return MyProviderFetcher(cfg.my_series_id, cfg)

registry.register(Provider.MyProvider, _myprovider_factory)
```

4. Add the enum value to `Provider` in `config.py`.

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Make your changes and add tests
4. Ensure `ruff check .` and `pytest` both pass
5. Open a pull request against `main`

For bug reports or feature requests, open an [issue](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/issues).

---

## License

MIT — see [LICENSE](LICENSE) for details.
