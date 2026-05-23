# 🏴‍☠️ Jellyfin Anime Renamer

A Python CLI tool that renames anime episode files into **Jellyfin-compatible `SxxExx` format**, organises them into season folders, and hooks directly into **qBittorrent** to rename files automatically after every download — without interrupting seeding.

```
Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni Sareteita Ken/
├── Season 01/
│   └── Otonari no Tenshi-sama - S01E01 - Chiisana Yakusoku.mkv
├── Season 02/
│   └── Otonari no Tenshi-sama - S02E08 - ...mkv
└── Specials/
    └── Otonari no Tenshi-sama - S00E01 - ...mkv
```

---

## Features

| Feature | Details |
|---|---|
| **Multi-source metadata** | TMDB (primary) + AniList + AniDB title dump |
| **Romaji titles** | AniList → AniDB → pykakasi, in priority order |
| **Smart title-case** | Japanese particles (`no`, `ni`, `wa` …) stay lowercase |
| **Season folders** | Organises into `Season 01/` … `Specials/` automatically |
| **qBittorrent hook** | Renames via the qBit API — seeding never interrupted |
| **Specials** | Detects `SP42`, `OVA1`, `Fan Letter`, `Pilot`, etc. |
| **Smart parser** | Handles `S02E08`, `[1100]`, `- 1140v2`, `ep42`, and more |
| **Undo** | Every rename logged to `rename_history.json`, fully reversible |
| **Dry-run** | Preview all changes before touching any file |
| **Works anywhere** | Run from any directory — path can be passed as a flag |

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.11+ | `python3 --version` |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| qBittorrent 4.3.9+ | For the `renameFile` API (auto-hook only) |
| TMDB API key | Free at https://www.themoviedb.org/settings/api |

---

## Installation

```bash
git clone https://github.com/WissamZa/jellyfin-anime-renamer.git
cd jellyfin-anime-renamer
uv sync
```

Verify:

```bash
uv run python -c "import pykakasi, bs4, dotenv; print('All deps OK')"
```

---

## Configuration

```bash
cp .env.example .env
# edit .env with your values
```

```dotenv
# ── Required ───────────────────────────────────────────────
TMDB_API_KEY=your_key_here

# ── Series defaults (manual CLI only) ──────────────────────
# These are used when you run jellyfin_renamer.py without --path.
# The qBittorrent hook resolves these automatically from the torrent.
SERIES_NAME=One Piece
TMDB_SERIES_ID=37854
ANILIST_ID=21            # optional — auto-looked up if blank

# ── Paths ───────────────────────────────────────────────────
MEDIA_DIR=/mnt/D/Torrent/One Piece   # default folder for manual CLI
BASE_DOWNLOAD_PATH=/mnt/D/Torrent    # root for qBit hook

# ── Folders ─────────────────────────────────────────────────
ORGANIZE_INTO_FOLDERS=true

# ── qBittorrent ─────────────────────────────────────────────
QBIT_URL=http://localhost:8080
QBIT_USERNAME=your_username
QBIT_PASSWORD=your_password
```

> **Tip:** `TMDB_SERIES_ID` and `ANILIST_ID` are optional when using the qBittorrent hook — they are resolved automatically from the torrent title. They are only needed for the manual CLI.

---

## Manual CLI usage

The CLI can be run **from any directory** — it always finds `.env` and `renamer_core.py` relative to the script itself.

### Interactive menu

```bash
# Use MEDIA_DIR from .env
uv run jellyfin_renamer.py

# Override with a specific folder
uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto
uv run jellyfin_renamer.py -p /mnt/D/Torrent/Naruto
```

```
╔══════════════════════════════════════════════════════╗
║        🏴‍☠️   JELLYFIN ANIME RENAMER  🏴‍☠️              ║
╚══════════════════════════════════════════════════════╝

  📂  Working folder: /mnt/D/Torrent/Naruto

  ┌──────────────────────────────────────┐
  │  1. Dry Run  (preview only)          │
  │  2. Live Rename + Organise           │
  │  3. Undo previous renames            │
  │  4. Show config                      │
  │  5. Exit                             │
  └──────────────────────────────────────┘
```

Always run **option 1** first to preview before committing.

### Non-interactive (scripting / cron)

```bash
# Preview only — prints what would happen, exits
uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --dry-run
uv run jellyfin_renamer.py -p /mnt/D/Torrent/Naruto -d

# Rename immediately — no prompts
uv run jellyfin_renamer.py --path /mnt/D/Torrent/Naruto --run
uv run jellyfin_renamer.py -p /mnt/D/Torrent/Naruto -r
```

### All flags

