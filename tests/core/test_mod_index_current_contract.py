from src.core.mod_index import ModIndex
from src.core.mod_manager import ModManager
from src.models.mod import Mod
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


def test_add_mod_rejects_empty_path(temp_data_dir):
    index = ModIndex("game_a")
    mod = make_mod(index, path="")
    assert not index.add_mod(mod)
    assert index.get_mod(mod.mod_id) is None


def test_add_mod_rejects_empty_game_id(temp_data_dir):
    index = ModIndex("game_a")
    mod = make_mod(index)
    mod.game_id = ""
    assert not index.add_mod(mod)
    assert index.get_mod(mod.mod_id) is None


def test_update_after_deactivation_preserves_explicit_game_owner(temp_data_dir):
    index = ModIndex("game_a")
    mod = make_mod(index)
    assert index.add_mod(mod)
    index.game_id = ""
    mod.name = "Edited after deactivation"
    assert index.update_mod(mod)
    assert index.get_mod(mod.mod_id).game_id == "game_a"
    assert index._db().execute("SELECT path FROM mods").fetchone()[0] == "body.zip"


def test_set_game_never_computes_content_hashes(temp_data_dir, monkeypatch):
    manager = ModManager()
    manager.set_game("game_a")

    def forbidden(*args, **kwargs):
        raise AssertionError("game switch hashed content")

    monkeypatch.setattr(Mod, "compute_sha1", forbidden)
    manager.set_game("game_b")
    assert manager.current_game_id == "game_b"
