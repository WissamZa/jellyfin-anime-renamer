# Jellyfin Anime Renamer

Automatically renames and organises anime episode files into the `Series - SxxExx - Title` format that Jellyfin (and Plex/Emby) expect. Supports three metadata providers, qBittorrent integration, and smart absolute-episode numbering for long-running series like One Piece.

---

## Features

- **Three metadata providers** — TMDB (default), AniList (free, no key), Kitsu (best season splits)
- **Smart episode numbering** — short series use per-season numbers (`S02E01`); long-running anime (>100 episodes by default) use the absolute count (`S23E1163`)
- **qBittorrent hook** — auto-renames as soon as a torrent completes, keeps seeding intact
- **Season folder organisation** — moves files into `Season 01/`, `Season 02/`, `Specials/`
- **Romaji titles** — Japanese titles are romanised via AniList's curated database or pykakasi
- **Per-folder cache** — avoids redundant API calls on subsequent runs
- **Undo** — every rename is logged; one command reverts all changes
- **Dry-run preview** — see exactly what will happen before touching a single file

---

## Output Format

```
One Piece - S23E1163 - Home Tehoshii Robin to Sauro no Saikai.mkv
Shingeki no Kyojin - S04E28 - The Dawn of Humanity.mkv
Re Zero kara Hajimeru Isekai Seikatsu - S00E01 - Memory Snow.mkv
```

Files are placed in season sub-folders when `ORGANIZE_INTO_FOLDERS=true`:

