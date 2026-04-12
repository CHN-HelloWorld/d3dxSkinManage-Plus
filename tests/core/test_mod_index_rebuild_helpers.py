from __future__ import annotations

from src.core.mod_index import ModIndex
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


def test_clear_mods_for_game_is_scoped(temp_data_dir):
    idx = ModIndex()
    idx.game_id = "game_a"
    idx.add_mod(make_mod(idx, "A"))
    idx.add_mod(make_mod(idx, "B"))
    idx.game_id = "game_b"
    idx.add_mod(make_mod(idx, "C"))

    removed = idx.clear_mods_for_game("game_a")
    assert removed == 2

    assert idx.get_all_mods("game_a") == []
    assert len(idx.get_all_mods("game_b")) == 1


def test_health_check_true_on_normal_db(temp_data_dir):
    idx = ModIndex()
    assert idx.health_check() is True
