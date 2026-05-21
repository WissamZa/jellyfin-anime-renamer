# Jellyfin Anime Renamer

A Python CLI tool that renames anime episode files into the **Jellyfin-compatible `SxxExx` format** and optionally organises them into season folders — all powered by live metadata from [TMDB](https://www.themoviedb.org/) and [AniList](https://anilist.co/).

```
One Piece/
├── Season 01/
│   └── One Piece - S01E01 - I'm Luffy! The Man Who's Gonna Be King....mkv
├── Season 22/
│   └── One Piece - S22E1137 - I'm Sorry, Dad - Bonney's Tears....mkv
└── Specials/
    ├── One Piece - S00E42 - Special 42.mkv
    └── One Piece - S00E00 - Fan Letter.mkv
```

---

## Features

- **TMDB + AniList fetchers** — primary and fallback metadata sources, fetched in parallel
- **Specials support** — TMDB Season 0 data for OVAs, fan episodes, and specials (`SP42`, `OVA1`, `Fan Letter`, etc.)
- **Season folder organisation** — moves files into `Season 01` … `Season XX` and a `Specials` folder automatically
- **Smart episode parser** — handles `S1E1100`, `[1100]`, `- 1140v2`, `ep1100`, and more
- **Undo / rollback** — every rename and move is logged to `rename_history.json` and fully reversible
- **Dry run mode** — preview all changes before touching a single file
- **`.env` based config** — no secrets in source code, safe to commit

---

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- A free [TMDB API key](https://www.themoviedb.org/settings/api)

---

## Installation

```bash
# Clone the repo
git clone https://github.com/WissamZa/jellyfin-anime-renamer.git
cd jellyfin-anime-renamer

# Install dependencies with uv
uv sync

# Or with pip
pip install requests python-dotenv
```

---

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

```dotenv
# .env

# Required — get your free key at https://www.themoviedb.org/settings/api
TMDB_API_KEY=your_tmdb_api_key_here

# Series
SERIES_NAME=One Piece
TMDB_SERIES_ID=37854
ANILIST_ID=21

# Path to your episode folder
MEDIA_DIR=/path/to/your/anime

# Move files into Season XX subfolders (true/false)
ORGANIZE_INTO_FOLDERS=true
```

> **Finding IDs:**  
> TMDB — open the show page on themoviedb.org, the number in the URL is the series ID  
> AniList — open the anime page on anilist.co, the number in the URL is the anime ID

---

## Usage

```bash
# With uv
uv run python jellyfin_renamer.py

# Or directly
python jellyfin_renamer.py
```

You'll see an interactive menu:

```
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️   JELLYFIN ANIME RENAMER  🏴‍☠️              ║
║    TMDB + AniList  |  Season folders  |  Undo-safe   ║
╚══════════════════════════════════════════════════════╝

  ┌──────────────────────────────────────┐
  │  1. Dry Run  (preview only)          │
  │  2. Live Rename + Organise           │
  │  3. Undo previous renames            │
  │  4. Show config                      │
  │  5. Exit                             │
  └──────────────────────────────────────┘
```

**Always run option 1 first** to preview changes before committing.

---

## How It Works

### Episode detection

The parser tries patterns in order, from most to least specific:

| Pattern | Example match |
|---|---|
| `SxxExx` format | `One Piece - S1E1100.mkv` → ep 1100 |
| Explicit keyword | `episode 42.mkv`, `ep042.mkv` |
| Bracketed number | `[One Piece] [1100].mkv` |
| Dash + number | `One Piece - 1140v2.mkv` → ep 1140 |
| Trailing number | `One_Piece_1100.mkv` |

### Special detection

Files are checked for special patterns **before** episode parsing:

| Pattern | Example |
|---|---|
| `SP##` | `[Judas] One Piece - SP42.mkv` |
| `OVA##` | `One Piece OVA1.mkv` |
| `Special ##` | `One Piece Special 3.mkv` |
| `Fan Letter` | `[Judas] One Piece - Fan Letter.mkv` |
| `Pilot` | `One Piece Pilot.mkv` |

Specials are matched against TMDB Season 0 for real titles. If no match is found, the filename is used as the title.

### Metadata sources

| Fetcher | Used for | API key needed |
|---|---|---|
| TMDB | Regular episodes + specials (primary) | Yes (free) |
| AniList | Episode titles (fallback) | No |
| TMDB Episode Group | Alternative season ordering | Same TMDB key |

### Output structure

With `ORGANIZE_INTO_FOLDERS=true`:

```
One Piece/
├── Season 01/          ← TMDB season number
├── Season 02/
│   └── …
├── Season 22/
│   └── One Piece - S22E1137 - I'm Sorry, Dad….mkv
├── Specials/           ← Season 0 / OVAs / fan episodes
│   └── One Piece - S00E42 - …
└── rename_history.json ← undo log, do not delete
```

With `ORGANIZE_INTO_FOLDERS=false`, all files stay in the root folder, only renamed.

---

## Undo

Every rename and move is recorded in `rename_history.json` inside your media folder. To reverse all changes:

```
Menu → 3. Undo previous renames
```

This moves files back to their original names and locations. Entries are removed from the log as they are restored.

> Do not delete `rename_history.json` if you want to be able to undo.

---

## Development

```bash
# Install dev tools (ruff + pyright)
uv sync --dev

# Lint
uv run ruff check .

# Type check
uv run pyright jellyfin_renamer.py

# Auto-fix safe issues
uv run ruff check . --fix
```

---

## Project Structure

```
jellyfin-anime-renamer/
├── jellyfin_renamer.py   # main script
├── pyproject.toml        # uv / build config
├── .env                  # your secrets (never commit)
├── .env.example          # template to share
├── .gitignore
└── README.md
```

---

## Supported File Types

`.mp4` `.mkv` `.avi` `.m4v` `.flv` `.webm`

---

## License

MIT
