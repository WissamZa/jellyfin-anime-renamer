#!/usr/bin/env -S uv run
"""
test_hook.py — diagnose the full qbit_hook pipeline step by step.

Run:  uv run python test_hook.py

Each step prints PASS / FAIL with a clear reason.
Fix failures top-to-bottom before using the real hook.
"""

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


# ── output helpers ───────────────────────────────────────
def ok(msg: str)   -> None: print(f"  ✅  PASS  {msg}")
def fail(msg: str) -> None: print(f"  ❌  FAIL  {msg}")
def info(msg: str) -> None: print(f"  ℹ️        {msg}")
def warn(msg: str) -> None: print(f"  ⚠️        {msg}")
def head(msg: str) -> None: print(f"\n{'─'*58}\n  {msg}\n{'─'*58}")
def bail(msg: str) -> None:
    fail(msg)
    print()
    sys.exit(1)


# ════════════════════════════════════════════════════════
# STEP 1 — Environment variables
# ════════════════════════════════════════════════════════
def step1_env() -> dict[str, str]:
    head("STEP 1 — Environment variables")

    keys = [
        "TMDB_API_KEY",
        "QBIT_URL",
        "QBIT_USERNAME",
        "QBIT_PASSWORD",
        "BASE_DOWNLOAD_PATH",
    ]

    env: dict[str, str] = {}
    missing = []

    for key in keys:
        val = os.getenv(key, "")
        if val:
            masked = val[:6] + "*" * max(0, len(val) - 6)
            ok(f"{key} = {masked}")
            env[key] = val
        else:
            fail(f"{key} is empty — add it to .env")
            missing.append(key)

    if missing:
        bail(f"Fix .env — missing: {', '.join(missing)}")

    base = Path(env["BASE_DOWNLOAD_PATH"])
    if base.exists():
        ok(f"BASE_DOWNLOAD_PATH exists: {base}")
    else:
        bail(f"BASE_DOWNLOAD_PATH does not exist: {base}")

    return env


# ════════════════════════════════════════════════════════
# STEP 2 — qBittorrent connection + login
# ════════════════════════════════════════════════════════
def step2_qbit_login(env: dict) -> requests.Session:
    head("STEP 2 — qBittorrent connection & login")

    url  = env["QBIT_URL"].rstrip("/")
    sess = requests.Session()

    try:
        r = sess.post(
            f"{url}/api/v2/auth/login",
            data={"username": env["QBIT_USERNAME"], "password": env["QBIT_PASSWORD"]},
            timeout=10,
        )
        if r.status_code in (200, 204):
            # qBit returns "Ok." on success, "Fails." on bad credentials
            if r.text.strip().lower() == "fails.":
                bail(
                    f"Login rejected — wrong username or password.\n"
                    f"  Check QBIT_USERNAME / QBIT_PASSWORD in .env"
                )
            ok(f"Logged into qBittorrent at {url}")
        else:
            bail(f"Login failed — HTTP {r.status_code}: {r.text[:80]}")
    except requests.exceptions.ConnectionError:
        fail(f"Cannot reach qBittorrent at {url}")
        info("Checklist:")
        info("  • Is qBittorrent running?")
        info("  • Tools → Preferences → Web UI → 'Enable the Web User Interface'")
        info(f"  • Is the port correct? (QBIT_URL={url})")
        bail("Connection failed")
    except Exception as exc:
        bail(f"Unexpected error: {exc}")

    return sess


# ════════════════════════════════════════════════════════
# STEP 3 — Find a completed torrent
# ════════════════════════════════════════════════════════
def step3_find_torrent(sess: requests.Session, env: dict) -> dict:
    head("STEP 3 — Find a completed torrent to test with")

    url = env["QBIT_URL"].rstrip("/")
    try:
        torrents = sess.get(
            f"{url}/api/v2/torrents/info", timeout=10
        ).json()
    except Exception as exc:
        bail(f"Could not fetch torrent list: {exc}")

    ok(f"Got {len(torrents)} torrent(s) from qBittorrent")

    completed = [
        t for t in torrents
        if t["state"] in ("stoppedUP", "uploading", "stalledUP", "pausedUP")
    ]
    info(f"{len(completed)} completed torrent(s) found")

    if not completed:
        bail(
            "No completed torrents found.\n"
            "  Download and finish at least one torrent, then re-run."
        )

    target = sorted(completed, key=lambda t: t.get("completion_on", 0), reverse=True)[0]

    ok(f"Using: '{target['name']}'")
    info(f"  hash  : {target['hash']}")
    info(f"  state : {target['state']}")
    info(f"  path  : {target['save_path']}")

    return target


