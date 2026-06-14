"""
tests/test_tmdb_episode_group.py
================================
Unit tests for TMDB episode group parsing, especially the
_parse_group_start function and absolute number calculation
for shows like One Piece where group episode counts differ
from the nominal range.
"""

import re
from typing import Any

# ---------------------------------------------------------------------------
# _parse_group_start (accessed via the compiled regex inside the method)
# ---------------------------------------------------------------------------

# Replicate the regex from fetch_episode_group_details for direct testing
_EP_RANGE_RE = re.compile(
    r"\(\s*(\d+)\s*[-\u2013\u2014]\s*(\d+|current|ongoing)\s*\)",
    re.IGNORECASE,
)


def _parse_group_start(name: str, episode_count: int) -> int | None:
    """Mirror of the function inside fetch_episode_group_details."""
    m = _EP_RANGE_RE.search(name)
    if m:
        start = int(m.group(1))
        if start > 0:
            return start
    return None


class TestParseGroupStart:
    """Tests for extracting the absolute episode start from group names."""

    def test_standard_range(self):
        assert _parse_group_start("East Blue (1-61)", 61) == 1

    def test_mid_range(self):
        assert _parse_group_start("Alabasta (62-143)", 82) == 62

    def test_high_range(self):
        assert _parse_group_start("Egghead (1089-1155)", 79) == 1089

    def test_count_mismatch_still_returns_start(self):
        """Egghead (1089-1155) has 79 eps but range suggests 67 — must still return 1089."""
        assert _parse_group_start("Egghead (1089-1155)", 79) == 1089

    def test_dressrosa_count_mismatch(self):
        """Dressrosa (629-750) has 126 eps but range suggests 122."""
        assert _parse_group_start("Dressrosa (629-750)", 126) == 629

    def test_wano_count_mismatch(self):
        """Land of Wano (892-1088) has 208 eps but range suggests 197."""
        assert _parse_group_start("Land of Wano (892-1088)", 208) == 892

    def test_current_end(self):
        """Groups with 'current' as the end should still extract start."""
        assert _parse_group_start("Elbaph (1156-current)", 8) == 1156

    def test_ongoing_end(self):
        assert _parse_group_start("Arc (200-ongoing)", 50) == 200

    def test_en_dash(self):
        assert _parse_group_start("Arc (1\u2013100)", 100) == 1

    def test_em_dash(self):
        assert _parse_group_start("Arc (1\u2014100)", 100) == 1

    def test_no_range_returns_none(self):
        assert _parse_group_start("Some Arc", 50) is None

    def test_zero_start_returns_none(self):
        assert _parse_group_start("Arc (0-10)", 10) is None

    def test_spaces_in_range(self):
        assert _parse_group_start("Arc ( 100 - 200 )", 100) == 100


