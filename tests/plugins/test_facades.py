import pytest

from src.core.config import Config
from src.core.mod_manager import ModManager
from src.models.mod import AgeRating
from src.plugin_sdk.facades import GameFacade, ModFacade
from tests.helpers import register_games, reset_game_service


@pytest.fixture
def library(temp_data_dir, tmp_path):
    register_games("first", "second")
    Config().set("import.format", "zip")
    manager = ModManager()
    manager.set_game("first")
    character = manager.add_character(name="Character", node_type="character")
    source = tmp_path / "Example"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = manager.import_mod(source_path=str(source), node_id=character.id)
    assert mod is not None
    mod.info.author = "Plugin author"
    mod.info.age_rating = AgeRating.R
    mod.info.tags = ["Blue", "Casual"]
    assert manager._index.update_mod(mod)
    yield manager, mod, character
    manager.stop_ini_sync()
    manager._index.close()
    reset_game_service()


@pytest.mark.parametrize("read", ["single", "list", "search"])
def test_reads_real_metadata(library, read):
    manager, mod, _ = library
    facade = ModFacade(manager, frozenset({"mods:read"}))
    if read == "single":
        info = facade.get_mod(mod.mod_id)
    elif read == "list":
        (info,) = facade.get_mods()
    else:
        (info,) = facade.search("Example")
    assert info.author == "Plugin author"
    assert info.rating == "R"
    assert info.tags == ("Blue", "Casual")
    assert info.mod_id == mod.mod_id
    assert info.game_id == "first"


def test_mod_id_keeps_owning_game_after_switch(library):
    manager, mod, _ = library
    manager.set_game("second")
    facade = ModFacade(manager, frozenset({"mods:read"}))
    assert facade.current_game_id() == "second"
    assert facade.get_mods() == []
    assert facade.get_mod(mod.mod_id).game_id == "first"
    assert facade.get_mod("missing") is None


def test_single_mod_identity_survives_projection_rebuild(library):
    manager, mod, _ = library
    old_id = mod.mod_id
    imported_at = mod.imported_at
    manager.rebuild_index_from_filesystem("first")
    manager.set_game("second")
    facade = ModFacade(manager, frozenset({"mods:read"}))
    info = facade.get_mod(old_id)
    assert info.mod_id == mod.mod_id
    assert info.game_id == "first"
    assert info.mod_id == mod.mod_id
    assert manager.get_mod(old_id).imported_at == imported_at
    assert facade.get_mod("missing") is None
    assert not hasattr(facade, "get_by_deployment_id")


def test_character_view_exposes_game_and_node_type(library):
    manager, _, character = library
    facade = GameFacade(manager, frozenset({"mods:read"}))
    info = facade.get_character(character.id)
    assert info.game_id == "first"
    assert info.node_type == "character"
    assert info in facade.get_characters()