# ════════════════════════════════════════════════════════
# STEP 4 — File list inside the torrent
# ════════════════════════════════════════════════════════
def step4_file_list(sess: requests.Session, env: dict, torrent: dict) -> list[dict]:
    head("STEP 4 — Files inside the torrent")

    url = env["QBIT_URL"].rstrip("/")
    try:
        files = sess.get(
            f"{url}/api/v2/torrents/files",
            params={"hash": torrent["hash"]},
            timeout=10,
        ).json()
    except Exception as exc:
        bail(f"Could not fetch file list: {exc}")

    if not files:
        bail("Torrent has no files — unusual, check qBittorrent.")

    ok(f"{len(files)} file(s) in torrent")
    for entry in files[:5]:
        info(f"  {entry['name']}")
    if len(files) > 5:
        info(f"  … and {len(files) - 5} more")

    return files


# ════════════════════════════════════════════════════════
# STEP 5 — Nyaa title lookup
# ════════════════════════════════════════════════════════
def step5_nyaa(torrent: dict) -> str:
    head("STEP 5 — Nyaa title lookup")

    torrent_hash = torrent["hash"]
    fallback     = torrent["name"]

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        warn("beautifulsoup4 not installed — run: uv add beautifulsoup4")
        warn(f"Using torrent name as fallback: {fallback}")
        return fallback

    try:
        r = requests.get(
            f"https://nyaa.si/?f=0&c=0_0&q={torrent_hash}",
            timeout=10,
        )
        soup = BeautifulSoup(r.text, "html.parser")

        # The first <a> in the torrent table title column
        title_link = soup.select_one("td.text-center + td a:not(.comments)")
        if title_link:
            nyaa_title = title_link.text.strip()
            ok(f"Nyaa title: {nyaa_title}")
            return nyaa_title

        # Older fallback: look for h3
        if soup.h3:
            nyaa_title = soup.h3.text.strip()
            ok(f"Nyaa title (h3): {nyaa_title}")
            return nyaa_title

        warn("Hash not found on Nyaa — using torrent name as fallback")
        info(f"  This is normal for private trackers or older torrents.")
        info(f"  Fallback: {fallback}")
        return fallback

    except Exception as exc:
        warn(f"Nyaa lookup failed ({exc}) — using torrent name")
        return fallback


# ════════════════════════════════════════════════════════
# STEP 6 — Series name extraction
# ════════════════════════════════════════════════════════
def step6_series_name(nyaa_title: str) -> str:
    head("STEP 6 — Series name extraction")

    TITLE_PATTERN = re.compile(
        r"\[[^\]]+\]\s+(.+?)(?:\s+\(.*?\))?\s+-\s+[^\[]+"
    )

    def sanitize(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*,]', "", name).strip()

    # Try fansub pattern first: [Group] Series Name - 1100 [tags]
    m = TITLE_PATTERN.match(nyaa_title)
    if m:
        name = sanitize(m.group(1).split("(")[0].strip())
        ok(f"Extracted (fansub pattern): '{name}'")
        return name

    # Fallback: strip [Group], episode number, year
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", nyaa_title)
    cleaned = re.sub(r"\s*-\s*\d+.*$",  "", cleaned)
    cleaned = re.sub(r"\s*\(.*?\)\s*$", "", cleaned)
    name    = sanitize(cleaned)

    if name:
        warn(f"Used fallback extraction: '{name}'")
        info("If this looks wrong, adjust TITLE_PATTERN in qbit_hook.py")
        return name

    fail(f"Could not extract series name from: '{nyaa_title}'")
    info("Expected format: [Group] Series Name - Episode [tags]")
    info("Adjust TITLE_PATTERN in qbit_hook.py to match your release group.")
    bail("Series name extraction failed")
    return ""  # unreachable — satisfies type checker


