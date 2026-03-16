"""验证用户配置与在线候选共同决定游戏身份。"""

import pytest

from src.services.gamebanana.constants import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_USER_AGENT_TEMPLATE,
    gb_override_key,
    official_name_from_records,
    pick_gb_game_match,
    resolve_gb_game_id_by_name,
)


def test_default_page_size_is_positive():
    assert DEFAULT_PAGE_SIZE > 0


def test_default_user_agent_contains_version_placeholder():
    assert "{ver}" in DEFAULT_USER_AGENT_TEMPLATE


@pytest.mark.parametrize(
    ("name", "local_id", "overrides", "expected"),
    [
        (" Example Game ", "game_a", {"example game": 42}, 42),
        ("EXAMPLE GAME", "game_a", {"example game": 42}, 42),
        ("", "game_a", {"game_a": 43}, 43),
        ("  ", "game_a", {"game_a": 43}, 43),
        ("Example Game", "game_a", {"game_a": 43}, None),
        ("Renamed Game", "game_a", {"example game": 42}, None),
        ("Renamed Game", "game_a", {"renamed game": 44}, 44),
        ("Example Game", "game_a", None, None),
        ("", "game_a", None, None),
        ("", "", {"": 45}, None),
    ],
)
def test_only_current_identity_can_use_a_saved_mapping(name, local_id, overrides, expected):
    assert resolve_gb_game_id_by_name(name, local_id, overrides) == expected


def test_override_key_normalizes_names_but_preserves_local_ids():
    assert gb_override_key(" Example Game ", "game_a") == "example game"
    assert gb_override_key("", "Game_A") == "Game_A"


@pytest.mark.parametrize(
    ("records", "query", "expected"),
    [
        ([{"_idRow": 42, "_sName": "Other"}, {"_idRow": "43", "_sName": "EXAMPLE GAME"}], " Example Game ", 43),
        ([{"_idRow": 42, "_sName": "Example Game"}], "Example", 42),
        ([{"_idRow": 42, "_sName": "First"}, {"_idRow": 43, "_sName": "Second"}], "Example", None),
        ([{"_idRow": "invalid", "_sName": "Example Game"}], "Example Game", None),
        ([], "Example Game", None),
    ],
)
def test_online_matching_requires_exact_or_unambiguous_candidates(records, query, expected):
    assert pick_gb_game_match(records, query) == expected


def test_display_name_comes_from_the_selected_online_record():
    records = [
        {"_idRow": "invalid", "_sName": "Invalid"},
        {"_idRow": 42, "_sName": "Other"},
        {"_idRow": "43", "_sName": " Example Game "},
    ]

    assert official_name_from_records(records, 43) == "Example Game"
    assert official_name_from_records(records, 44) == ""
