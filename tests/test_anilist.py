"""Tests for the AniList provider: fetch fallback, GraphQL errors, search mapping."""

from unittest.mock import MagicMock, patch

import pytest

from renamer.providers.anilist import AniListFetcher


@pytest.fixture
def fetcher() -> AniListFetcher:
    return AniListFetcher(anime_id=21)


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_from_streaming_episodes(self, fetcher: AniListFetcher):
        fetcher._gql = MagicMock(
            return_value={
                "episodes": 2,
                "streamingEpisodes": [
                    {"title": "Episode 1 - Romance Dawn"},
                    {"title": "Episode 2 - The Pirate Hunter"},
                ],
            }
        )
        mapping = fetcher.fetch()
        assert mapping is not None
        assert len(mapping) == 2
        assert mapping[1].title == "Romance Dawn"
        assert mapping[2].title == "The Pirate Hunter"
        assert mapping[1].season == 1
        assert mapping[1].episode == 1
        assert mapping[1].source == "AniList"

    def test_fetch_falls_back_to_episode_count(self, fetcher: AniListFetcher):
        """Shows without streamingEpisodes still produce a full episode map."""
        fetcher._gql = MagicMock(return_value={"episodes": 3, "streamingEpisodes": []})
        mapping = fetcher.fetch()
        assert mapping is not None
        assert len(mapping) == 3
        assert mapping[1].title == "Episode 1"
        assert mapping[3].title == "Episode 3"

    def test_fetch_uses_longer_of_count_and_streaming(self, fetcher: AniListFetcher):
        fetcher._gql = MagicMock(
            return_value={"episodes": 5, "streamingEpisodes": [{"title": "Episode 1 - A"}]}
        )
        mapping = fetcher.fetch()
        assert mapping is not None
        assert len(mapping) == 5
        assert mapping[1].title == "A"
        assert mapping[2].title == "Episode 2"  # generic fallback

    def test_fetch_returns_none_when_both_absent(self, fetcher: AniListFetcher):
        fetcher._gql = MagicMock(return_value={"episodes": 0, "streamingEpisodes": []})
        assert fetcher.fetch() is None

    def test_fetch_returns_none_when_media_missing(self, fetcher: AniListFetcher):
        fetcher._gql = MagicMock(return_value=None)
        assert fetcher.fetch() is None

    def test_fetch_returns_none_without_id(self):
        assert AniListFetcher().fetch() is None


# ---------------------------------------------------------------------------
# _gql — GraphQL errors and rate limiting
# ---------------------------------------------------------------------------


class TestGql:
    def test_graphql_errors_payload_does_not_crash(self):
        """HTTP 200 with an errors payload and null data returns None."""
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"errors": [{"message": "Not Found."}], "data": None}
        with patch("renamer.providers.anilist._get_shared_session") as sess:
            sess.return_value.post.return_value = resp
            f = AniListFetcher(anime_id=1)
            assert f._gql(f.EPISODES_QUERY, {"id": 1}) is None

    def test_rate_limit_retries_then_succeeds(self):
        throttled = MagicMock(status_code=429, headers={"Retry-After": "0"})
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"data": {"Media": {"id": 21}}}
        with (
            patch("renamer.providers.anilist._get_shared_session") as sess,
            patch("renamer.providers.anilist.time.sleep") as mock_sleep,
        ):
            sess.return_value.post.side_effect = [throttled, ok]
            f = AniListFetcher()
            result = f._gql(f.SERIES_QUERY, {"search": "one piece"})
            assert result == {"id": 21}
            mock_sleep.assert_called_once()

    def test_http_error_returns_none(self):
        resp = MagicMock(status_code=500)
        resp.json.return_value = {}
        with patch("renamer.providers.anilist._get_shared_session") as sess:
            sess.return_value.post.return_value = resp
            f = AniListFetcher()
            assert f._gql(f.SERIES_QUERY, {"search": "x"}) is None

    def test_default_headers_sent(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"Media": {"id": 1}}}
        with patch("renamer.providers.anilist._get_shared_session") as sess:
            sess.return_value.post.return_value = resp
            f = AniListFetcher()
            f._gql(f.SERIES_QUERY, {"id": 1})
            call_kwargs = sess.return_value.post.call_args[1]
            headers = call_kwargs["headers"]
            assert headers["Origin"] == "https://anilist.co"
            assert headers["Referer"] == "https://anilist.co/"
            assert "User-Agent" in headers
            assert headers["Accept"] == "application/json"

    def test_token_header_sent(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"Media": {"id": 1}}}
        with patch("renamer.providers.anilist._get_shared_session") as sess:
            sess.return_value.post.return_value = resp
            f = AniListFetcher(token="secret_token_123")
            f._gql(f.SERIES_QUERY, {"id": 1})
            call_kwargs = sess.return_value.post.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer secret_token_123"

    def test_page_query_result_returned(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"Page": {"media": [{"id": 123}]}}}
        with patch("renamer.providers.anilist._get_shared_session") as sess:
            sess.return_value.post.return_value = resp
            f = AniListFetcher()
            result = f._gql(f.SEARCH_QUERY, {"search": "test"})
            assert result == {"media": [{"id": 123}]}

    def test_http_403_with_errors_payload_logged(self):
        resp = MagicMock(status_code=403)
        resp.json.return_value = {
            "errors": [
                {
                    "message": "The AniList API has been temporarily disabled due to severe stability issues.",
                    "status": 403,
                }
            ]
        }
        with (
            patch("renamer.providers.anilist._get_shared_session") as sess,
            patch("renamer.providers.anilist.log.warning") as mock_warn,
        ):
            sess.return_value.post.return_value = resp
            f = AniListFetcher()
            assert f._gql(f.SERIES_QUERY, {"search": "test"}) is None
            mock_warn.assert_called()
            # Verify the warning logged the 403 and the error payload message
            warning_msg = mock_warn.call_args[0][0] % mock_warn.call_args[0][1:]
            assert "AniList HTTP 403" in warning_msg
            assert "The AniList API has been temporarily disabled" in warning_msg