```
One Piece/
  Season 23/
    One Piece - S23E1163 - Home Tehoshii Robin to Sauro no Saikai.mkv
  Specials/
    One Piece - S00E01 - Episode of Merry.mkv
```

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) **or** pip
- A free [TMDB API key](https://www.themoviedb.org/settings/api) (only required when using the TMDB provider)

---

## Installation

```bash
git clone https://github.com/youruser/jellyfin-anime-renamer
cd jellyfin-anime-renamer
cp .env.example .env
# Edit .env — add TMDB_API_KEY and set MEDIA_DIR
uv sync          # installs all dependencies
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `PROVIDER` | `tmdb` | Metadata provider: `tmdb`, `anilist`, or `kitsu` |
| `TMDB_API_KEY` | — | Required when `PROVIDER=tmdb` |
| `MEDIA_DIR` | `.` | Folder to scan (overridden by `--path`) |
| `BASE_DOWNLOAD_PATH` | `/mnt/D/Torrent` | Root folder for qBit hook downloads |
| `SERIES_NAME` | *(folder name)* | Override series name (skips auto-detection) |
| `TMDB_SERIES_ID` | *(auto)* | Skip TMDB search if you already know the ID |
| `ANILIST_ID` | *(auto)* | Skip AniList search |
| `KITSU_ID` | *(auto)* | Skip Kitsu search |
| `ORGANIZE_INTO_FOLDERS` | `true` | Move files into `Season XX/` sub-folders |
| `ABSOLUTE_EPISODE_THRESHOLD` | `100` | Use absolute ep numbers above this count (0 = never, -1 = always) |
| `QBIT_URL` | `http://localhost:8080` | qBittorrent Web API URL |
| `QBIT_USERNAME` | — | qBittorrent username |
| `QBIT_PASSWORD` | — | qBittorrent password |

---

## Usage

### Interactive CLI

```bash
# Use MEDIA_DIR from .env
uv run jellyfin_renamer.py

# Scan a specific folder
uv run jellyfin_renamer.py --path /mnt/D/Torrent/OnePiece

# Override provider
uv run jellyfin_renamer.py --path . --provider anilist

# Manual title + provider ID (skips all API searches)
uv run jellyfin_renamer.py --path . --title "One Piece" --provider tmdb --tmdb-id 37854
```

The interactive menu offers:

```
  1. Dry Run  (preview only)
  2. Live Rename + Organise
  3. Undo previous renames
  4. Show config
  5. Switch provider
  6. Clear series cache (re-search)
  7. Set title / provider ID manually
  8. Exit
```

### Non-interactive (scripting / cron)

```bash
# Preview without touching files
uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --dry-run

# Rename immediately (no menu)
uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --run

# AniList provider, no TMDB key needed
uv run jellyfin_renamer.py --path . --provider anilist --run
```

---

## Providers

| Provider | API Key | Best for |
|---|---|---|
| **TMDB** (default) | Required | Most series; best season/episode metadata |
| **AniList** | None | Human-curated romaji titles; free |
| **Kitsu** | None | Anime with complex season splits (e.g. Re:Zero, Index) |

Providers are tried in order when cross-referencing titles. The resolved name and IDs are cached in `.series_cache.json` inside the media folder so subsequent runs skip the API calls entirely.

---

## Long-running Anime (Absolute Episode Numbers)

Series like One Piece (1163+ episodes) or Naruto span many seasons on TMDB. Using just the within-season episode number would produce confusing filenames like `One Piece - S23E01`. Instead, when a series has more than `ABSOLUTE_EPISODE_THRESHOLD` episodes (default: 100), the **absolute episode number** is used:

```
One Piece - S23E1163 - Home Tehoshii Robin to Sauro no Saikai.mkv
```

Adjust the threshold in `.env`:

```ini
ABSOLUTE_EPISODE_THRESHOLD=100   # default — applies to series with >100 episodes
ABSOLUTE_EPISODE_THRESHOLD=0     # always use within-season numbers
ABSOLUTE_EPISODE_THRESHOLD=-1    # always use absolute numbers for every series
```

---

## qBittorrent Hook

Automatically processes torrents on completion. Configure in qBittorrent:

**Settings → Downloads → Run external program on torrent completion:**
```
python /path/to/qbit_hook.py "%I" "%N" "%F"
```

The hook:
1. Looks up the real title from Nyaa (by torrent hash)
2. Searches for the series using the configured provider
3. Moves the torrent's save location to `BASE_DOWNLOAD_PATH/<series>/`
4. Renames files via the qBittorrent API (seeding continues uninterrupted)
5. Organises into season sub-folders

**Manual trigger** (re-process the last completed torrent):
```bash
uv run qbit_hook.py
```

**Re-process by hash:**
```bash
uv run qbit_hook.py -hash <torrent_hash>
```

---

## Project Structure

```
jellyfin-anime-renamer/
├── jellyfin_renamer.py        # Interactive CLI entry point
├── qbit_hook.py               # qBittorrent automation hook
├── renamer_core.py            # Legacy monolith (kept for reference)
├── .env.example               # Copy to .env and fill in your values
├── title_case_particles.json  # Japanese/English particles for title casing
├── anidb_titles.dat.gz        # AniDB title database (offline fallback)
├── pyproject.toml
└── renamer/                   # Main package
    ├── __init__.py
    ├── config.py              # Config class, Provider enum, logging
    ├── cache.py               # Per-folder series metadata cache
    ├── history.py             # Rename undo log
    ├── romaniser.py           # Japanese → romaji (pykakasi + title-case)
    ├── renamer.py             # AnimeRenamer — main engine
    ├── parsers.py             # Episode number & special parsers
    └── providers/
        ├── __init__.py        # ProviderRegistry (plugin system)
        ├── base.py            # EpisodeFetcher ABC, data classes
        ├── tmdb.py            # TMDB fetcher + search
        ├── anilist.py         # AniList GraphQL fetcher
        ├── kitsu.py           # Kitsu JSON fetcher (sequel-chain seasons)
        └── romaji_resolver.py # Cross-reference romaji across providers
```

---

## Undo

Every live rename is recorded in `rename_history.json` inside the media folder. To revert:

```bash
uv run jellyfin_renamer.py --path /mnt/D/Torrent/OnePiece
# → choose option 3 (Undo)
```

---

## Adding a New Provider

1. Create `renamer/providers/your_provider.py` — subclass `EpisodeFetcher`, implement `fetch()` and `fetch_specials()`.
2. Add the enum value in `renamer/config.py` → `Provider`.
3. Register it in `renamer/providers/__init__.py` (or `registry.py`) with `ProviderRegistry.register(...)`.

No other files need to change — the registry handles everything.

---

## License

MIT
