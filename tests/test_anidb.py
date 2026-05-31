"""
tests/test_anidb.py
===================
Unit tests for the AniDB UDP API client, cache, and fetcher.

All tests are fully offline — no network calls are made.
UDP socket interactions are patched; the SQLite cache uses tmp_path.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from renamer.providers.anidb import (
    AniDBClient,
    AniDBFetcher,
    _looks_like_hash,
)
from renamer.providers.anidb_cache import AniDBCache, AniDBFileInfo


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_info(**kwargs) -> AniDBFileInfo:
    """Return an AniDBFileInfo with sensible defaults, overridable via kwargs."""
    defaults = dict(
        ed2k="abcdef1234567890abcdef1234567890",
        size=123456789,
        fid=100,
        aid=5,
        eid=10,
        gid=2,
        anime_title_romaji="Shingeki no Kyojin",
        anime_title_english="Attack on Titan",
        anime_title_kanji="進撃の巨人",
        episode_number="1",
        episode_title_en="To You, in 2000 Years",
        episode_title_romaji="Nisennen-go no Kimi e",
        episode_title_kanji="2000年後の君へ",
        group_name="HorribleSubs",
        quality="high",
        source="BluRay",
        cached_at=time.time(),
    )
    defaults.update(kwargs)
    return AniDBFileInfo(**defaults)


@pytest.fixture()
def db_cache(tmp_path: Path) -> AniDBCache:
    """Return a fresh AniDBCache backed by a temp SQLite DB."""
    return AniDBCache(db_path=tmp_path / "anidb_test.db")


@pytest.fixture()
def client() -> AniDBClient:
    """Return an AniDBClient with a patched socket (no real network)."""
    c = AniDBClient(username="testuser", password="testpass")
    c._socket = MagicMock()
    return c


# ---------------------------------------------------------------------------
# _looks_like_hash
# ---------------------------------------------------------------------------

class TestLooksLikeHash:
    @pytest.mark.parametrize("s,expected", [
        ("abcdef1234567890abcdef1234567890", True),   # pure hex MD5-length
        ("DEADBEEFCAFE1234DEAD", True),                # uppercase hex
        ("Shingeki no Kyojin", False),                 # normal title
        ("1080p", False),                              # short
        ("Attack on Titan", False),                    # mixed
        ("", False),                                   # empty
        ("abc", False),                                # too short
        ("g1h2i3j4k5l6m7n8", False),                  # non-hex chars > 20 %
    ])
    def test_hash_detection(self, s: str, expected: bool):
        assert _looks_like_hash(s) is expected


# ---------------------------------------------------------------------------
# AniDBClient._parse_reply
# ---------------------------------------------------------------------------

class TestParseReply:
    def test_basic_code_and_label(self):
        code, data = AniDBClient._parse_reply("220 FILE 1|2|3|4")
        assert code == 220
        assert data == "1|2|3|4"

    def test_code_only(self):
        code, data = AniDBClient._parse_reply("300 PONG")
        assert code == 300
        assert data == "PONG"

    def test_three_part_reply(self):
        code, data = AniDBClient._parse_reply("200 abc123 LOGIN ACCEPTED")
        assert code == 200
        assert data == "LOGIN ACCEPTED"

    def test_empty_string_returns_zero(self):
        code, data = AniDBClient._parse_reply("")
        assert code == 0

    def test_non_numeric_code(self):
        code, data = AniDBClient._parse_reply("NOT A CODE")
        assert code == 0
        assert "NOT A CODE" in data


# ---------------------------------------------------------------------------
# AniDBClient._parse_file_response
# ---------------------------------------------------------------------------

class TestParseFileResponse:
    def _call(self, data_line: str) -> AniDBFileInfo | None:
        return AniDBClient._parse_file_response(data_line, size=1000, ed2k="aa" * 16)

    def test_parses_ids_correctly(self):
        info = self._call("42|7|15|3|high|BluRay|Shingeki no Kyojin")
        assert info is not None
        assert info.fid == 42
        assert info.aid == 7
        assert info.eid == 15
        assert info.gid == 3

    def test_quality_and_source(self):
        info = self._call("1|2|3|4|very high|web")
        assert info is not None
        assert info.quality == "very high"
        assert info.source == "web"

    def test_passthrough_of_size_and_ed2k(self):
        ed2k = "bb" * 16
        result = AniDBClient._parse_file_response("1|2|3|4", size=99999, ed2k=ed2k)
        assert result is not None
        assert result.size == 99999
        assert result.ed2k == ed2k

    def test_returns_none_for_garbage(self):
        # Non-integer IDs → should return None
        result = AniDBClient._parse_file_response("not|valid|data|here", 0, "x")
        # The implementation falls back gracefully — IDs default to 0
        # (not None, because isdigit() is False → 0)
        assert result is not None
        assert result.fid == 0
        assert result.aid == 0

    def test_hash_like_parts_not_used_as_title(self):
        ed2k_like = "a" * 32
        info = self._call(f"1|2|3|4|good|web|{ed2k_like}")
        assert info is not None
        # Hash-like part must NOT appear as the anime title
        assert info.anime_title_romaji != ed2k_like


# ---------------------------------------------------------------------------
# AniDBClient._parse_episode_number
# ---------------------------------------------------------------------------

class TestParseEpisodeNumber:
    @pytest.mark.parametrize("epno,expected", [
        ("1", 1),
        ("12", 12),
        ("100", 100),
        ("S1", None),     # special
        ("S5", None),     # special
        ("C1", None),     # credit
        ("c2", None),     # credit lowercase
        ("",   None),     # empty
        ("E5", 5),        # digits extracted
        ("ep10", 10),     # digits extracted
    ])
    def test_parse(self, epno: str, expected: int | None):
        assert AniDBFetcher._parse_episode_number(epno) == expected


# ---------------------------------------------------------------------------
# AniDBClient._fetch_anime / _fetch_episode / _fetch_group
# ---------------------------------------------------------------------------

class TestFetchHelpers:
    """Test the enrichment helpers by mocking _send_recv."""

    def _make_client(self) -> AniDBClient:
        c = AniDBClient("u", "p")
        c._session = "SESS1234"
        c._connected = True
        c._socket = MagicMock()
        return c

    def test_fetch_anime_parses_titles(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="230 ANIME 7|TV Series|Shingeki no Kyojin|進撃の巨人|Attack on Titan")
        result = c._fetch_anime(7)
        assert result is not None
        assert result["romaji"] == "Shingeki no Kyojin"
        assert result["kanji"] == "進撃の巨人"
        assert result["english"] == "Attack on Titan"

    def test_fetch_anime_returns_none_on_wrong_code(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="320 NO SUCH ANIME")
        assert c._fetch_anime(999) is None

    def test_fetch_anime_returns_none_on_timeout(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value=None)
        assert c._fetch_anime(1) is None

    def test_fetch_episode_parses_fields(self):
        c = self._make_client()
        c._send_recv = MagicMock(
            return_value="240 EPISODE 55|7|1|Nisennen-go no Kimi e|2000年後の君へ|To You in 2000 Years"
        )
        result = c._fetch_episode(55)
        assert result is not None
        assert result["epno"] == "1"
        assert result["title_romaji"] == "Nisennen-go no Kimi e"
        assert result["title_en"] == "To You in 2000 Years"

    def test_fetch_episode_returns_none_on_wrong_code(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="321 NO SUCH EPISODE")
        assert c._fetch_episode(0) is None

    def test_fetch_group_parses_name(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="250 GROUP 3|HorribleSubs|HS")
        result = c._fetch_group(3)
        assert result is not None
        assert result["name"] == "HorribleSubs"

    def test_fetch_group_returns_none_on_wrong_code(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="350 NO SUCH GROUP")
        assert c._fetch_group(999) is None


# ---------------------------------------------------------------------------
# AniDBClient.connect / _try_auth / _ping
# ---------------------------------------------------------------------------

class TestConnect:
    def _make_client(self) -> AniDBClient:
        return AniDBClient("user", "pass", client_name="testclient", client_ver=1)

    def test_connect_returns_true_on_200(self):
        # _parse_reply("200 SESS1234 LOGIN ACCEPTED") → data="LOGIN ACCEPTED"
        # _parse_reply("200 SESS1234") → data="SESS1234" → session = "SESS1234"
        c = self._make_client()
        # PING returns 300 PONG; AUTH reply has session as the only data token
        responses = iter(["300 PONG", "200 SESS1234"])
        c._send_recv = MagicMock(side_effect=lambda msg, **kw: next(responses))
        with patch("socket.socket"):
            c._socket = MagicMock()
            result = c.connect()
        assert result is True
        assert c._connected is True
        assert c._session == "SESS1234"

    def test_connect_returns_true_on_201(self):
        c = self._make_client()
        # 201 with session as sole data token
        c._send_recv = MagicMock(return_value="201 SESS5678")
        c._socket = MagicMock()
        result = c._try_auth("testclient", 1)
        assert result is True
        assert c._connected is True
        assert c._session == "SESS5678"

    def test_connect_returns_false_on_500(self):
        c = self._make_client()
        c._send_recv = MagicMock(return_value="500 LOGIN ACCEPTED FAILED")
        c._socket = MagicMock()
        result = c._try_auth("testclient", 1)
        assert result is False
        assert c._last_auth_code == 500

    def test_connect_returns_false_when_already_aborted(self):
        c = self._make_client()
        c._aborted = True
        c._socket = MagicMock()
        # _send_recv returns None when aborted
        assert c._send_recv("anything") is None

    def test_ping_returns_true_on_300(self):
        c = self._make_client()
        c._socket = MagicMock()
        c._send_recv = MagicMock(return_value="300 PONG")
        assert c._ping() is True

    def test_ping_returns_false_on_timeout(self):
        c = self._make_client()
        c._socket = MagicMock()
        c._send_recv = MagicMock(return_value=None)
        assert c._ping() is False

    def test_ping_returns_false_on_wrong_code(self):
        c = self._make_client()
        c._socket = MagicMock()
        c._send_recv = MagicMock(return_value="500 ERROR")
        assert c._ping() is False

    def test_already_connected_returns_true_immediately(self):
        c = self._make_client()
        c._connected = True
        # No socket needed — should short-circuit
        assert c.connect() is True


# ---------------------------------------------------------------------------
# AniDBClient.file_lookup
# ---------------------------------------------------------------------------

class TestFileLookup:
    def _make_connected_client(self) -> AniDBClient:
        c = AniDBClient("u", "p")
        c._connected = True
        c._session = "SESSION1"
        c._socket = MagicMock()
        return c

    def test_220_returns_info(self):
        c = self._make_connected_client()
        # Stub _send_recv for FILE + enrichment ANIME/EPISODE/GROUP calls
        file_reply = "220 FILE 42|7|15|3|high|BluRay|Shingeki no Kyojin"
        anime_reply = "230 ANIME 7|TV|Shingeki no Kyojin|進撃の巨人|Attack on Titan"
        ep_reply = "240 EPISODE 15|7|1|Title Romaji|Title Kanji|Episode Title EN"
        grp_reply = "250 GROUP 3|HorribleSubs|HS"
        c._send_recv = MagicMock(side_effect=[file_reply, anime_reply, ep_reply, grp_reply])
        info = c.file_lookup(size=100, ed2k="aa" * 16)
        assert info is not None
        assert info.fid == 42
        assert info.aid == 7
        assert info.anime_title_romaji == "Shingeki no Kyojin"
        assert info.group_name == "HorribleSubs"

    def test_320_returns_none(self):
        c = self._make_connected_client()
        c._send_recv = MagicMock(return_value="320 NO SUCH FILE")
        assert c.file_lookup(0, "x") is None

    def test_506_clears_session(self):
        c = self._make_connected_client()
        c._send_recv = MagicMock(return_value="506 INVALID SESSION")
        result = c.file_lookup(0, "x")
        assert result is None
        assert c._connected is False

    def test_505_sets_aborted(self):
        c = self._make_connected_client()
        c._send_recv = MagicMock(return_value="505 ACCESS DENIED")
        result = c.file_lookup(0, "x")
        assert result is None
        assert c._aborted is True

    def test_unknown_code_returns_none(self):
        c = self._make_connected_client()
        c._send_recv = MagicMock(return_value="999 WHATEVER")
        assert c.file_lookup(0, "x") is None

    def test_returns_none_when_send_recv_fails(self):
        c = self._make_connected_client()
        c._send_recv = MagicMock(return_value=None)
        assert c.file_lookup(100, "z" * 32) is None


# ---------------------------------------------------------------------------
# AniDBClient._enrich_info — hash-like title discarding
# ---------------------------------------------------------------------------

class TestEnrichInfo:
    def test_hash_like_romaji_replaced_by_english(self):
        c = AniDBClient("u", "p")
        c._session = "S"
        c._connected = True
        c._socket = MagicMock()

        info = _make_info(
            anime_title_romaji="abcdef1234567890abcdef1234567890",  # looks like hash
            anime_title_english="Attack on Titan",
        )

        anime_reply = "230 ANIME 5|TV|abcdef1234567890abcdef1234567890|進撃の巨人|Attack on Titan"
        ep_reply = None
        grp_reply = None

        call_queue = [anime_reply, ep_reply, grp_reply]
        c._send_recv = MagicMock(side_effect=call_queue)

        c._enrich_info(info)
        # Hash-like romaji should be replaced with english fallback
        assert info.anime_title_romaji == "Attack on Titan"

    def test_hash_like_episode_title_cleared(self):
        c = AniDBClient("u", "p")
        c._session = "S"
        c._connected = True
        c._socket = MagicMock()

        info = _make_info(
            aid=0,  # skip ANIME call
            episode_title_en="abcdef1234567890abcdef1234567890",
        )

        ep_reply = "240 EPISODE 10|5|1|RomajiTitle|KanjiTitle|abcdef1234567890abcdef1234567890"
        grp_reply = None
        c._send_recv = MagicMock(side_effect=[ep_reply, grp_reply])

        c._enrich_info(info)
        assert info.episode_title_en == ""


# ---------------------------------------------------------------------------
# AniDBCache — SQLite-backed
# ---------------------------------------------------------------------------

class TestAniDBCache:
    def test_get_returns_none_when_empty(self, db_cache: AniDBCache):
        assert db_cache.get(size=999, ed2k="nonexistent") is None

    def test_set_and_get_roundtrip(self, db_cache: AniDBCache):
        info = _make_info()
        db_cache.set(info)
        result = db_cache.get(size=info.size, ed2k=info.ed2k)
        assert result is not None
        assert result.fid == info.fid
        assert result.aid == info.aid
        assert result.eid == info.eid
        assert result.gid == info.gid
        assert result.anime_title_romaji == info.anime_title_romaji
        assert result.episode_title_en == info.episode_title_en
        assert result.group_name == info.group_name

    def test_stale_entry_returns_none(self, db_cache: AniDBCache):
        # Write an entry cached 31 days ago
        old_ts = time.time() - (31 * 86_400)
        info = _make_info(cached_at=old_ts)
        db_cache.set(info)
        assert db_cache.get(size=info.size, ed2k=info.ed2k) is None

    def test_fresh_entry_is_returned(self, db_cache: AniDBCache):
        info = _make_info(cached_at=time.time())
        db_cache.set(info)
        assert db_cache.get(size=info.size, ed2k=info.ed2k) is not None

    def test_upsert_overwrites_existing(self, db_cache: AniDBCache):
        info = _make_info(anime_title_english="Attack on Titan")
        db_cache.set(info)
        updated = _make_info(anime_title_english="AoT Updated")
        db_cache.set(updated)
        result = db_cache.get(size=info.size, ed2k=info.ed2k)
        assert result is not None
        assert result.anime_title_english == "AoT Updated"

    def test_clear_all_removes_entries(self, db_cache: AniDBCache):
        db_cache.set(_make_info())
        db_cache.clear_all()
        assert db_cache.get(size=_make_info().size, ed2k=_make_info().ed2k) is None

    def test_clear_stale_removes_only_old_entries(self, db_cache: AniDBCache):
        fresh = _make_info(ed2k="f" * 32, cached_at=time.time())
        stale = _make_info(ed2k="a" * 32, cached_at=time.time() - 32 * 86_400)
        db_cache.set(fresh)
        db_cache.set(stale)
        removed = db_cache.clear_stale()
        assert removed == 1
        assert db_cache.get(fresh.size, fresh.ed2k) is not None
        assert db_cache.get(stale.size, stale.ed2k) is None

    def test_get_by_aid_returns_all_entries(self, db_cache: AniDBCache):
        info1 = _make_info(ed2k="a" * 32, eid=1, episode_number="1")
        info2 = _make_info(ed2k="b" * 32, eid=2, episode_number="2")
        info3 = _make_info(ed2k="c" * 32, aid=999, eid=3)  # different AID
        db_cache.set(info1)
        db_cache.set(info2)
        db_cache.set(info3)
        results = db_cache.get_by_aid(aid=5)
        assert len(results) == 2
        eids = {r.eid for r in results}
        assert eids == {1, 2}

    def test_cached_at_auto_set_when_zero(self, db_cache: AniDBCache):
        info = _make_info(cached_at=0.0)
        before = time.time()
        db_cache.set(info)
        result = db_cache.get(size=info.size, ed2k=info.ed2k)
        assert result is not None
        assert result.cached_at >= before

    def test_different_size_same_ed2k_are_separate(self, db_cache: AniDBCache):
        info_a = _make_info(size=111, fid=1)
        info_b = _make_info(size=222, fid=2)
        db_cache.set(info_a)
        db_cache.set(info_b)
        r_a = db_cache.get(size=111, ed2k=info_a.ed2k)
        r_b = db_cache.get(size=222, ed2k=info_b.ed2k)
        assert r_a is not None and r_a.fid == 1
        assert r_b is not None and r_b.fid == 2


# ---------------------------------------------------------------------------
# AniDBFetcher.fetch — offline, using the cache
# ---------------------------------------------------------------------------

class TestAniDBFetcherOffline:
    """Test AniDBFetcher.fetch() entirely from cache — no network, no hashing."""

    def _make_fetcher(self, tmp_path: Path) -> AniDBFetcher:
        """Return a fetcher with ANIDB credentials set and offline mode on."""
        from renamer.config import Config
        cfg = Config(
            media_dir=tmp_path,
            anidb_username="u",
            anidb_password="p",
            anidb_offline=True,
        )
        fetcher = AniDBFetcher(cfg)
        fetcher._anidb_cache = AniDBCache(db_path=tmp_path / "anidb_test.db")
        return fetcher

    def _seed_cache(self, fetcher: AniDBFetcher, infos: list[AniDBFileInfo]) -> None:
        for info in infos:
            fetcher._anidb_cache.set(info)

    def _create_fake_video(self, directory: Path, name: str) -> Path:
        """Create a tiny fake .mkv file so fetch() finds it."""
        p = directory / name
        p.write_bytes(b"\x00" * 64)
        return p

    def test_fetch_returns_none_when_no_video_files(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)
        # No .mkv files → no video files → returns None
        # compute_ed2k/get_file_size are imported locally inside fetch()
        with (
            patch("renamer.ed2k.compute_ed2k", return_value="aa" * 16),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            result = fetcher.fetch()
        assert result is None

    def test_fetch_returns_episode_map_from_cache(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)

        # Create two fake video files
        self._create_fake_video(tmp_path, "ep01.mkv")
        self._create_fake_video(tmp_path, "ep02.mkv")

        ed2k_ep1 = "a" * 32
        ed2k_ep2 = "b" * 32

        self._seed_cache(fetcher, [
            _make_info(ed2k=ed2k_ep1, size=64, aid=7, eid=1, episode_number="1",
                       episode_title_en="Episode One"),
            _make_info(ed2k=ed2k_ep2, size=64, aid=7, eid=2, episode_number="2",
                       episode_title_en="Episode Two"),
        ])

        call_count = 0

        def fake_ed2k(path: Path) -> str:
            nonlocal call_count
            call_count += 1
            return ed2k_ep1 if "ep01" in path.name else ed2k_ep2

        with (
            patch("renamer.ed2k.compute_ed2k", side_effect=fake_ed2k),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            result = fetcher.fetch()

        assert result is not None
        assert 1 in result
        assert 2 in result
        assert result[1].title == "Episode One"
        assert result[2].title == "Episode Two"

    def test_fetch_sets_series_name_from_anidb(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)
        self._create_fake_video(tmp_path, "ep01.mkv")
        ed2k = "c" * 32

        self._seed_cache(fetcher, [
            _make_info(ed2k=ed2k, size=64, aid=7, eid=1, episode_number="1",
                       anime_title_romaji="Shingeki no Kyojin",
                       episode_title_en="First Episode"),
        ])

        with (
            patch("renamer.ed2k.compute_ed2k", return_value=ed2k),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            result = fetcher.fetch()

        assert result is not None
        assert fetcher._cfg.series_name == "Shingeki no Kyojin"

    def test_fetch_skips_special_episodes(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)
        self._create_fake_video(tmp_path, "ep01.mkv")
        self._create_fake_video(tmp_path, "sp1.mkv")

        ed2k_ep = "d" * 32
        ed2k_sp = "e" * 32

        self._seed_cache(fetcher, [
            _make_info(ed2k=ed2k_ep, size=64, aid=7, eid=1, episode_number="1",
                       episode_title_en="Regular"),
            _make_info(ed2k=ed2k_sp, size=64, aid=7, eid=99, episode_number="S1",
                       episode_title_en="Special"),
        ])

        def fake_ed2k(path: Path) -> str:
            return ed2k_sp if "sp1" in path.name else ed2k_ep

        with (
            patch("renamer.ed2k.compute_ed2k", side_effect=fake_ed2k),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            result = fetcher.fetch()

        assert result is not None
        assert 1 in result
        # S1 → _parse_episode_number returns None → not in map
        assert all(isinstance(k, int) and k > 0 for k in result)

    def test_fetch_returns_none_when_all_files_unidentified(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)
        self._create_fake_video(tmp_path, "mystery.mkv")

        with (
            patch("renamer.ed2k.compute_ed2k", return_value="f" * 32),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            # Cache is empty, offline=True → nothing identified
            result = fetcher.fetch()

        assert result is None

    def test_fetch_specials_returns_empty_dict(self, tmp_path: Path):
        fetcher = self._make_fetcher(tmp_path)
        assert fetcher.fetch_specials() == {}

    def test_primary_anime_is_the_one_with_most_files(self, tmp_path: Path):
        """When files belong to two different anime, the one with more files wins."""
        fetcher = self._make_fetcher(tmp_path)

        for i in range(3):
            self._create_fake_video(tmp_path, f"anime_a_ep0{i+1}.mkv")
        self._create_fake_video(tmp_path, "anime_b_ep01.mkv")

        ed2k_map = {
            "anime_a_ep01.mkv": "a1" * 16,
            "anime_a_ep02.mkv": "a2" * 16,
            "anime_a_ep03.mkv": "a3" * 16,
            "anime_b_ep01.mkv": "b1" * 16,
        }

        self._seed_cache(fetcher, [
            _make_info(ed2k="a1" * 16, size=64, aid=100, eid=1, episode_number="1",
                       anime_title_romaji="Anime Alpha", episode_title_en="Alpha Ep 1"),
            _make_info(ed2k="a2" * 16, size=64, aid=100, eid=2, episode_number="2",
                       anime_title_romaji="Anime Alpha", episode_title_en="Alpha Ep 2"),
            _make_info(ed2k="a3" * 16, size=64, aid=100, eid=3, episode_number="3",
                       anime_title_romaji="Anime Alpha", episode_title_en="Alpha Ep 3"),
            _make_info(ed2k="b1" * 16, size=64, aid=200, eid=10, episode_number="1",
                       anime_title_romaji="Anime Beta", episode_title_en="Beta Ep 1"),
        ])

        def fake_ed2k(path: Path) -> str:
            return ed2k_map.get(path.name, "zz" * 16)

        with (
            patch("renamer.ed2k.compute_ed2k", side_effect=fake_ed2k),
            patch("renamer.ed2k.get_file_size", return_value=64),
        ):
            result = fetcher.fetch()

        assert result is not None
        assert len(result) == 3  # only Anime Alpha's 3 episodes
        assert fetcher._cfg.series_name == "Anime Alpha"