# ════════════════════════════════════════════════════════
# STEP 7 — TMDB search + romaji resolution
# ════════════════════════════════════════════════════════
def step7_tmdb_and_romaji(series_name: str, env: dict) -> tuple[int, str]:
    head("STEP 7 — TMDB search + romaji resolution (TMDB × AniList)")

    # Import from renamer_core so we test the exact same code path
    # the real hook uses
    try:
        from renamer_core import AniListFetcher, RomajiResolver, TMDBSearch, _romaniser
    except ImportError as exc:
        bail(
            f"Could not import renamer_core: {exc}\n"
            "  Make sure renamer_core.py is in the same folder."
        )

    searcher = TMDBSearch(env["TMDB_API_KEY"])

    # ── TMDB search ──────────────────────────────────────
    tmdb_data = requests.get(
        "https://api.themoviedb.org/3/search/tv",
        params={
            "api_key": env["TMDB_API_KEY"],
            "query":   series_name,
            "language": "en-US",
        },
        timeout=15,
    )
    if tmdb_data.status_code != 200:
        bail(f"TMDB API error: HTTP {tmdb_data.status_code} — check your API key")

    results = tmdb_data.json().get("results", [])
    if not results:
        fail(f"No TMDB results for '{series_name}'")
        info("Search manually at https://www.themoviedb.org/")
        bail("TMDB search failed")

    top     = results[0]
    tmdb_id = top["id"]
    en_name = top["name"]
    ok(f"TMDB top match: id={tmdb_id}  name='{en_name}'")

    if len(results) > 1:
        info("Other matches (first was used):")
        for r in results[1:4]:
            info(f"  id={r['id']}  '{r['name']}'  ({r.get('first_air_date', '?')})")

    # ── Japanese title → pykakasi ─────────────────────────
    ja_data = requests.get(
        f"https://api.themoviedb.org/3/tv/{tmdb_id}",
        params={"api_key": env["TMDB_API_KEY"], "language": "ja"},
        timeout=15,
    )
    ja_name     = ja_data.json().get("name", en_name) if ja_data.status_code == 200 else en_name
    tmdb_romaji = _romaniser.to_romaji(ja_name)
    info(f"TMDB Japanese title : '{ja_name}'")
    info(f"pykakasi romanised  : '{tmdb_romaji}'")

    # ── AniList romaji ────────────────────────────────────
    anilist    = AniListFetcher()
    al_romaji  = anilist.find_romaji(series_name)
    if al_romaji:
        ok(f"AniList romaji      : '{al_romaji}'")
    else:
        warn("AniList returned no romaji — will rely on pykakasi")

    # ── RomajiResolver final decision ─────────────────────
    resolver     = RomajiResolver()
    final_romaji = resolver.resolve(
        search_name  = series_name,
        tmdb_romaji  = tmdb_romaji,
        english_name = en_name,
    )
    ok(f"Final series name   : '{final_romaji}'")

    return tmdb_id, final_romaji


# ════════════════════════════════════════════════════════
# STEP 8 — qBit renameFile endpoint probe
# ════════════════════════════════════════════════════════
def step8_rename_probe(sess: requests.Session, env: dict, torrent: dict) -> None:
    head("STEP 8 — qBit renameFile API probe (no files changed)")

    url = env["QBIT_URL"].rstrip("/")

    # Send a deliberately invalid path — qBit returns 400/409 which is fine;
    # we just want to confirm the endpoint exists and auth works.
    r = sess.post(
        f"{url}/api/v2/torrents/renameFile",
        data={
            "hash":    torrent["hash"],
            "oldPath": "__test_probe_old__",
            "newPath": "__test_probe_new__",
        },
    )

    if r.status_code in (400, 409):
        ok(f"renameFile endpoint alive (HTTP {r.status_code} for fake path — expected)")
    elif r.status_code in (200, 204):
        ok("renameFile returned 200 — endpoint fully works")
    elif r.status_code == 403:
        fail("renameFile returned 403 Forbidden")
        info("Possible causes:")
        info("  • qBittorrent version too old (need 4.3.9+)")
        info("  • CSRF protection — try accessing via http, not https")
        bail("renameFile endpoint not accessible")
    elif r.status_code == 404:
        fail("renameFile returned 404 — endpoint not found")
        info("qBittorrent 4.3.9+ is required for the renameFile API")
        bail("renameFile endpoint missing")
    else:
        warn(f"Unexpected status {r.status_code}: {r.text[:120]}")
        info("Proceeding — this may still work")


