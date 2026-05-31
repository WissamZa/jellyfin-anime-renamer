"""
tests/test_romaniser.py
=======================
Unit tests for Romaniser and anime_title_case.
"""

import pytest

from renamer.romaniser import Romaniser, anime_title_case


class TestAnimeTitleCase:
    def test_first_word_always_capitalised(self):
        assert anime_title_case("no game no life").startswith("No")

    def test_particles_stay_lowercase(self):
        result = anime_title_case("honzuki no gekokujou")
        words = result.split()
        assert words[1] == "no"

    def test_non_particle_words_capitalised(self):
        result = anime_title_case("sword art online")
        assert result == "Sword Art Online"

    def test_first_word_particle_still_capitalised(self):
        result = anime_title_case("no game no life")
        assert result.startswith("No")

    def test_empty_string(self):
        assert anime_title_case("") == ""

    def test_single_word(self):
        assert anime_title_case("bleach") == "Bleach"

    def test_preserves_mixed_case_non_particles(self):
        result = anime_title_case("the rising of the shield hero")
        # 'the' and 'of' are lowercase words, 'rising', 'shield', 'hero' capitalised
        assert "Rising" in result
        assert "Shield" in result
        assert "Hero" in result


class TestRomaniser:
    @pytest.fixture
    def romaniser(self):
        return Romaniser()

    def test_ascii_title_unchanged(self, romaniser):
        assert romaniser.to_romaji("One Piece") == "One Piece"

    def test_is_japanese_detects_hiragana(self, romaniser):
        assert romaniser.is_japanese("あ") is True

    def test_is_japanese_detects_katakana(self, romaniser):
        assert romaniser.is_japanese("ア") is True

    def test_is_japanese_detects_kanji(self, romaniser):
        assert romaniser.is_japanese("漫画") is True

    def test_is_japanese_false_for_ascii(self, romaniser):
        assert romaniser.is_japanese("Naruto") is False

    def test_romanises_hiragana(self, romaniser):
        if not romaniser._available:
            pytest.skip("pykakasi not installed")
        result = romaniser.to_romaji("なると")
        assert isinstance(result, str)
        assert len(result) > 0
        # Should not contain hiragana
        assert not romaniser.is_japanese(result)

    def test_passes_through_when_unavailable(self, monkeypatch):
        r = Romaniser.__new__(Romaniser)
        r._available = False
        r._kks = None
        r._JAPANESE = Romaniser._JAPANESE
        result = r.to_romaji("テスト")
        assert result == "テスト"
