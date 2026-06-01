# Jellyfin Anime Renamer

> Automatically rename and organise anime episodes for Jellyfin, with metadata from TMDB, AniList, Kitsu, or AniDB. Hooks into qBittorrent so seeding continues uninterrupted after a rename.

[![CI](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/actions/workflows/ci.yml)
[![Release](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/actions/workflows/release.yml/badge.svg)](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Interactive CLI](#interactive-cli)
  - [Command-line flags](#command-line-flags)
  - [qBittorrent hook](#qbittorrent-hook)
  - [Programmatic API](#programmatic-api)
- [Output Format](#output-format)
- [Metadata Providers](#metadata-providers)
- [Episode Ordering Modes](#episode-ordering-modes)
- [Undo](#undo)
- [Development](#development)
  - [Project Structure](#project-structure)
  - [Running Tests](#running-tests)
  - [Workflows](#workflows)
  - [Adding a Provider](#adding-a-provider)
- [Contributing](#contributing)
- [License](#license)

---

## Features

| Feature | Details |
|---------|---------|
| **Multi-provider** | TMDB, AniList, Kitsu, AniDB — switchable per-folder |
| **TMDB Episode Groups** | Use alternate orderings: Absolute, DVD, Story Arc, etc. |
| **Romaji resolution** | AniList curated titles + pykakasi fallback for Japanese text |
| **Smart filename parsing** | Handles `SxxExx`, `2x08`, `[1105]`, `ep 42`, bare numbers, OVAs, specials |
| **Folder organisation** | Moves files into `Season 01/`, `Season 02/`, `Specials/` |
| **Continuing numbering** | S2E01 becomes S02E13 when S1 had 12 episodes |
| **Absolute numbering** | Long-runners like One Piece: `S23E1163` instead of `S23E28` |
| **ED2K hash lookup** | AniDB hash-based identification for files that can't be named-matched |
| **qBittorrent-safe moves** | Renames via the qBit API — seeding never breaks |
| **Undo log** | Every session recorded; one command reverts all renames |
| **Interactive picker** | Arrow-key / number selection in the terminal |
| **Dry run** | Preview every rename before touching a single file |
| **Subtitle matching** | Matches `.srt`/`.ass` files to renamed video files by season/episode |
| **Folder icons** | Sets poster art as folder icon (Dolphin / Nautilus / Thunar) |
| **Recursive scanning** | Scan sub-folder trees to a configurable depth |

---

## Architecture

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
          │  • series cache            │
          │  • auto-search provider    │
          │  • filename parsing        │
          │  • episode map building    │
          │  • rename / move files     │
          │  • undo log                │
          └──┬──────────┬──────────────┘
             │          │
   ┌─────────▼──┐  ┌────▼────────┐  ┌──────────────┐  ┌───────────┐
   │ TMDBFetcher│  │AniListFetch │  │ KitsuFetcher │  │AniDBFetch │
   └────────────┘  └─────────────┘  └──────────────┘  └───────────┘
```

---

## Requirements

- Python **3.11** or newer
- [uv](https://docs.astral.sh/uv/) (recommended) **or** pip
- Free [TMDB API key](https://www.themoviedb.org/settings/api) for TMDB provider (not needed for AniList / Kitsu)
- [AniDB account](https://anidb.net/) for AniDB / hash-based identification (free)

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

### From PyPI

```bash
pip install jellyfin-anime-renamer
# or
uv add jellyfin-anime-renamer
```

---

## Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

### Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TMDB_API_KEY` | — | Required when `PROVIDER=tmdb`. Get one free at [themoviedb.org](https://www.themoviedb.org/settings/api) |
| `PROVIDER` | `tmdb` | Metadata provider: `tmdb`, `anilist`, `kitsu`, `anidb` |
| `MEDIA_DIR` | `.` | Absolute path to the directory of anime files to rename |
| `BASE_DOWNLOAD_PATH` | — | Root of your torrent download folder (prevents renaming it accidentally) |
| `ORGANIZE_INTO_FOLDERS` | `true` | Move files into `Season XX/` sub-folders |
| `ABSOLUTE_NUMBERING` | `false` | Use absolute episode number in the `SxxExx` slot |
| `EPISODE_START_MODE` | `per_season` | `per_season` or `continuing` (see [Episode Ordering](#episode-ordering-modes)) |
| `SCAN_RECURSIVE` | `false` | Scan sub-folders recursively |
| `SCAN_DEPTH` | `3` | Maximum recursion depth when `SCAN_RECURSIVE=true` |

### Series identity (optional — auto-detected at runtime)

| Variable | Description |
|----------|-------------|
| `SERIES_NAME` | Override the series name used for searching |
| `TMDB_SERIES_ID` | Skip search; use this TMDB series ID directly |
| `ANILIST_ID` | AniList series ID |
| `KITSU_ID` | Kitsu series ID |
| `EPISODE_GROUP_ID` | TMDB episode group ID for alternate orderings |

### AniDB / hash-based identification

| Variable | Default | Description |
|----------|---------|-------------|
| `ANIDB_USERNAME` | — | Required when `PROVIDER=anidb` |
| `ANIDB_PASSWORD` | — | Required when `PROVIDER=anidb` |
| `ANIDB_API_KEY` | — | Optional AES-128 session key |
| `ANIDB_CLIENT` | `jenameramer` | Client name registered at AniDB |
| `ANIDB_CLIENT_VER` | `1` | Client version |
| `ANIDB_OFFLINE` | `false` | Use only cached AniDB data |
| `USE_HASH` | `false` | Compute ED2K hash for unidentified files as a fallback |

### qBittorrent hook

| Variable | Description |
|----------|-------------|
| `QBIT_URL` | qBittorrent WebUI URL (e.g. `http://localhost:8080`) |
| `QBIT_USERNAME` | WebUI username |
| `QBIT_PASSWORD` | WebUI password |

---

## Usage

### Interactive CLI

Launch the interactive menu from the media directory you want to rename:

```bash
cd /mnt/anime/Attack\ on\ Titan
uv run jellyfin_renamer.py
```

Or point it at a directory:

```bash
MEDIA_DIR=/mnt/anime/One\ Piece uv run jellyfin_renamer.py
```

The main menu offers:

```
+======================================================+
|       JELLYFIN ANIME RENAMER  v2.2.0                 |
|  TMDB + AniList + Kitsu + AniDB | Episode Groups     |
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

> **Always run Dry Run first.** It shows exactly what would change before any file is touched.

---

### Command-line flags

```bash
# Preview what would be renamed (no changes)
uv run jellyfin_renamer.py --dry-run

# Override series and provider
uv run jellyfin_renamer.py --title "One Piece" --provider tmdb --tmdb-id 37854

# Use a TMDB episode group (Absolute Order, etc.)
uv run jellyfin_renamer.py --episode-group 5f5a11c5c4b3c7002196453 --absolute

# AniList provider (no API key required)
uv run jellyfin_renamer.py --provider anilist --anilist-id 21

# Recursive scan, depth 2
uv run jellyfin_renamer.py --recursive --scan-depth 2

# All options
uv run jellyfin_renamer.py --help
```

---

### qBittorrent hook

The hook fires automatically when a torrent finishes downloading. It:

1. Resolves the real series name from the torrent metadata
2. Searches the configured provider for a series match
3. Moves the torrent via the qBit API to `BASE_DOWNLOAD_PATH/<series>/` — **seeding is never interrupted**
4. Renames all files through the same API

**Setup** — qBittorrent → Settings → Downloads → *Run external program on torrent completion*:

```
uv run /path/to/qbit_hook.py "%I" "%N" "%F"
```

Or with a plain Python venv:

```
/path/to/.venv/bin/python /path/to/qbit_hook.py "%I" "%N" "%F"
```

A separate deletion hook (`qbit_delete_hook.py`) removes the media folder when a torrent is deleted from qBittorrent.

---

### Programmatic API

The library is fully importable for use in scripts or other tools:

```python
from pathlib import Path
from renamer import AnimeRenamer, Config, Provider

# Build config programmatically
cfg = Config(
    tmdb_api_key="your_key",
    series_name="Frieren",
    tmdb_series_id=209867,
    media_dir=Path("/mnt/anime/Frieren"),
    organize_into_folders=True,
    provider=Provider.TMDB,
)

# Validate before running
errors = cfg.validate()
if errors:
    for e in errors:
        print(f"Config error: {e}")
    raise SystemExit(1)

renamer = AnimeRenamer(cfg)

# Dry run first
results = renamer.run(dry_run=True)
for r in results:
    print(f"{r.original} -> {r.renamed}")

# Live rename
results = renamer.run(dry_run=False)

# Undo
renamer.undo()
```

**Load config from environment / `.env` file:**

```python
cfg = Config.from_env()          # reads .env + real environment
cfg = Config.from_env(provider=Provider.AniList)  # override individual fields
```

**Subtitle matching:**

```python
from renamer.subtitle_matcher import (
    scan_subtitle_matches,
    preview_subtitle_renames,
    execute_subtitle_renames,
)

scan = scan_subtitle_matches(
    media_dir=Path("/mnt/anime/Frieren"),
    video_extensions=(".mkv", ".mp4"),
    subtitle_extensions=(".srt", ".ass", ".ssa"),
)
preview_subtitle_renames(scan)
execute_subtitle_renames(scan, dry_run=False)
```

---

## Output Format

Default filename template:

```
{series} - S{season:02d}E{episode:02d} - {title}{ext}
```

Specials template:

```
{series} - S00E{episode:02d} - {title}{ext}
```

Examples:

| Input | Output |
|-------|--------|
| `[SubGroup] One Piece - 1105 [1080p].mkv` | `One Piece - S23E28 - The Hero's Name.mkv` |
| `Frieren.S01E05.mkv` | `Frieren Beyond Journey's End - S01E05 - The Journey Begins.mkv` |
| `[GJM] Violet Evergarden OVA.mkv` | `Violet Evergarden - S00E01 - Eternity and the Auto Memory Doll.mkv` |

Resulting folder structure:

```
Attack on Titan/
├── Season 01/
│   ├── Attack on Titan - S01E01 - To You, in 2000 Years.mkv
│   └── …
├── Season 02/
│   └── …
└── Specials/
    └── Attack on Titan - S00E01 - Ilse's Notebook.mkv
```

Templates are configurable in `Config`:

```python
cfg = Config(
    name_template="{series} - S{season:02d}E{episode:02d} - {title}{ext}",
    special_template="{series} - S00E{episode:02d} - {title}{ext}",
    season_folder_template="Season {season:02d}",
    specials_folder_name="Specials",
)
```

---

## Metadata Providers

### TMDB

- Requires a free API key from [themoviedb.org](https://www.themoviedb.org/settings/api)
- Best general coverage for seasonal anime
- Supports **Episode Groups** for alternative orderings (Absolute, DVD, Story Arc…)
  - Use option **8** in the interactive menu to browse and select
- AniList titles are used as a romaji cross-reference automatically

### AniList

- No API key required (public GraphQL API)
- Highest-quality romaji/English titles — curated by the community
- Recommended as the primary provider for title accuracy

### Kitsu

- No API key required
- Walks sequel chains to build correct season/episode mappings
- Good fallback for older or niche titles not in TMDB

### AniDB

- Requires a free account at [anidb.net](https://anidb.net/)
- Uses the **UDP API** with optional AES-128 encryption
- Enables **ED2K hash-based file identification** — the most accurate matching method for files with unusual names
- Automatically falls back to filename-based identification if authentication fails
- `ANIDB_OFFLINE=true` uses only the bundled `anidb_titles.dat.gz` — no network calls

---

## Episode Ordering Modes

| Mode | Config value | Description |
|------|-------------|-------------|
| Per-season (default) | `per_season` | Each season resets to E01 — `S02E01` |
| Continuing | `continuing` | Episodes count up across seasons — `S02E13` if S1 had 12 eps |
| Absolute numbering | `absolute_numbering=True` | Places the global episode count in the `Exx` slot — `S23E1163` |
| TMDB Episode Group | via `EPISODE_GROUP_ID` | Use any alternate ordering TMDB defines for the series |

Switch modes in the interactive menu: option **11** for per-season vs. continuing, option **8** for episode groups.

---

## Undo

Every live rename session writes a `rename_history.json` file inside the media folder mapping each new filename back to its original. To revert:

**Via the menu:** Main menu → *Undo previous renames*

**Via CLI:**

```bash
uv run jellyfin_renamer.py --undo
```

**Programmatically:**

```python
from renamer import AnimeRenamer, Config

cfg = Config.from_env()
AnimeRenamer(cfg).undo()
```

The history file uses absolute paths on both sides, so undo works correctly even if the working directory changes between sessions.

---

## Development

### Project Structure

```
jellyfin-anime-renamer/
├── renamer/                      # Core library
│   ├── __init__.py               # Public API surface + __version__
│   ├── config.py                 # Config dataclass, Provider enum, logging
│   ├── renamer.py                # AnimeRenamer engine
│   ├── parsers.py                # Filename parsers (SxxExx, brackets, specials)
│   ├── cache.py                  # Per-folder .series_cache.json
│   ├── history.py                # JSON undo log
│   ├── picker.py                 # Interactive terminal picker
│   ├── romaniser.py              # Japanese → romaji + smart title-casing
│   ├── security.py               # Path traversal / symlink guards
│   ├── subtitles.py              # Subtitle rename helpers
│   ├── subtitle_matcher.py       # Match subs to videos by S/E number
│   ├── hash_organizer.py         # ED2K hash-based batch organiser
│   ├── ed2k.py                   # ED2K checksum computation
│   ├── cli/
│   │   ├── main.py               # CLI argument parsing + entry point
│   │   ├── menus.py              # Interactive menu screens
│   │   ├── multi_series.py       # Multi-series batch operations
│   │   └── scanner.py            # Library scanner
│   ├── icons/
│   │   ├── fetcher.py            # Download poster art from providers
│   │   └── setter.py             # Apply poster as folder icon (Linux DE)
│   └── providers/
│       ├── base.py               # EpisodeFetcher ABC + EpisodeInfo dataclasses
│       ├── registry.py           # Provider plugin registry
│       ├── tmdb.py               # TMDB fetcher
│       ├── anilist.py            # AniList GraphQL fetcher
│       ├── kitsu.py              # Kitsu REST fetcher
│       ├── anidb.py              # AniDB UDP fetcher
│       ├── anidb_cache.py        # AniDB local title cache
│       ├── local.py              # Local file provider
│       └── romaji_resolver.py    # AniList × TMDB romaji selection
├── tests/
│   ├── test_core.py              # Unit tests — sanitize, parsers, config, offsets
│   ├── test_rename_pipeline.py   # Integration tests — full rename in tmp dir
│   ├── test_romaniser.py         # Romaniser + anime_title_case
│   ├── test_storage.py           # RenameHistory + SeriesCache
│   ├── test_subtitle_matcher.py  # Subtitle scan/match/execute
│   └── test_config.py            # Config.from_env, validate, Provider.from_str
├── .github/
│   └── workflows/
│       ├── ci.yml                # Lint + test on every push / PR
│       └── release.yml           # Build + PyPI publish on version tag
├── jellyfin_renamer.py           # Interactive CLI entry point
├── qbit_hook.py                  # qBittorrent completion hook
├── qbit_delete_hook.py           # qBittorrent deletion hook
├── .env.example                  # Environment variable template
├── title_case_particles.json     # Japanese particles + named specials
├── anidb_titles.dat.gz           # Bundled AniDB title database
└── pyproject.toml                # Project metadata + tool config
```

### Running Tests

```bash
# Install dev dependencies
uv sync --dev

# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=renamer --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_core.py -v

# Run tests matching a keyword
uv run pytest -k "subtitle" -v
```

The test suite is fully offline — no network calls are made. All provider interactions are mocked via `unittest.mock.patch`.

### Linting and Type Checking

```bash
# Check for lint errors
uv run ruff check renamer/ tests/

# Auto-fix safe issues
uv run ruff check --fix renamer/ tests/

# Check formatting
uv run ruff format --check renamer/ tests/

# Apply formatting
uv run ruff format renamer/ tests/

# Type-check
uv run pyright renamer/
```

### Workflows

Two GitHub Actions workflows are included:

**`ci.yml`** — runs on every push to `main`/`develop` and on all pull requests:

1. **Lint** — `ruff check` + `ruff format --check` + `pyright`
2. **Test** — `pytest` across Python 3.11, 3.12, 3.13 on Ubuntu, Windows, and macOS
3. **Build** — verifies the wheel and sdist can be built

**`release.yml`** — triggered by a version tag (e.g. `v2.3.2`):

1. **Validate** — checks that the tag matches `pyproject.toml` and `renamer/__init__.py`
2. **Test** — full test matrix
3. **Build** — wheel + sdist
4. **Publish** — uploads to PyPI via OIDC trusted publishing (no API token required)
5. **GitHub Release** — creates a release with auto-generated changelog and attached dist files

**Releasing a new version:**

```bash
# 1. Update version in exactly two places:
#    - pyproject.toml  → version = "X.Y.Z"
#    - renamer/__init__.py → __version__ = "X.Y.Z"

# 2. Commit the version bump
git add pyproject.toml renamer/__init__.py
git commit -m "chore: bump version to X.Y.Z"

# 3. Tag and push — the release workflow fires automatically
git tag vX.Y.Z
git push origin main --tags
```

> Pre-release tags (e.g. `v2.3.0-rc1`) are published as GitHub pre-releases.

### Adding a Provider

1. Create `renamer/providers/myprovider.py` extending `EpisodeFetcher`:

```python
from renamer.providers.base import EpisodeFetcher, EpisodeInfo

class MyProviderFetcher(EpisodeFetcher):
    def fetch(self) -> dict[int, EpisodeInfo]:
        ...

    def fetch_specials(self) -> list[EpisodeInfo]:
        ...
```

2. Add an enum value to `Provider` in `renamer/config.py`.

3. Register a factory in `renamer/providers/registry.py`:

```python
def _myprovider_factory(cfg: Config) -> EpisodeFetcher:
    from renamer.providers.myprovider import MyProviderFetcher
    return MyProviderFetcher(cfg.my_series_id, cfg)

registry.register(Provider.MyProvider, _myprovider_factory)
```

4. Add tests in `tests/`.

---

## Contributing

Contributions are welcome. Please:

1. Fork the repository and create a feature branch: `git checkout -b feat/my-feature`
2. Write tests for any new behaviour
3. Ensure `uv run ruff check .` and `uv run pytest` both pass
4. Open a pull request against `main` with a clear description

For bugs or feature requests, open an [issue](https://github.com/YOUR_USERNAME/jellyfin-anime-renamer/issues).

---

## License

MIT — see [LICENSE](LICENSE) for full terms.