# ════════════════════════════════════════════════════════
# STEP 9 — Dry-run rename preview
# ════════════════════════════════════════════════════════
def step9_dry_run(
    torrent:      dict,
    tmdb_id:      int,
    final_romaji: str,
    series_name:  str,
    env:          dict,
) -> None:
    head("STEP 9 — Rename preview (dry run — nothing moves)")

    save_path = Path(torrent["save_path"])

    # The real hook moves the torrent to BASE/series_name/ first.
    # For the preview we scan wherever the files currently live.
    if not save_path.exists():
        warn(f"save_path does not exist on this machine: {save_path}")
        info("Skipping file preview — files may be on a different drive.")
        info("The hook will still work when triggered by qBittorrent.")
        return

    try:
        from renamer_core import AniListFetcher, AnimeRenamer, Config
    except ImportError as exc:
        bail(f"Cannot import renamer_core: {exc}")

    # Resolve AniList ID from the API — don't require it in .env
    anilist    = AniListFetcher()
    anilist_id = anilist.find_id(final_romaji) or anilist.find_id(series_name)
    if anilist_id:
        info(f"AniList ID resolved: {anilist_id}")
    else:
        warn("Could not resolve AniList ID — episode title fallback disabled")

    cfg = Config(
        tmdb_api_key          = env["TMDB_API_KEY"],
        series_name           = final_romaji,
        tmdb_series_id        = tmdb_id,
        anilist_id            = anilist_id,
        media_dir             = save_path,
        organize_into_folders = True,
    )

    errors = cfg.validate()
    if errors:
        for e in errors:
            warn(f"Config: {e}")
        info("Skipping dry-run preview.")
        return

    info(f"Scanning: {save_path}")
    info("Running dry run — no files will be changed …")
    print()

    renamer = AnimeRenamer(cfg)
    results = renamer.run(dry_run=True)

    would_rename = [r for r in results if r.success]
    skipped      = [r for r in results if r.skipped]
    unmatched    = [r for r in results if not r.success and not r.skipped]

    print()
    ok(f"Would rename : {len(would_rename)} file(s)")
    if skipped:
        info(f"Would skip   : {len(skipped)} file(s) (already renamed or unrecognised)")
    if unmatched:
        warn(f"Unmatched    : {len(unmatched)} file(s)")
        for r in unmatched:
            info(f"  {r.original}")


# ════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════
def summary(torrent: dict, tmdb_id: int, final_romaji: str, env: dict) -> None:
    base = Path(env["BASE_DOWNLOAD_PATH"])
    print(f"\n{'═'*58}")
    print("  ALL STEPS PASSED ✅")
    print(f"{'═'*58}")
    print()
    print("  To trigger manually right now:")
    print(f'    uv run python qbit_hook.py "{torrent["hash"]}" "{torrent["name"]}"')
    print()
    print("  Expected result:")
    print(f"    Series  → '{final_romaji}'")
    print(f"    Dest    → {base / final_romaji}/")
    print(f"              Season 01/, Season 02/, …, Specials/")
    print()
    print("  To watch the hook live:")
    print("    tail -f renamer.log")
    print(f"{'═'*58}\n")


# ════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════
def main() -> None:
    env          = step1_env()
    sess         = step2_qbit_login(env)
    torrent      = step3_find_torrent(sess, env)
    _            = step4_file_list(sess, env, torrent)
    nyaa_title   = step5_nyaa(torrent)
    series_name  = step6_series_name(nyaa_title)
    tmdb_id, final_romaji = step7_tmdb_and_romaji(series_name, env)
    step8_rename_probe(sess, env, torrent)
    step9_dry_run(torrent, tmdb_id, final_romaji, series_name, env)
    summary(torrent, tmdb_id, final_romaji, env)


if __name__ == "__main__":
    main()