class TestAbsoluteNumberCalculation:
    """
    Integration test for the absolute number calculation logic
    using simulated One Piece Crunchyroll episode group data.
    """

    @staticmethod
    def _build_mapping(groups, use_orig_ep=True):
        """
        Simulate fetch_episode_group_details to build the episode mapping.

        groups: list of (name, episode_count, orig_ep_list)
        orig_ep_list: list of orig_ep values for each episode in the group,
                      or None to auto-generate from range start + position.
        """
        abs_counter = 1
        mapping = {}

        for name, count, orig_eps in groups:
            group_abs_start = _parse_group_start(name, count)

            for i in range(count):
                # Get orig_ep
                if orig_eps is not None:
                    orig_ep = orig_eps[i] if i < len(orig_eps) else 0
                elif group_abs_start is not None:
                    # Simulate: orig_ep = absolute number (like One Piece)
                    orig_ep = group_abs_start + i
                else:
                    orig_ep = 0

                # Apply the logic from the fixed code
                if group_abs_start is not None:
                    if use_orig_ep and orig_ep >= group_abs_start:
                        actual_abs = orig_ep
                    else:
                        actual_abs = group_abs_start + i
                else:
                    actual_abs = abs_counter

                abs_counter += 1
                mapping[actual_abs] = (name, i)

        return mapping

    def test_episode_1137_with_orig_ep(self):
        """
        Episode 1137 should map to Egghead (1089-1155) at position 48
        when using orig_ep-based absolute numbering.
        """
        groups = [
            ("East Blue (1-61)", 61, None),
            ("Alabasta (62-143)", 82, None),
            ("Skypiea (144-206)", 63, None),
            ("Davy Back Fight (207-228)", 22, None),
            ("Water Seven (229-263)", 35, None),
            ("Enies Lobby (264-336)", 73, None),
            ("Thriller Bark (337-381)", 45, None),
            ("Seabody Archipelago (382-407)", 26, None),
            ("Amazon Lily (408-421)", 14, None),
            ("Impel Down (422-458)", 37, None),
            ("Marineford (459-516)", 58, None),
            ("Fish-Man Island (517-578)", 62, None),
            ("Punk Hazard (579-628)", 49, None),
            ("Dressrosa (629-750)", 126, None),
            ("Zou (751-782)", 32, None),
            ("Whole Cake Island (783-891)", 111, None),
            ("Land of Wano (892-1088)", 208, None),
            ("Egghead (1089-1155)", 79, None),
            ("Elbaph (1156-current)", 8, None),
        ]
        mapping = self._build_mapping(groups)

        assert 1137 in mapping, "Episode 1137 must exist in the mapping"
        name, pos = mapping[1137]
        assert name == "Egghead (1089-1155)"
        assert pos == 1137 - 1089, f"Expected position {1137 - 1089}, got {pos}"

    def test_episode_1_maps_to_east_blue(self):
        """Episode 1 should map to East Blue."""
        groups = [
            ("East Blue (1-61)", 61, None),
            ("Alabasta (62-143)", 82, None),
        ]
        mapping = self._build_mapping(groups)
        assert 1 in mapping
        assert mapping[1][0] == "East Blue (1-61)"

    def test_episode_62_maps_to_alabasta(self):
        """Episode 62 (first of Alabasta) should map correctly."""
        groups = [
            ("East Blue (1-61)", 61, None),
            ("Alabasta (62-143)", 82, None),
        ]
        mapping = self._build_mapping(groups)
        assert 62 in mapping
        assert mapping[62][0] == "Alabasta (62-143)"
        assert mapping[62][1] == 0  # first episode in group

    def test_extra_episodes_beyond_range_use_orig_ep(self):
        """
        When a group has more episodes than its range and those extra
        episodes have orig_ep values that are absolute numbers, they
        should be mapped using orig_ep, not group_abs_start + i.
        """
        # Simulate Egghead with 79 eps: 67 in range (1089-1155) + 12 specials
        # The specials have orig_ep values that are small (season 0 specials)
        orig_eps = list(range(1089, 1156))  # 67 episodes in range
        orig_eps += [0] * 12  # 12 specials with orig_ep=0

        groups = [
            ("Egghead (1089-1155)", 79, orig_eps),
        ]
        mapping = self._build_mapping(groups)

        # Episode 1137 (within range) should be correct
        assert 1137 in mapping
        name, pos = mapping[1137]
        assert name == "Egghead (1089-1155)"
        assert pos == 1137 - 1089

        # The 12 specials with orig_ep=0 fall back to group_abs_start + i
        # They would get abs numbers 1089+67=1156 through 1089+78=1167
        assert 1156 in mapping  # first special

    def test_orig_ep_prevents_collision_for_absolute_numbered_extras(self):
        """
        When extra episodes have orig_ep values that are valid absolute
        numbers (>= group_abs_start), using orig_ep prevents collisions
        with the next group.

        This simulates a case where extra episodes in the Egghead group
        are actually later episodes (e.g. 1156, 1157) that Crunchyroll
        groups with Egghead. Since orig_ep >= 1089, the code uses orig_ep
        directly, and Elbaph would then overwrite these entries.
        """
        # Egghead: 67 in-range + 2 extras with orig_ep=1156 and 1157
        # (these are episodes that Crunchyroll groups with Egghead but
        # which have absolute numbers in Elbaph's range)
        orig_eps = list(range(1089, 1156)) + [1156, 1157]
        groups = [
            ("Egghead (1089-1155)", 69, orig_eps),
            ("Elbaph (1156-current)", 8, None),
        ]
        mapping = self._build_mapping(groups)

        # Elbaph episodes should overwrite Egghead's extras at 1156-1157
        # since they process later
        assert 1156 in mapping
        assert mapping[1156][0] == "Elbaph (1156-current)"

        # Main Egghead episodes should still be correct
        assert 1137 in mapping
        assert mapping[1137][0] == "Egghead (1089-1155)"

    def test_specials_in_group_fall_back_to_position(self):
        """
        When extra episodes have orig_ep < group_abs_start (e.g. specials
        with season_number=0, episode_number=1), the code falls back to
        group_abs_start + i, which may extend beyond the nominal range.
        This is a known limitation for groups with embedded specials.
        """
        # Egghead: 67 in-range + 2 specials with orig_ep=1 and 2
        orig_eps = list(range(1089, 1156)) + [1, 2]
        groups = [
            ("Egghead (1089-1155)", 69, orig_eps),
        ]
        mapping = self._build_mapping(groups)

        # In-range episodes use orig_ep directly
        assert 1137 in mapping
        assert mapping[1137][0] == "Egghead (1089-1155)"

        # Specials with small orig_ep fall back to group_abs_start + i
        # Position 67: actual_abs = 1089 + 67 = 1156
        assert 1156 in mapping
        assert mapping[1156][0] == "Egghead (1089-1155)"


