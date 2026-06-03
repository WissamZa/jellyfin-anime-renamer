import pytest
from pathlib import Path
from qbit_hook import normalize_for_matching, find_matching_folder

def test_normalize_for_matching():
    # Romaji spelling variants
    assert normalize_for_matching("Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U") == "jidohanbaikiniumarekawattaorewameikyuohokou"
    assert normalize_for_matching("Jidouhanbaiki ni Umarekawatta Ore wa Meikyuu o Samayou") == "jidohanbaikiniumarekawattaorewameikyuosamayo"
    
    # Season stripping
    base_shingeki = normalize_for_matching("Shingeki no Kyojin")
    assert normalize_for_matching("Shingeki no Kyojin Season 3") == base_shingeki
    assert normalize_for_matching("Shingeki no Kyojin 3rd Season") == base_shingeki
    
    base_mushoku = normalize_for_matching("Mushoku Tensei: Jobless Reincarnation")
    assert normalize_for_matching("Mushoku Tensei: Jobless Reincarnation Season 2 Part 2") == base_mushoku
    
    base_spice = normalize_for_matching("Spice and Wolf: Merchant Meets the Wise Wolf")
    assert normalize_for_matching("Spice and Wolf: Merchant Meets the Wise Wolf Part II") == base_spice


def test_find_matching_folder(tmp_path):
    # Setup test directories
    base = tmp_path
    target_dir = base / "Jidouhanbaiki ni Umarekawatta Ore wa Meikyuu o Samayou"
    target_dir.mkdir()
    
    # Matching folder should be found even with spelling variations and season suffixes
    matched = find_matching_folder(
        base,
        candidates=[
            "Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U",
            "Jidouhanbaiki ni Umare Kawa Tta Ore ha Meikyuu wo Houkou U 2nd Season"
        ]
    )
    assert matched == target_dir

    # Exact mismatch should return None
    assert find_matching_folder(base, candidates=["Different Show", "Another Different"]) is None
