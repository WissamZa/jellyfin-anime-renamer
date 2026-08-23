"""
renamer.providers.anidb
=======================
AniDB UDP API client for hash-based file identification.

This provider computes the ED2K hash of video files and queries AniDB's
UDP API (api.anidb.net:9000) to identify them.  The FILE command
accepts (size, ed2k) and returns the anime title, episode number,
episode title, and fansub group — providing 100% accurate file
identification regardless of filename quality.

Protocol notes
--------------
* AniDB requires authentication (username + password from a free account).
* The UDP API uses a session-based protocol: AUTH → commands → LOGOUT.
* Throttling: burst of 5 packets, then 1 packet per 2.5 seconds.
* Optional AES-128 encryption when ANIDB_API_KEY is set.
* Client identification: each client must register a name + version with AniDB.

AniDB Error 505 Fix
--------------------
Error 505 "ILLEGAL INPUT OR ACCESS DENIED" is commonly caused by:
  1. Client name not registered at AniDB — register at
     https://wiki.anidb.net/UDP_API_Definition#Client_Registering
  2. IP temporarily banned from too many auth attempts — wait 30 min
  3. Invalid auth parameters

The client will:
  - Try a PING first to verify connectivity
  - Retry AUTH with an incremented client version (sometimes stale ver = 505)
  - Fall back gracefully so the filename-based resolver can still work

References
----------
* AniDB UDP API Definition: https://wiki.anidb.net/UDP_API_Definition
* ED2K hash: https://wiki.anidb.net/Ed2k-hash
"""

from __future__ import annotations

import hashlib
import random
import socket
import time
from typing import TYPE_CHECKING

from renamer.config import Config, get_logger
from renamer.providers.anidb_cache import AniDBCache, AniDBFileInfo
from renamer.providers.base import EpisodeFetcher, EpisodeInfo

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)

# AniDB UDP API constants
ANIDB_HOST = "api.anidb.net"
ANIDB_PORT = 9000
ANIDB_TIMEOUT = 30  # seconds per UDP round-trip

# Throttling: AniDB allows burst of 5, then 1 per 2.5s
BURST_SIZE = 5
PACKET_INTERVAL = 2.5

# FILE command fmask/amask for requesting data fields
# fmask=79FAFFE900: fid, aid, eid, gid, quality, source, video/audio codec, resolution, etc.
# amask=F2FCF0C0: anime romaji/english/kanji titles, episode count, year, type
FILE_FMASK = "79FAFFE900"
FILE_AMASK = "F2FCF0C0"

# Maximum AUTH attempts per connect() — repeated AUTH failures trigger a
# 30-minute IP ban at AniDB, so never hammer the endpoint.
_MAX_AUTH_ATTEMPTS = 3


def _looks_like_hash(s: str) -> bool:
    """True if the string looks like a hex hash — do not use as a title/group name."""
    if len(s) < 8:
        return False
    hex_chars = sum(1 for c in s if c in "0123456789abcdefABCDEF")
    return hex_chars / len(s) > 0.8


