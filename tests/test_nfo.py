"""
tests.test_nfo
==============
Unit tests for tvshow.nfo generation, XML structure, Romaji title resolution,
and single/batch folder processing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from renamer.config import Config
from renamer.nfo import NfoActor, NfoData, NfoGenerator


@pytest.fixture
def sample_nfo_data(tmp_path: Path) -> NfoData:
    folder = tmp_path / "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"
    folder.mkdir()
    (folder / "folder.jpg").write_text("fake_poster")
    (folder / "backdrop.jpg").write_text("fake_backdrop")

    return NfoData(
        title="Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu",
        originaltitle="Skeleton Knight in Another World",
        plot="One day, a gamer played video games until he fell asleep...",
        outline="One day, a gamer played video games until he fell asleep...",
        lockdata=False,
        dateadded="2026-07-04 17:04:07",
        trailer="plugin://plugin.video.youtube/play/?video_id=dPzd8VNbQQI",
        rating=7,
        year="2022",
        mpaa="TV-14",
        imdb_id="tt14476236",
        tmdb_id=123528,
        tvdb_id=401279,
        anilist_id=132474,
        premiered="2022-04-06",
        releasedate="2022-04-06",
        enddate="2022-06-22",
        runtime=24,
        genres=["Action", "Adventure", "Anime", "Comedy", "Fantasy"],
        studios=["AT-X", "BS11", "Crunchyroll", "Studio KAI", "Tokyo MX"],
        tags=["adventure", "anime", "based on light novel", "Isekai", "Magic"],
        poster_path=str((folder / "folder.jpg").resolve()),
        fanart_path=str((folder / "backdrop.jpg").resolve()),
        actors=[
            NfoActor(
                name="Tomoaki Maeno",
                role="Arc",
                type="Actor",
                thumb="https://image.tmdb.org/t/p/original/test1.jpg",
                sortorder=0,
            ),
            NfoActor(
                name="Fairouz Ai",
                role="Ariane Glenys Maple",
                type="Actor",
                thumb="https://image.tmdb.org/t/p/original/test2.jpg",
                sortorder=1,
            ),
        ],
        status="Ended",
    )


class TestNfoXmlRendering:
    def test_xml_declaration_and_root(self, sample_nfo_data: NfoData):
        gen = NfoGenerator()
        xml_text = gen.render_xml(sample_nfo_data)

        assert xml_text.startswith('<?xml version="1.0" encoding="utf-8" standalone="yes"?>')
        root = ET.fromstring(xml_text)
        assert root.tag == "tvshow"

    def test_xml_structure_matches_template_tags(self, sample_nfo_data: NfoData):
        gen = NfoGenerator()
        xml_text = gen.render_xml(sample_nfo_data)
        root = ET.fromstring(xml_text)

        # Core fields
        assert root.findtext("title") == "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"
        assert root.findtext("originaltitle") == "Skeleton Knight in Another World"
        assert root.findtext("plot").startswith("One day, a gamer played")
        assert root.findtext("outline").startswith("One day, a gamer played")
        assert root.findtext("lockdata") == "false"
        assert root.findtext("dateadded") == "2026-07-04 17:04:07"
        assert root.findtext("trailer") == "plugin://plugin.video.youtube/play/?video_id=dPzd8VNbQQI"
        assert root.findtext("rating") == "7"
        assert root.findtext("year") == "2022"
        assert root.findtext("mpaa") == "TV-14"
        assert root.findtext("imdb_id") == "tt14476236"
        assert root.findtext("tmdbid") == "123528"
        assert root.findtext("tvdbid") == "401279"
        assert root.findtext("anilistid") == "132474"
        assert root.findtext("premiered") == "2022-04-06"
        assert root.findtext("releasedate") == "2022-04-06"
        assert root.findtext("enddate") == "2022-06-22"
        assert root.findtext("runtime") == "24"
        assert root.findtext("id") == "401279"
        assert root.findtext("season") == "-1"
        assert root.findtext("episode") == "-1"
        assert root.findtext("status") == "Ended"

        # Genres
        genres = [g.text for g in root.findall("genre")]
        assert genres == ["Action", "Adventure", "Anime", "Comedy", "Fantasy"]

        # Studios
        studios = [s.text for s in root.findall("studio")]
        assert "AT-X" in studios
        assert "Crunchyroll" in studios

        # Tags
        tags = [t.text for t in root.findall("tag")]
        assert "adventure" in tags
        assert "Isekai" in tags

        # Art
        art = root.find("art")
        assert art is not None
        assert art.findtext("poster").endswith("folder.jpg")
        assert art.findtext("fanart").endswith("backdrop.jpg")

        # Actors
        actors = root.findall("actor")
        assert len(actors) == 2
        assert actors[0].findtext("name") == "Tomoaki Maeno"
        assert actors[0].findtext("role") == "Arc"
        assert actors[0].findtext("type") == "Actor"
        assert actors[0].findtext("sortorder") == "0"
        assert actors[0].findtext("thumb") == "https://image.tmdb.org/t/p/original/test1.jpg"

        # Episode guide
        eg = root.find("episodeguide")
        assert eg is not None
        assert "401279" in eg.find("url").attrib["cache"]


class TestNfoGeneratorProcessing:
    @patch.object(NfoGenerator, "fetch_tmdb_show")
    @patch.object(NfoGenerator, "fetch_tmdb_japanese_name")
    @patch.object(NfoGenerator, "fetch_anilist_details")
    def test_build_nfo_data_resolves_romaji_title(
        self,
        mock_al: MagicMock,
        mock_ja: MagicMock,
        mock_show: MagicMock,
        tmp_path: Path,
    ):
        mock_show.return_value = {
            "name": "Skeleton Knight in Another World",
            "original_name": "骸骨騎士様、只今異世界へお出掛け中",
            "overview": "Synopsis here",
            "vote_average": 7.3,
            "first_air_date": "2022-04-06",
            "last_air_date": "2022-06-22",
            "episode_run_time": [24],
            "genres": [{"name": "Action & Adventure"}, {"name": "Sci-Fi & Fantasy"}],
            "networks": [{"name": "AT-X"}],
            "production_companies": [{"name": "Studio KAI"}],
            "keywords": {"results": [{"name": "isekai"}]},
            "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-14"}]},
            "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "dPzd8VNbQQI"}]},
            "external_ids": {"imdb_id": "tt14476236", "tvdb_id": 401279},
            "credits": {
                "cast": [
                    {
                        "name": "Tomoaki Maeno",
                        "character": "Arc",
                        "profile_path": "/test.jpg",
                        "order": 0,
                    }
                ]
            },
            "status": "Ended",
        }
        mock_ja.return_value = "骸骨騎士様、只今異世界へお出掛け中"
        mock_al.return_value = {
            "id": 132474,
            "title": {"romaji": "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"},
            "genres": ["Action", "Adventure", "Fantasy"],
            "tags": [{"name": "Skeleton", "isMediaSpoiler": False}],
        }

        folder = tmp_path / "Skeleton Knight"
        folder.mkdir()

        gen = NfoGenerator(Config(tmdb_api_key="dummy_key"))
        with patch("renamer.providers.romaji_resolver.RomajiResolver.resolve") as mock_resolve:
            mock_resolve.return_value = "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"
            data = gen.build_nfo_data(123528, folder=folder)

        assert data is not None
        assert data.title == "Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu"
        assert data.originaltitle == "Skeleton Knight in Another World"
        assert data.anilist_id == 132474
        assert data.tmdb_id == 123528
        assert data.tvdb_id == 401279
        assert "Anime" in data.genres
        assert "Action" in data.genres
        assert "Skeleton" in data.tags
        assert "isekai" in data.tags

    def test_write_nfo_creates_file(self, tmp_path: Path, sample_nfo_data: NfoData):
        folder = tmp_path / "Anime Show"
        folder.mkdir()

        gen = NfoGenerator()
        nfo_file = gen.write_nfo(folder, sample_nfo_data, overwrite=True)

        assert nfo_file is not None
        assert nfo_file.is_file()
        assert nfo_file.name == "tvshow.nfo"
        content = nfo_file.read_text(encoding="utf-8")
        assert "<title>Gaikotsu Kishi-sama, Tadaima Isekai e Odekakechuu</title>" in content

    def test_write_nfo_respects_no_overwrite(self, tmp_path: Path, sample_nfo_data: NfoData):
        folder = tmp_path / "Anime Show"
        folder.mkdir()
        existing = folder / "tvshow.nfo"
        existing.write_text("existing content", encoding="utf-8")

        gen = NfoGenerator()
        gen.write_nfo(folder, sample_nfo_data, overwrite=False)

        assert existing.read_text(encoding="utf-8") == "existing content"

    @patch.object(NfoGenerator, "build_nfo_data")
    def test_process_folder(self, mock_build: MagicMock, tmp_path: Path, sample_nfo_data: NfoData):
        folder = tmp_path / "Gaikotsu Kishi"
        folder.mkdir()
        (folder / "ep01.mkv").write_text("video")

        mock_build.return_value = sample_nfo_data
        gen = NfoGenerator()
        result = gen.process_folder(folder, tmdb_id=123528, overwrite=True)

        assert result is not None
        assert result.is_file()
        assert (folder / "tvshow.nfo").exists()


class TestBatchNfoCreation:
    @patch("renamer.picker.MultiPicker.run")
    @patch.object(NfoGenerator, "process_folder")
    def test_batch_create_nfo_generates_files(
        self,
        mock_process: MagicMock,
        mock_picker: MagicMock,
        tmp_path: Path,
    ):
        from renamer.cli.menus import batch_create_nfo
        from renamer.cli.multi_series import DiscoveredSeries

        f1 = tmp_path / "Anime One"
        f1.mkdir()
        (f1 / "ep01.mkv").write_text("video")
        f2 = tmp_path / "Anime Two"
        f2.mkdir()
        (f2 / "ep01.mkv").write_text("video")

        s1 = DiscoveredSeries(folder=f1, folder_name=f1.name, resolved_name="Anime One", tmdb_id=101)
        s2 = DiscoveredSeries(folder=f2, folder_name=f2.name, resolved_name="Anime Two", tmdb_id=102)

        mock_picker.return_value = [("label1", s1), ("label2", s2)]
        mock_process.return_value = Path("tvshow.nfo")

        cfg = Config(media_dir=tmp_path)
        batch_create_nfo(cfg, target_dir=tmp_path)

        assert mock_process.call_count == 2
        mock_process.assert_any_call(
            folder=f1,
            tmdb_id=101,
            preferred_title="Anime One",
            anilist_id=None,
            overwrite=True,
        )
        mock_process.assert_any_call(
            folder=f2,
            tmdb_id=102,
            preferred_title="Anime Two",
            anilist_id=None,
            overwrite=True,
        )