# ---------------------------------------------------------------------------
# search / to_search_dicts
# ---------------------------------------------------------------------------

_RAW_SEARCH = [
    {
        "id": 21,
        "title": {"romaji": "One Piece", "english": "One Piece", "native": "ワンピース"},
        "episodes": 1100,
        "format": "TV",
        "season": "FALL",
        "seasonYear": 1999,
        "description": "Gold Roger... <b>King of the Pirates</b>.<br>",
        "countryOfOrigin": "JP",
    },
    {
        "id": 16498,
        "title": {"romaji": "Shingeki no Kyojin", "english": None, "native": "進撃の巨人"},
        "episodes": 25,
        "format": "TV",
        "season": "SPRING",
        "seasonYear": 2013,
        "description": None,
        "countryOfOrigin": "JP",
    },
]


class TestSearchMapping:
    def test_search_returns_raw_media(self):
        with patch.object(AniListFetcher, "_gql") as gql:
            gql.return_value = {"media": _RAW_SEARCH}
            f = AniListFetcher()
            assert f.search("attack", limit=2) == _RAW_SEARCH
            gql.assert_called_once()

    def test_search_returns_empty_list_on_no_data(self):
        with patch.object(AniListFetcher, "_gql") as gql:
            gql.return_value = None
            assert AniListFetcher().search("nothing") == []

    def test_to_search_dicts_shape(self):
        dicts = AniListFetcher.to_search_dicts(_RAW_SEARCH)
        assert dicts[0]["id"] == 21
        assert dicts[0]["name"] == "One Piece"
        assert dicts[0]["original_name"] == "ワンピース"
        assert dicts[0]["romaji"] == "One Piece"
        assert dicts[0]["first_air_date"] == "1999-01-01"
        assert dicts[0]["origin_country"] == ["JP"]
        assert dicts[0]["anilist_id"] == 21
        # HTML stripped from description
        assert "<b>" not in dicts[0]["overview"]
        assert "King of the Pirates" in dicts[0]["overview"]

    def test_to_search_dicts_english_fallback(self):
        dicts = AniListFetcher.to_search_dicts(_RAW_SEARCH)
        assert dicts[1]["name"] == "Shingeki no Kyojin"  # romaji when no english
        assert dicts[1]["first_air_date"] == "2013-01-01"
        assert dicts[1]["overview"] == ""


# ---------------------------------------------------------------------------
# find_romaji / find_id (regression)
# ---------------------------------------------------------------------------


class TestLookups:
    def test_find_id(self):
        with patch.object(AniListFetcher, "_gql") as gql:
            gql.return_value = {"id": 21, "title": {"romaji": "One Piece"}}
            assert AniListFetcher().find_id("one piece") == 21

    def test_find_romaji_returns_romaji(self):
        with patch.object(AniListFetcher, "_gql") as gql:
            gql.return_value = {
                "id": 21,
                "title": {"romaji": "One Piece", "english": "One Piece", "native": "x"},
            }
            f = AniListFetcher()
            assert f.find_romaji("one piece") == "One Piece"
            assert f._id == 21


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_anilist_has_search_factories(self):
        from renamer.config import Config, Provider
        from renamer.providers.registry import get_registry

        registry = get_registry()
        cfg = Config()

        with patch.object(AniListFetcher, "search", return_value=_RAW_SEARCH[:1]):
            result = registry.search(Provider.AniList, "one piece", cfg)
            assert result is not None
            assert result.provider == "anilist"
            assert result.series_id == 21
            assert result.series_name == "One Piece"
            assert result.anilist_id == 21

        with patch.object(AniListFetcher, "search", return_value=_RAW_SEARCH):
            results = registry.search_multi(Provider.AniList, "one", cfg, limit=5)
            assert len(results) == 2
            assert results[0]["id"] == 21
            assert results[0]["romaji"] == "One Piece"