class AniDBClient:
    """
    AniDB UDP API client for file identification.

    Parameters
    ----------
    username : str
        AniDB account username (free registration at anidb.net).
    password : str
        AniDB account password.
    client_name : str
        Registered client name (e.g. "jenameramer").
    client_ver : int
        Client version number.
    api_key : str or None
        Optional API key for AES-128 encryption of the session.
    """

    def __init__(
        self,
        username: str,
        password: str,
        client_name: str = "jenameramer",
        client_ver: int = 1,
        api_key: str | None = None,
        use_encryption: bool = True,
    ) -> None:
        self._username = username
        self._password = password
        self._client_name = client_name
        self._client_ver = client_ver
        self._api_key = api_key
        self._use_encryption = use_encryption and bool(api_key)
        self._session: str = ""
        self._aes_key: bytes | None = None
        self._socket: socket.socket | None = None
        self._burst_counter = 0
        self._last_send_time = 0.0
        self._connected = False
        self._aborted = False

    # ── Connection lifecycle ──────────────────────────────────

    def connect(self) -> bool:
        """
        Open a UDP socket and authenticate with AniDB.

        Strategy (kept deliberately short — repeated AUTH failures earn a
        30-minute IP ban at AniDB):
          1. PING the server to verify connectivity
          2. AUTH with the configured client name/version
          3. On 503/505, retry a few times with incremented versions
             (stale client version is the usual cause)

        Returns True if authentication succeeded, False otherwise.
        """
        if self._connected:
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(ANIDB_TIMEOUT)
            self._burst_counter = 0

            # Step 0: PING the server to verify connectivity
            print("  AniDB: testing connectivity … ", end="", flush=True)
            ping_ok = self._ping()
            if not ping_ok:
                print("server unreachable")
                log.error("AniDB: PING failed — server unreachable")
                return False
            print("OK")

            # Step 1: Try AUTH with configured client name/version
            success = self._try_auth(self._client_name, self._client_ver)
            if success:
                return True

            # Step 2: On 503/505, retry with incremented versions (capped —
            # hammering AUTH triggers a 30-minute IP ban)
            attempts = 1
            while (
                attempts < _MAX_AUTH_ATTEMPTS
                and self._last_auth_code in (503, 505)
            ):
                self._client_ver += 1
                attempts += 1
                log.info("AniDB: retrying AUTH with client_ver=%d …", self._client_ver)
                success = self._try_auth(self._client_name, self._client_ver)
                if success:
                    return True

            if self._last_auth_code in (504, 505):
                print(f"\n  AniDB rejected client '{self._client_name}' "
                      f"(code {self._last_auth_code}).")
                print("  Client names must be registered at AniDB before use — see")
                print("  https://wiki.anidb.net/UDP_API_Definition#Client_Registering")
                print("  Set ANIDB_CLIENT / ANIDB_CLIENT_VER in .env to a name you")
                print("  registered, or use PROVIDER=tmdb/anilist/kitsu instead.")

            return False

        except TimeoutError:
            log.error("AniDB: connection timed out")
            return False
        except OSError as exc:
            log.error("AniDB: socket error: %s", exc)
            return False

    def _ping(self) -> bool:
        """Send a PING to verify AniDB server is reachable."""
        try:
            reply = self._send_recv("PING")
            if reply is None:
                return False
            code, _ = self._parse_reply(reply)
            return code == 300  # 300 PONG
        except Exception:
            return False

    _last_auth_code: int = 0

    def _try_auth(self, client_name: str, client_ver: int) -> bool:
        """
        Try AUTH with the given client name and version.

        Returns True if auth succeeded, False otherwise.
        Sets self._last_auth_code to the response code.
        """
        msg = (
            f"AUTH user={self._username}&password={self._password}"
            f"&protover=3&client={client_name}&clientver={client_ver}"
            f"&enc=UTF-8"
        )
        reply = self._send_recv(msg)

        if reply is None:
            log.error("AniDB: no response to AUTH")
            self._last_auth_code = 0
            return False

        code, data = self._parse_reply(reply)
        self._last_auth_code = code

        if code == 200:
            # 200 {session_key} LOGIN ACCEPTED
            self._session = self._parse_session(reply)
            self._connected = True
            if not self._session:
                log.error("AniDB: AUTH accepted but no session key in reply")
                self._connected = False
                return False
            if self._use_encryption:
                self._setup_encryption()
            log.info(
                "AniDB: authenticated as %s (client=%s v%d, session=%s)",
                self._username,
                client_name,
                client_ver,
                self._session[:4] + "...",
            )
            return True

        if code == 201:
            # 201 {session_key} LOGIN ACCEPTED — NEW VERSION AVAILABLE
            self._session = self._parse_session(reply)
            self._connected = True
            if not self._session:
                log.error("AniDB: AUTH accepted but no session key in reply")
                self._connected = False
                return False
            if self._use_encryption:
                self._setup_encryption()
            log.info(
                "AniDB: authenticated (new version available, client=%s v%d)",
                client_name,
                client_ver,
            )
            return True

        if code == 500:
            log.error("AniDB: login failed — invalid username/password")
            print("\n  AniDB: Login failed — check ANIDB_USERNAME and ANIDB_PASSWORD in .env")
        elif code == 501:
            log.error("AniDB: login failed — account temporarily blocked (too many attempts)")
            print("\n  AniDB: Account temporarily blocked — wait 30 minutes and try again")
        elif code == 503:
            log.error("AniDB: client version outdated — will try higher version")
            print(f"\n  AniDB: Client version outdated (tried {client_name} v{client_ver})")
        elif code == 504:
            log.error("AniDB: client not registered — will try fallback")
            print(f"\n  AniDB: Client '{client_name}' not registered at AniDB")
        elif code == 505:
            log.error("AniDB: ACCESS DENIED (code 505)")
            print(f"\n  AniDB: ACCESS DENIED (code 505) with client '{client_name}' v{client_ver}")
        else:
            log.error("AniDB: AUTH failed with code %d: %s", code, data)
            print(f"\n  AniDB: AUTH failed with code {code}.")

        return False

    def disconnect(self) -> None:
        """Send LOGOUT and close the socket."""
        if self._connected and self._session:
            import contextlib

            with contextlib.suppress(Exception):  # Best-effort logout
                self._send_recv(f"LOGOUT s={self._session}", expect_reply=True)

        if self._socket:
            self._socket.close()
            self._socket = None

        self._connected = False
        self._session = ""
        self._aes_key = None

    # ── File lookup ───────────────────────────────────────────

    def file_lookup(self, size: int, ed2k: str) -> AniDBFileInfo | None:
        """
        Look up a file by (size, ed2k) on AniDB.

        Returns AniDBFileInfo if the file is found, None otherwise.
        Also makes ANIME, EPISODE, and GROUP enrichment calls.
        """
        if not self._connected and not self.connect():
            return None

        if not self._session:
            log.error("AniDB: no session — AUTH failed or session was lost")
            return None

        # FILE command
        msg = (
            f"FILE size={size}&ed2k={ed2k}&fmask={FILE_FMASK}&amask={FILE_AMASK}&s={self._session}"
        )
        reply = self._send_recv(msg)

        if reply is None:
            return None

        code, data = self._parse_reply(reply)

        if code == 220:
            # 220 FILE — file found
            info = self._parse_file_response(data, size, ed2k)

            if info is None:
                return None

            # Enrich with anime title, episode title, and group name
            self._enrich_info(info)
            return info

        if code == 320:
            # 320 NO SUCH FILE
            log.info("AniDB: no file found for size=%d ed2k=%s", size, ed2k[:8])
            return None

        if code == 322:
            # 322 MULTIPLE FILES FOUND
            log.warning("AniDB: multiple files match size=%d ed2k=%s — using first", size, ed2k[:8])
            info = self._parse_file_response(data, size, ed2k)
            if info:
                self._enrich_info(info)
            return info

        if code == 505:
            # 505 ILLEGAL INPUT / ILLEGAL ACCESS
            log.error("AniDB: illegal input (session may have expired)")
            self._connected = False
            self._aborted = True
            return None

        if code == 506:
            # 506 INVALID SESSION
            log.warning("AniDB: session expired — will re-authenticate on next call")
            self._connected = False
            return None

        log.warning("AniDB: unexpected FILE response code %d: %s", code, data)
        return None

    # ── Enrichment calls ──────────────────────────────────────

    def _enrich_info(self, info: AniDBFileInfo) -> None:
        """
        Make ANIME, EPISODE, and GROUP calls to fill in human-readable
        titles for the file identified by the FILE command.
        """
        # ANIME command — get anime titles
        if info.aid:
            anime_data = self._fetch_anime(info.aid)
            if anime_data:
                info.anime_title_romaji = anime_data.get("romaji", info.anime_title_romaji)
                info.anime_title_english = anime_data.get("english", info.anime_title_english)
                info.anime_title_kanji = anime_data.get("kanji", info.anime_title_kanji)

                # Discard hash-like garbage titles
                if _looks_like_hash(info.anime_title_romaji):
                    info.anime_title_romaji = info.anime_title_english or ""
                if _looks_like_hash(info.anime_title_english):
                    info.anime_title_english = info.anime_title_romaji or ""

        # EPISODE command — get episode title
        if info.eid:
            ep_data = self._fetch_episode(info.eid)
            if ep_data:
                info.episode_number = ep_data.get("epno", info.episode_number)
                info.episode_title_en = ep_data.get("title_en", info.episode_title_en)
                info.episode_title_romaji = ep_data.get("title_romaji", info.episode_title_romaji)
                info.episode_title_kanji = ep_data.get("title_kanji", info.episode_title_kanji)

                # Discard hash-like episode titles
                if _looks_like_hash(info.episode_title_en):
                    info.episode_title_en = ""
                if _looks_like_hash(info.episode_title_romaji):
                    info.episode_title_romaji = ""

        # GROUP command — get fansub group name
        if info.gid:
            group_data = self._fetch_group(info.gid)
            if group_data:
                info.group_name = group_data.get("name", info.group_name)

    def _fetch_anime(self, aid: int) -> dict[str, str] | None:
        """Fetch anime metadata via ANIME command."""
        if not self._session:
            log.debug("AniDB: skipping ANIME — no session")
            return None
        msg = f"ANIME aid={aid}&s={self._session}"
        reply = self._send_recv(msg)
        if reply is None:
            return None

        code, data = self._parse_reply(reply)
        if code != 230:
            log.debug("AniDB: ANIME aid=%d returned code %d", aid, code)
            return None

        # Parse: 230 ANIME aid|type|romaji|kanji|english|...
        parts = data.split("|")
        result: dict[str, str] = {}
        if len(parts) >= 3:
            result["romaji"] = parts[2].strip()
        if len(parts) >= 4:
            result["kanji"] = parts[3].strip()
        if len(parts) >= 5:
            result["english"] = parts[4].strip()
        return result

    def _fetch_episode(self, eid: int) -> dict[str, str] | None:
        """Fetch episode metadata via EPISODE command."""
        if not self._session:
            log.debug("AniDB: skipping EPISODE — no session")
            return None
        msg = f"EPISODE eid={eid}&s={self._session}"
        reply = self._send_recv(msg)
        if reply is None:
            return None

        code, data = self._parse_reply(reply)
        if code != 240:
            log.debug("AniDB: EPISODE eid=%d returned code %d", eid, code)
            return None

        # Parse: 240 EPISODE eid|aid|epno|romaji|kanji|english|...
        parts = data.split("|")
        result: dict[str, str] = {}
        if len(parts) >= 3:
            result["epno"] = parts[2].strip()
        if len(parts) >= 4:
            result["title_romaji"] = parts[3].strip()
        if len(parts) >= 5:
            result["title_kanji"] = parts[4].strip()
        if len(parts) >= 6:
            result["title_en"] = parts[5].strip()
        return result

    def _fetch_group(self, gid: int) -> dict[str, str] | None:
        """Fetch group metadata via GROUP command."""
        if not self._session:
            log.debug("AniDB: skipping GROUP — no session")
            return None
        msg = f"GROUP gid={gid}&s={self._session}"
        reply = self._send_recv(msg)
        if reply is None:
            return None

        code, data = self._parse_reply(reply)
        if code != 250:
            log.debug("AniDB: GROUP gid=%d returned code %d", gid, code)
            return None

        # Parse: 250 GROUP gid|name|shortname|...
        parts = data.split("|")
        result: dict[str, str] = {}
        if len(parts) >= 2:
            result["name"] = parts[1].strip()
        return result

    # ── Low-level UDP protocol ────────────────────────────────

    def _send_recv(self, message: str, expect_reply: bool = True) -> str | None:
        """
        Send a UDP message to AniDB and return the response string.

        Implements throttling: burst of 5 packets, then 1 per 2.5s.
        Payloads are AES-128-CBC encrypted once the session is encrypted.
        """
        if self._aborted:
            return None

        if self._socket is None:
            return None

        # Throttle
        self._throttle()

        try:
            payload = message.encode("utf-8")
            if self._aes_key is not None:
                payload = self._encrypt_payload(payload)

            self._socket.sendto(payload, (ANIDB_HOST, ANIDB_PORT))
            self._last_send_time = time.time()
            self._burst_counter += 1

            if not expect_reply:
                return None

            data, _ = self._socket.recvfrom(16384)
            raw = bytes(data)
            if self._aes_key is not None:
                raw = self._decrypt_payload(raw)
            return raw.decode("utf-8", errors="replace").strip()

        except TimeoutError:
            log.warning("AniDB: UDP request timed out")
            return None
        except OSError as exc:
            log.error("AniDB: socket error: %s", exc)
            return None

    def _throttle(self) -> None:
        """Enforce AniDB rate limit: burst of 5, then 1 per 2.5s."""
        now = time.time()
        if now - self._last_send_time > PACKET_INTERVAL:
            self._burst_counter = 0  # idle longer than the interval — window expired
        if self._burst_counter < BURST_SIZE:
            return

        elapsed = now - self._last_send_time
        if elapsed < PACKET_INTERVAL:
            wait = PACKET_INTERVAL - elapsed + random.uniform(0, 0.3)
            log.debug("AniDB: throttling — waiting %.1fs", wait)
            time.sleep(wait)

    @staticmethod
    def _parse_session(reply: str) -> str:
        """
        Extract the session key from a 200/201 AUTH reply.

        Format: ``200 {session_key} LOGIN ACCEPTED`` — the session key is
        the token immediately after the numeric code.  Note that
        ``_parse_reply`` deliberately drops this token from its data field,
        so callers must use this method for auth replies.
        """
        parts = reply.split(" ", 2)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _parse_reply(reply: str) -> tuple[int, str]:
        """
        Parse an AniDB reply into (code, data).

        Format: ``CODE LABEL data_line`` — e.g. ``220 FILE aid|eid|…``.
        For 200/201 auth replies the second token is the *session key*,
        which must be read via :meth:`_parse_session` instead.
        """
        parts = reply.split(" ", 2)
        if not parts:
            return (0, "")
        try:
            code = int(parts[0])
        except ValueError:
            return (0, reply)
        data = parts[1] if len(parts) > 1 else ""
        # Strip the text label (e.g. "220 FILE" → data after "FILE ")
        if len(parts) > 2:
            data = parts[2]
        return (code, data)

    @staticmethod
    def _parse_file_response(data_line: str, size: int, ed2k: str) -> AniDBFileInfo | None:
        """
        Parse the data line from a 220 FILE response.

        The response is pipe-delimited fields controlled by fmask/amask.
        """
        parts = [p.strip() for p in data_line.split("|")]

        try:
            fid = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
            aid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            eid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            gid = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        except (ValueError, IndexError):
            log.warning("AniDB: could not parse FILE response IDs")
            return None

        # Extract quality/source from appropriate positions (depends on fmask)
        quality = parts[4] if len(parts) > 4 else ""
        source = parts[5] if len(parts) > 5 else ""

        # Anime-level titles come after file-level fields (depends on amask)
        # The exact positions depend on the fmask/amask combination.
        # With our fmask/amask, we expect anime titles later in the response.
        anime_romaji = ""
        anime_english = ""
        anime_kanji = ""

        # Try to extract anime titles from the response
        # This is a best-effort parse; enrichment calls will fill in properly
        for _i, part in enumerate(parts):
            if not part or _looks_like_hash(part):
                continue
            # Heuristic: longer strings that don't look like codes are titles
            if len(part) > 5 and not part.isdigit():
                if not anime_romaji:
                    anime_romaji = part
                elif not anime_english and part != anime_romaji:
                    anime_english = part
                elif not anime_kanji and part != anime_romaji and part != anime_english:
                    anime_kanji = part

        return AniDBFileInfo(
            ed2k=ed2k,
            size=size,
            fid=fid,
            aid=aid,
            eid=eid,
            gid=gid,
            anime_title_romaji=anime_romaji,
            anime_title_english=anime_english,
            anime_title_kanji=anime_kanji,
            quality=quality,
            source=source,
            cached_at=time.time(),
        )

    # ── Encryption support ────────────────────────────────────

    def _setup_encryption(self) -> bool:
        """
        Upgrade the freshly-authenticated session to AES-128 encryption.

        Per the AniDB UDP API spec this is done *after* a successful
        plaintext AUTH with an ENCRYPT command:

          1. Send ``ENCRYPT user={username}&type=1`` (plaintext)
          2. Reply: ``ENCRYPTED {salt}`` (4-char salt)
          3. AES key = md5(api_key + salt)

        On any failure the session stays plaintext and we log a warning —
        an unusable encrypted session is worse than a working plain one.
        """
        if not self._api_key or self._aes_key is not None:
            return self._aes_key is not None

        try:
            reply = self._send_recv(f"ENCRYPT user={self._username}&type=1")
            if reply is None:
                log.warning("AniDB: no response to ENCRYPT — staying plaintext")
                return False

            # Reply format: "ENCRYPTED {salt}" or "3xx ENCRYPTED {salt}"
            parts = reply.split()
            salt = ""
            for i, token in enumerate(parts):
                if token == "ENCRYPTED" and i + 1 < len(parts):
                    salt = parts[i + 1]
                    break
            if not salt:
                log.warning(
                    "AniDB: unexpected ENCRYPT reply (%r) — staying plaintext",
                    reply[:40],
                )
                return False

            self._aes_key = self._derive_aes_key(salt)
            log.info("AniDB: session encryption enabled (salt=%s)", salt)
            return True
        except Exception as exc:
            log.warning("AniDB: encryption handshake failed (%s) — staying plaintext", exc)
            self._aes_key = None
            return False

    def _derive_aes_key(self, salt: str) -> bytes:
        """
        Derive the AES-128 session key from the API key + salt (AniDB spec).

        NOTE: the wiki formula is md5(key + salt); whether AniDB expects the
        profile *API key* or the account *password* must be validated on a
        live run — offline tests can only assert the derivation math.
        """
        raw = (self._api_key or "") + salt
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).digest()

    def _encrypt_payload(self, payload: bytes) -> bytes:
        """AES-128-CBC encrypt a packet; AniDB pads with null bytes to 16 B."""
        from Crypto.Cipher import AES  # pycryptodome

        pad_len = (-len(payload)) % 16
        padded = payload + b"\x00" * pad_len
        cipher = AES.new(self._aes_key, AES.MODE_CBC, iv=b"\x00" * 16)
        return cipher.encrypt(padded)

    def _decrypt_payload(self, data: bytes) -> bytes:
        """AES-128-CBC decrypt a received packet and strip null padding."""
        from Crypto.Cipher import AES  # pycryptodome

        cipher = AES.new(self._aes_key, AES.MODE_CBC, iv=b"\x00" * 16)
        return cipher.decrypt(data).rstrip(b"\x00")