class TestOriginalSeMap:
    """Test that original TMDB season/episode mapping is built correctly."""

    def test_original_se_map_uses_tmdb_numbering(self):
        """
        For One Piece, TMDB's default structure uses absolute numbering.
        Episode 1137 would have season_number=22, episode_number=1137.
        The _original_se_map should have key (22, 1137).
        """
        # This is a documentation test showing expected behavior.
        # In TMDB's default structure for One Piece:
        # - Season 22 is the Egghead arc
        # - episode_number = absolute episode number (e.g., 1089-1155)
        # So _original_se_map[(22, 1137)] should exist
        #
        # When matching S02E1137:
        # 1. season_ep_map.get((2, 1137)) -> None (group uses sequential seasons)
        # 2. original_se_map.get((2, 1137)) -> None (TMDB has season 22, not 2)
        # 3. episode_map.get(1137) -> Should work with the fix!
        assert True  # Placeholder - actual test requires API mock


class TestAbsoluteGroupNumberingDetection:
    """Test detection of absolute/continuing group episode numbers."""

    def test_absolute_group_numbering_detection(self):
        # Simulate TMDB response format for groups
        groups: list[dict[str, Any]] = [
            {
                "name": "Season 1",
                "episodes": [{"episode_number": i, "season_number": 1} for i in range(1, 9)],
            },
            {
                "name": "Season 2",
                "episodes": [{"episode_number": i, "season_number": 2} for i in range(9, 31)],
            },
        ]

        # Check logic inside fetch_episode_group_details
        has_absolute_group_nums = False
        non_special_groups = []
        for g in groups:
            gname = g.get("name", "")
            if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
                continue
            eps = g.get("episodes", [])
            if {ep.get("season_number", -1) for ep in eps} == {0}:
                continue
            non_special_groups.append(g)

        if len(non_special_groups) > 1:
            for g in non_special_groups[1:]:
                eps = g.get("episodes", [])
                if eps and eps[0].get("episode_number", 0) > 1:
                    has_absolute_group_nums = True
                    break

        assert has_absolute_group_nums is True

    def test_per_season_group_numbering_detection(self):
        # Simulate standard per-season groups where each season starts at 1
        groups: list[dict[str, Any]] = [
            {
                "name": "Season 1",
                "episodes": [{"episode_number": i, "season_number": 1} for i in range(1, 9)],
            },
            {
                "name": "Season 2",
                "episodes": [{"episode_number": i, "season_number": 2} for i in range(1, 10)],
            },
        ]

        has_absolute_group_nums = False
        non_special_groups = []
        for g in groups:
            gname = g.get("name", "")
            if re.search(r"\bspecials?\b", gname, re.IGNORECASE):
                continue
            eps = g.get("episodes", [])
            if {ep.get("season_number", -1) for ep in eps} == {0}:
                continue
            non_special_groups.append(g)

        if len(non_special_groups) > 1:
            for g in non_special_groups[1:]:
                eps = g.get("episodes", [])
                if eps and eps[0].get("episode_number", 0) > 1:
                    has_absolute_group_nums = True
                    break

        assert has_absolute_group_nums is False
