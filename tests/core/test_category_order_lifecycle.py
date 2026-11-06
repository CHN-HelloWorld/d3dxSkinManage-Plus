"""用真实分类目录、索引及游戏迁移验证自定义排序的生命周期。"""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from src.core.config import Config
from src.core.game_service import GameConfig, GameService
from src.core.mod_manager import ModManager
from src.ui.category_order import CategoryOrderStore
from tests.helpers import reset_game_service


@pytest.fixture
def library(temp_data_dir):
    """创建完整游戏与管理器，结束时关闭索引、监听和单例。"""
    reset_game_service()
    service = GameService()
    game_id = service.save_game(GameConfig(name="示例游戏", name_en="Original Resource"))
    manager = ModManager()
    manager.set_game(game_id)
    yield manager, service, game_id, CategoryOrderStore(Config())
    current = ModManager._instance
    if current is not None:
        current.stop_ini_sync()
        current.variants.stop_watch()
        current._index.close()
    reset_game_service()


def _tree(manager):
    """创建两个父类别以及三个直接子类别。"""
    parent = manager.add_character("Parent A")
    destination = manager.add_character("Parent B")
    children = [manager.add_character(name, parent_id=parent.id) for name in ("Alpha", "Beta", "Charlie")]
    return parent, destination, children


def test_actual_cut_preserves_internal_order_and_appends_to_destination(library):
    manager, _service, game_id, store = library
    parent, destination, children = _tree(manager)
    existing = manager.add_character("Existing", parent_id=destination.id)
    expected = [children[2].id, children[0].id, children[1].id]
    assert store.save_order(game_id, parent.id, expected)
    assert store.save_order(game_id, "", [parent.id, destination.id])
    assert store.save_order(game_id, destination.id, [existing.id])
    assert manager.move_character(parent.id, destination.id)
    store.reconcile_game(game_id, manager.get_characters_for_ui())
    assert store.ordered_ids(game_id, parent.id, [child.id for child in children]) == expected
    assert store.ordered_ids(game_id, "", [destination.id]) == [destination.id]
    assert store.ordered_ids(game_id, destination.id, [parent.id, existing.id]) == [existing.id, parent.id]


def test_in_app_delete_closes_gap_and_new_category_is_appended(library):
    manager, _service, game_id, store = library
    parent, _destination, children = _tree(manager)
    assert store.save_order(game_id, parent.id, [children[2].id, children[0].id, children[1].id])
    assert manager.delete_character(children[0].id)
    added = manager.add_character("New", parent_id=parent.id)
    store.reconcile_game(game_id, manager.get_characters_for_ui())
    assert store.ordered_ids(game_id, parent.id, [children[1].id, added.id, children[2].id]) == [
        children[2].id,
        children[1].id,
        added.id,
    ]


def test_external_delete_while_closed_filters_missing_card_and_closes_gap(library, temp_data_dir):
    manager, _service, game_id, store = library
    parent, _destination, children = _tree(manager)
    assert store.save_order(game_id, parent.id, [children[2].id, children[0].id, children[1].id])
    deleted_path = manager._index.paths.node_path(children[0].id)
    deleted_path.resolve().relative_to(temp_data_dir.resolve())
    manager.stop_ini_sync()
    manager.variants.stop_watch()
    manager._index.close()
    ModManager._instance = None
    shutil.rmtree(deleted_path)
    restarted = ModManager()
    restarted.set_game(game_id)
    snapshot = restarted.get_characters_for_ui()
    assert children[0].id not in {node["id"] for node in snapshot}
    restored = CategoryOrderStore(Config())
    restored.reconcile_game(game_id, snapshot)
    assert restored.ordered_ids(game_id, parent.id, [children[1].id, children[2].id]) == [
        children[2].id,
        children[1].id,
    ]
    assert restarted.get_character(children[0].id) is not None


def test_unavailable_root_never_hides_records_or_clears_order(library, monkeypatch):
    """根目录不可访问不等于用户删除，不能据此清除顺序。"""
    from src.core import mod_manager as manager_module
    from src.core.library_snapshot import PathCheck

    manager, _service, game_id, store = library
    parent, _destination, children = _tree(manager)
    expected = [children[2].id, children[0].id, children[1].id]
    assert store.save_order(game_id, parent.id, expected)
    monkeypatch.setattr(manager_module, "check_path", lambda path: PathCheck(path, "error", "access denied"))
    snapshot = manager.get_characters_for_ui()
    assert {child.id for child in children}.issubset({node["id"] for node in snapshot})
    store.reconcile_game(game_id, snapshot)
    assert store.ordered_ids(game_id, parent.id, [child.id for child in children]) == expected


def test_actual_english_name_change_preserves_ids_and_independent_order_file(library, temp_data_dir):
    manager, service, game_id, store = library
    parent, _destination, children = _tree(manager)
    expected = [children[2].id, children[0].id, children[1].id]
    assert store.save_order(game_id, parent.id, expected)
    before = store.path.read_bytes()
    assert service.save_game(replace(service.get_game(game_id), name_en="Renamed Resource")) == game_id
    manager.set_game(game_id)
    assert service.get_game(game_id).id == game_id
    assert not (temp_data_dir / "data" / "Original Resource").exists()
    assert manager._index.paths.node_path(parent.id).is_dir()
    restored = CategoryOrderStore(Config())
    restored.reconcile_game(game_id, manager.get_characters_for_ui())
    assert restored.ordered_ids(game_id, parent.id, [child.id for child in children]) == expected
    assert store.path.read_bytes() == before