class AniDBFetcher(EpisodeFetcher):
    """
    AniDB-based episode fetcher using ED2K hash file identification.

    This fetcher works differently from the others:
    - Instead of searching by series name, it identifies each file by hash
      (AniDB is a hash-only provider — no name-search factory exists).
    - It first computes ED2K hashes for all video files in the media directory.
    - Then queries AniDB for each hash to get anime + episode metadata.
    - Groups files by anime and builds an episode map.

    Usage
    -----
    Typically used with ``--use-hash`` flag or ``PROVIDER=anidb``.
    Can also be used as a fallback when filename parsing fails.
    """

    name = "AniDB"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self._client: AniDBClient | None = None
        self._anidb_cache: AniDBCache = AniDBCache()

    def _get_client(self) -> AniDBClient | None:
        """Create or return the AniDB client. Returns None if credentials are missing."""
        if self._client is not None:
            return self._client

        username = self._cfg.anidb_username
        password = self._cfg.anidb_password

        if not username or not password:
            log.warning(
                "AniDB credentials not configured. Set ANIDB_USERNAME and ANIDB_PASSWORD in .env"
            )
            return None

        self._client = AniDBClient(
            username=username,
            password=password,
            client_name=self._cfg.anidb_client or "jenameramer",
            client_ver=self._cfg.anidb_client_ver or 1,
            api_key=self._cfg.anidb_api_key or None,
            use_encryption=bool(self._cfg.anidb_api_key),
        )
        return self._client

    def fetch(self) -> dict[int, EpisodeInfo] | None:
        """
        Scan media_dir for video files, compute ED2K hashes, and
        query AniDB for file identification.

        Returns a mapping of absolute-episode-number → EpisodeInfo,
        grouped by the first identified anime.
        """
        from renamer.ed2k import compute_ed2k, get_file_size

        cfg = self._cfg
        media_dir = cfg.media_dir

        if not media_dir.is_dir():
            log.error("AniDB: media_dir does not exist: %s", media_dir)
            return None

        # Collect all video files recursively
        video_files = sorted(
            p
            for p in media_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in cfg.video_extensions
        )

        if not video_files:
            log.info("AniDB: no video files found in %s", media_dir)
            return None

        log.info("AniDB: computing ED2K hashes for %d file(s) …", len(video_files))

        # Phase 1: Compute hashes for all files
        file_hashes: list[tuple[Path, int, str]] = []  # (path, size, ed2k)
        for f in video_files:
            try:
                size = get_file_size(f)
                ed2k = compute_ed2k(f)
                file_hashes.append((f, size, ed2k))
                log.debug("ED2K: %s → %s (size=%d)", f.name, ed2k[:8], size)
            except Exception as exc:
                log.warning("Could not hash %s: %s", f.name, exc)

        if not file_hashes:
            return None

        # Phase 2: Look up each file via cache, then AniDB API
        client = self._get_client()
        offline = cfg.anidb_offline

        # Group files by anime (aid)
        anime_groups: dict[int, list[tuple[Path, int, str, AniDBFileInfo]]] = {}

        for f, size, ed2k in file_hashes:
            # Check cache first
            info = self._anidb_cache.get(size, ed2k)

            if info is None and not offline and client:
                # Cache miss — query AniDB API
                api_info = client.file_lookup(size, ed2k)
                if api_info:
                    self._anidb_cache.set(api_info)
                    info = api_info

            if info and info.aid:
                anime_groups.setdefault(info.aid, []).append((f, size, ed2k, info))
            else:
                log.warning(
                    "AniDB: could not identify %s (ed2k=%s size=%d)",
                    f.name,
                    ed2k[:8],
                    size,
                )

        if client:
            client.disconnect()

        if not anime_groups:
            log.error("AniDB: no files could be identified")
            return None

        # Phase 3: Build episode map from the identified files
        # Use the anime with the most files as the primary series
        primary_aid = max(anime_groups, key=lambda aid: len(anime_groups[aid]))
        primary_files = anime_groups[primary_aid]
        primary_info = primary_files[0][3]  # first file's info for anime title

        log.info(
            "AniDB: identified %d file(s) as '%s' (aid=%d)",
            len(primary_files),
            primary_info.anime_title_romaji or primary_info.anime_title_english or "Unknown",
            primary_aid,
        )

        # Update cfg series name from AniDB data
        series_title = (
            primary_info.anime_title_romaji or primary_info.anime_title_english or cfg.series_name
        )
        if series_title != cfg.series_name:
            log.info("AniDB: updating series name from %r to %r", cfg.series_name, series_title)
            cfg.series_name = series_title

        # Build episode map (regular episodes) and specials map (S-prefixed)
        mapping: dict[int, EpisodeInfo] = {}
        self._last_specials: dict[int, EpisodeInfo] = {}
        for _, _, _, info in primary_files:
            kind, num = self._classify_epno(info.episode_number)
            if num is None:
                continue

            title = info.episode_title_en or info.episode_title_romaji or f"Episode {num}"

            if kind == "special":
                # AniDB "S1" → S00E01-style special entry
                if num not in self._last_specials:
                    self._last_specials[num] = EpisodeInfo(
                        absolute=num,
                        season=0,
                        episode=num,
                        title=title,
                        is_special=True,
                        source=self.name,
                    )
                continue

            # For regular episodes we assign season=1 by default (AniDB doesn't
            # always have season info; cross-referencing with TMDB is better)
            mapping[num] = EpisodeInfo(
                absolute=num,
                season=1,
                episode=num,
                title=title,
                source=self.name,
            )

        log.info(
            "AniDB: mapped %d episode(s) + %d special(s) for '%s'",
            len(mapping),
            len(self._last_specials),
            series_title,
        )
        return mapping

    def fetch_specials(self) -> dict[int, EpisodeInfo]:
        """
        Return specials collected during the last ``fetch()`` call.

        AniDB marks specials with an 'S'-prefixed episode number; they are
        classified out of the regular episode stream and returned here so
        the renamer can file them under Specials (S00Exx).
        """
        return getattr(self, "_last_specials", {})

    @staticmethod
    def _classify_epno(epno: str) -> tuple[str, int | None]:
        """
        Classify an AniDB episode-number field.

        AniDB episode numbers can be:
          - Regular: "1", "2", "13"      → ("regular", n)
          - Special: "S1", "S2"          → ("special", n)
          - Credit: "C1", "C2"           → ("credit", n)
          - Other prefixed: "E5", "P1"   → ("other", n)
          - Unparsable                    → ("other", None)

        Returns ``(kind, number)``.
        """
        import re

        if not epno:
            return ("other", None)
        epno = epno.strip()
        if epno.isdigit():
            return ("regular", int(epno))
        prefix = epno[0].upper()
        digits = epno[1:]
        if digits.isdigit():
            kind = {"S": "special", "C": "credit"}.get(prefix, "other")
            return (kind, int(digits))
        # Fall back to extracting any embedded digit group (e.g. "ep10")
        m = re.search(r"\d+", epno)
        return (("other", int(m.group())) if m else ("other", None))

    @staticmethod
    def _parse_episode_number(epno: str) -> int | None:
        """
        Parse AniDB episode number field for regular-episode streams:
        returns the integer number for plain and unprefixed-fallback forms,
        None for specials ("S1") and credits ("C1").
        """
        kind, num = AniDBFetcher._classify_epno(epno)
        return num if kind in ("regular", "other") else None

    def lookup_single_file(self, file_path: Path) -> AniDBFileInfo | None:
        """
        Look up a single file by ED2K hash. Useful as a fallback
        when filename parsing fails for individual files.

        This is the key integration point: the main renamer can call
        this for any file that couldn't be identified by filename.
        """
        from renamer.ed2k import compute_ed2k, get_file_size

        try:
            size = get_file_size(file_path)
            ed2k = compute_ed2k(file_path)
        except Exception as exc:
            log.warning("Could not hash %s: %s", file_path.name, exc)
            return None

        # Check cache
        info = self._anidb_cache.get(size, ed2k)
        if info:
            log.debug("AniDB cache hit for %s: %s", file_path.name, info.anime_title_romaji)
            return info

        # Query API
        client = self._get_client()
        if client is None:
            return None

        info = client.file_lookup(size, ed2k)
        if info:
            self._anidb_cache.set(info)
            log.info(
                "AniDB identified %s → %s E%s",
                file_path.name,
                info.anime_title_romaji or info.anime_title_english,
                info.episode_number,
            )
        return info