| Flag | Short | Description |
|---|---|---|
| `--path DIR` | `-p` | Folder to scan (overrides `MEDIA_DIR` in `.env`) |
| `--dry-run` | `-d` | Preview renames, exit without touching files |
| `--run` | `-r` | Rename immediately without the menu |

---

## qBittorrent auto-hook

Every time a torrent finishes, qBittorrent calls `qbit_hook.py` automatically.

### How it works

```
Download finishes
    ↓
qbit_hook.py "%I" "%N" "%F"
    → Nyaa lookup by hash → real title
    → Extract series name
    → TMDB search → series ID
    → AniList + AniDB → romaji name
    → qBit setLocation → /mnt/D/Torrent/<Series>/
    → AnimeRenamer (via qBit renameFile API)
       → renames SxxExx, moves into Season XX/
       → seeding continues uninterrupted
    → renamer.log updated
```

### Setup

**1. Enable qBittorrent Web UI**

Tools → Preferences → Web UI → tick *Enable the Web User Interface*

Set a username and password, note the port (default `8080`).

**2. Get the venv Python path**

```bash
cd /path/to/jellyfin-anime-renamer
uv run which python
# → /home/you/jellyfin-anime-renamer/.venv/bin/python
```

**3. Set the external program**

Tools → Preferences → Downloads → *Run external program on torrent completion*:

```
/home/you/jellyfin-anime-renamer/.venv/bin/python \
  /home/you/jellyfin-anime-renamer/qbit_hook.py "%I" "%N" "%F"
```

| Placeholder | Meaning |
|---|---|
| `%I` | Info-hash (used to look up on Nyaa) |
| `%N` | Torrent name (fallback title) |
| `%F` | Content path on disk |

**4. Test before relying on it**

```bash
uv run python test_hook.py
```

This validates every step — env vars, qBit login, Nyaa lookup, TMDB search, romaji resolution, `renameFile` endpoint, and a dry-run preview — and prints PASS / FAIL for each.

Once all steps pass, trigger manually:

```bash
uv run python qbit_hook.py "<hash>" "<torrent name>"
tail -f renamer.log
```

### Batch / manual trigger

```bash
# Process the most recently completed torrent
uv run python qbit_hook.py

# Process the last N completed torrents
uv run python qbit_hook.py -n 3
```

---

## Romaji title resolution

Titles are resolved in priority order:

```
1. AniList  title.romaji   — human-curated, highest quality
2. AniDB    x-jat dump     — broad coverage, no API key needed
3. pykakasi conversion     — mechanical fallback, always works
```

The AniDB title dump (~3 MB) is downloaded once and cached locally, refreshed automatically every 7 days.

Example:

| Japanese | Resolved romaji |
|---|---|
| 進撃の巨人 | Shingeki no Kyojin |
| 鬼滅の刃 | Kimetsu no Yaiba |
| ワンピース | One Piece |
| お隣の天使様 | Otonari no Tenshi-sama ni… |

---

## Undo

Every rename is recorded in `rename_history.json` inside the media folder.

```bash
# Interactive menu → option 3
uv run jellyfin_renamer.py --path /mnt/D/Torrent/One\ Piece

# Or with the qBit hook's history (same mechanism)
```

Files are moved back and entries removed from the log as each is restored.

> Do not delete `rename_history.json` if you want to be able to undo.

---

## Output structure

```
/mnt/D/Torrent/
└── Otonari no Tenshi-sama ni Itsunomanika Dame Ningen ni Sareteita Ken/
    ├── Season 01/
    │   └── Otonari no Tenshi-sama - S01E01 - Chiisana Yakusoku.mkv
    ├── Season 02/
    │   └── Otonari no Tenshi-sama - S02E08 - ...mkv
    ├── Specials/
    │   └── Otonari no Tenshi-sama - S00E01 - ...mkv
    └── rename_history.json
```

---

## Project structure

```
jellyfin-anime-renamer/
├── renamer_core.py          # all shared logic
├── jellyfin_renamer.py      # interactive + scripting CLI
├── qbit_hook.py             # qBittorrent completion hook
├── qbit_delete_hook.py      # (optional) cleanup on torrent delete
├── test_hook.py             # step-by-step diagnostic
├── title_case_particles.json# particles kept lowercase in romaji titles
├── anidb_titles.dat.gz      # AniDB dump cache (auto-downloaded)
├── pyproject.toml
├── .env                     # your secrets — never commit
├── .env.example             # template
├── .gitignore
└── README.md
```

---

## Supported extensions

`.mp4` `.mkv` `.avi` `.m4v` `.flv` `.webm`

---

## Development

```bash
uv sync --dev
uv run ruff check .
uv run ruff check . --fix
uv run pyright jellyfin_renamer.py renamer_core.py qbit_hook.py
```

---

## License

MIT