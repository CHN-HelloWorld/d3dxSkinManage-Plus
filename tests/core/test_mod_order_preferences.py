"""验证 Mod 顺序与类别顺序共用独立文件且互不覆盖。"""

import json

from src.core.config import Config
from src.ui.category_order import CategoryOrderStore


def test_mod_order_upgrades_old_file_without_changing_category_order(temp_data_dir):
    path = temp_data_dir / "config" / "category_orders.json"
    path.write_text(json.dumps({"schema_version": 1, "games": {"game.a": {"": ["category-b", "category-a"]}}}))
    store = CategoryOrderStore(Config())
    assert store.save_order("game.a", "parent.with.dot", ["mod-b", "mod-a"], item_type="mod")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["games"] == {"game.a": {"": ["category-b", "category-a"]}}
    assert payload["mods"] == {"game.a": {"parent.with.dot": ["mod-b", "mod-a"]}}
    restored = CategoryOrderStore(Config())
    assert restored.ordered_ids("game.a", "parent.with.dot", ["mod-a", "mod-b"], item_type="mod") == ["mod-b", "mod-a"]


def test_mod_scope_reconcile_closes_gaps_appends_and_keeps_other_scopes(temp_data_dir):
    store = CategoryOrderStore(Config())
    assert store.save_order("game.a", "parent", ["mod-c", "mod-a", "mod-b"], item_type="mod")
    assert store.save_order("game.b", "parent", ["mod-other"], item_type="mod")
    assert store.save_order("game.a", "other-parent", ["mod-elsewhere"], item_type="mod")
    store.reconcile_mod_scope("game.a", "parent", ["mod-b", "new", "mod-c"])
    expected = ["mod-c", "mod-b", "new"]
    assert store.ordered_ids("game.a", "parent", ["new", "mod-b", "mod-c"], item_type="mod") == expected
    before = store.path.read_bytes()
    store.reconcile_mod_scope("game.a", "parent", ["mod-b", "new", "mod-c"])
    assert store.path.read_bytes() == before
    assert store.ordered_ids("game.b", "parent", ["mod-other"], item_type="mod") == ["mod-other"]
    assert store.has_order("game.a", "other-parent", item_type="mod")


def test_category_move_keeps_mod_scope_and_deleted_parent_removes_only_its_scope(temp_data_dir):
    store = CategoryOrderStore(Config())
    assert store.save_order("game.a", "parent-a", ["mod-b", "mod-a"], item_type="mod")
    assert store.save_order("game.a", "deleted-parent", ["old"], item_type="mod")
    store.reconcile_game("game.a", [{"id": "parent-b"}, {"id": "parent-a", "parent_id": "parent-b"}])
    assert store.has_order("game.a", "parent-a", item_type="mod")
    assert not store.has_order("game.a", "deleted-parent", item_type="mod")
    assert store.ordered_ids("game.a", "parent-a", ["mod-a", "mod-b"], item_type="mod") == ["mod-b", "mod-a"]


def test_failed_mod_save_preserves_both_sequences(temp_data_dir, monkeypatch):
    from src.ui import category_order as order_module

    store = CategoryOrderStore(Config())
    assert store.save_order("game.a", "", ["category-b", "category-a"])
    assert store.save_order("game.a", "", ["mod-b", "mod-a"], item_type="mod")
    before = store.path.read_bytes()

    def fail_write(*args):
        """模拟排序文件原子替换失败。"""
        raise PermissionError("locked")

    monkeypatch.setattr(order_module, "write_json_atomic", fail_write)
    assert not store.save_order("game.a", "", ["mod-a", "mod-b"], item_type="mod")
    assert store.path.read_bytes() == before
    assert store.ordered_ids("game.a", "", ["mod-a", "mod-b"], item_type="mod") == ["mod-b", "mod-a"]
    assert store.ordered_ids("game.a", "", ["category-a", "category-b"]) == ["category-b", "category-a"]
