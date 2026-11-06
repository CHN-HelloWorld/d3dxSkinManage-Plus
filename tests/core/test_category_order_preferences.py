"""验证类别排序独立文件、旧设置迁移与失效 ID 协调。"""

from __future__ import annotations

import json

import pytest

from src.core.config import SETTINGS_SCHEMA_VERSION, Config
from src.ui import category_order as order_module
from src.ui.category_order import CategoryOrderStore

ORDER_KEY = "ui.overview_category_orders"


@pytest.fixture
def order_case(temp_data_dir, monkeypatch):
    """隔离排序文件并记录原子写入，不改动真实用户配置。"""
    config = Config()
    path = temp_data_dir / "config" / "category_orders.json"
    writes = []
    original_write = order_module.write_json_atomic

    def record_write(target, data):
        """记录真正完成的原子写入。"""
        original_write(target, data)
        writes.append(json.loads(json.dumps(data)))

    monkeypatch.setattr(order_module, "write_json_atomic", record_write)
    return config, path, writes


def _seed(path, games):
    """写入独立文件的版本化中性夹具。"""
    path.write_text(json.dumps({"schema_version": 1, "games": games}), encoding="utf-8")


def _games(path):
    """读取磁盘中的排序作用域。"""
    return json.loads(path.read_text(encoding="utf-8"))["games"]


def test_orders_use_separate_file_and_reads_never_write_settings(order_case):
    config, path, writes = order_case
    config.set("ui.overview_category_sort_key", "custom")
    before = config._config_path.read_bytes()
    store = CategoryOrderStore(config)
    assert store.path == path
    assert not path.exists()
    assert store.save_order("game.a", "", ["beta", "alpha"])
    assert config._config_path.read_bytes() == before
    assert config.get(ORDER_KEY) is None
    assert _games(path) == {"game.a": {"": ["beta", "alpha"]}}
    writes.clear()
    assert store.ordered_ids("game.a", "", ["alpha", "beta", "new"]) == ["beta", "alpha", "new"]
    assert writes == []


def test_reconcile_closes_gaps_appends_new_ids_and_preserves_moved_parent_scope(order_case):
    config, path, writes = order_case
    _seed(path, {"game.a": {"": ["parent-a", "gone", "parent-b"], "parent-a": ["two", "deleted", "one"]}})
    store = CategoryOrderStore(config)
    snapshot = [
        {"id": "parent-b"},
        {"id": "parent-a", "parent_id": "parent-b", "name": "改名后类别"},
        {"id": "one", "parent_id": "parent-a"},
        {"id": "new", "parent_id": "parent-a"},
        {"id": "two", "parent_id": "parent-a"},
    ]
    store.reconcile_game("game.a", snapshot)
    assert _games(path) == {"game.a": {"": ["parent-b"], "parent-a": ["two", "one", "new"]}}
    assert store.ordered_ids("game.a", "parent-a", ["one", "two", "new"]) == ["two", "one", "new"]
    writes.clear()
    snapshot[1]["name"] = "再次改名"
    store.reconcile_game("game.a", snapshot)
    assert writes == []


def test_orders_survive_restart_reset_and_literal_ids(order_case):
    config, path, _writes = order_case
    store = CategoryOrderStore(config)
    assert store.save_order("game.a", "parent.with.dot", ["beta", "alpha"])
    assert store.save_order("game.b", "parent.with.dot", ["other"])
    before = path.read_bytes()
    assert config.reset()
    Config._instance = None
    restored = CategoryOrderStore(Config())
    assert restored.ordered_ids("game.a", "parent.with.dot", ["alpha", "beta"]) == ["beta", "alpha"]
    assert path.read_bytes() == before
    assert restored.has_order("game.b", "parent.with.dot")


def test_legacy_migration_merges_without_overwriting_dedicated_scopes(order_case):
    config, path, writes = order_case
    config.set(ORDER_KEY, {"game.a": {"": ["old"], "parent": ["child"]}, "game.b": {"": ["other"]}})
    _seed(path, {"game.a": {"": ["newer"]}})
    store = CategoryOrderStore(config)
    expected = {"game.a": {"": ["newer"], "parent": ["child"]}, "game.b": {"": ["other"]}}
    assert _games(path) == expected
    assert config.get(ORDER_KEY) is None
    assert store.has_order("game.a", "parent")
    assert len(writes) == 1
    writes.clear()
    CategoryOrderStore(config)
    assert writes == []


def test_failed_legacy_migration_preserves_settings_and_retries_on_save(order_case, monkeypatch):
    config, path, _writes = order_case
    config.set(ORDER_KEY, {"game.a": {"": ["beta", "alpha"]}})
    before = config._config_path.read_bytes()

    def fail_write(*args):
        """模拟排序目录不可写。"""
        raise PermissionError("read-only")

    with monkeypatch.context() as patch:
        patch.setattr(order_module, "write_json_atomic", fail_write)
        store = CategoryOrderStore(config)
        assert store.ordered_ids("game.a", "", ["alpha", "beta"]) == ["beta", "alpha"]
        assert not store.save_order("game.a", "", ["alpha", "beta"])
    assert config._config_path.read_bytes() == before
    assert not path.exists()
    assert store.save_order("game.a", "", ["alpha", "beta"])
    assert config.get(ORDER_KEY) is None


def test_failed_save_keeps_previous_disk_and_memory_order(order_case, monkeypatch):
    config, path, _writes = order_case
    store = CategoryOrderStore(config)
    assert store.save_order("game.a", "", ["beta", "alpha"])
    before = path.read_bytes()

    def fail_write(*args):
        """模拟原子替换失败，不能发布新顺序。"""
        raise PermissionError("locked")

    monkeypatch.setattr(order_module, "write_json_atomic", fail_write)
    assert not store.save_order("game.a", "", ["alpha", "beta"])
    assert path.read_bytes() == before
    assert store.ordered_ids("game.a", "", ["alpha", "beta"]) == ["beta", "alpha"]


@pytest.mark.parametrize("payload", ["broken json", "[]", '{"schema_version": 1, "games": []}'])
def test_invalid_file_is_backed_up_before_recovery(order_case, payload):
    config, path, _writes = order_case
    path.write_text(payload, encoding="utf-8")
    store = CategoryOrderStore(config)
    backups = list(path.parent.glob("category_orders.json.invalid-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == payload
    assert store.save_order("game.a", "", ["alpha"])
    assert _games(path) == {"game.a": {"": ["alpha"]}}


def test_future_version_and_failed_backup_are_never_overwritten(order_case, monkeypatch):
    config, path, _writes = order_case
    path.write_text('{"schema_version": 3, "games": {}}', encoding="utf-8")
    before = path.read_bytes()
    assert not CategoryOrderStore(config).save_order("game.a", "", ["alpha"])
    assert path.read_bytes() == before
    path.write_text("broken json", encoding="utf-8")
    monkeypatch.setattr(order_module, "backup_invalid_file", lambda *args: None)
    assert not CategoryOrderStore(config).save_order("game.a", "", ["alpha"])
    assert path.read_text(encoding="utf-8") == "broken json"


def test_corrupt_ids_and_other_games_are_handled_without_cross_scope_changes(order_case):
    config, path, _writes = order_case
    _seed(path, {"game.a": {"": [None, {}, "beta", "beta", "gone"], "gone": ["old"]}, "game.b": {"": ["other"]}})
    store = CategoryOrderStore(config)
    assert store.ordered_ids("game.a", "", ["alpha", "beta", "new"]) == ["beta", "alpha", "new"]
    store.reconcile_game("game.a", [{"id": "alpha"}, {"id": "beta"}, {"id": "new"}])
    assert _games(path) == {"game.a": {"": ["beta", "alpha", "new"]}, "game.b": {"": ["other"]}}


def test_old_settings_keep_defaults_without_creating_or_rewriting_files(order_case):
    config, path, _writes = order_case
    before = config._config_path.read_bytes() if config._config_path.exists() else None
    store = CategoryOrderStore(config)
    assert config.get("ui.overview_sort_key") == "alpha_asc"
    assert config.get("ui.overview_category_sort_key") == "alpha_asc"
    assert SETTINGS_SCHEMA_VERSION == 1
    assert config.get(ORDER_KEY) is None
    assert not store.has_order("game.a", "")
    assert not path.exists()
    assert (config._config_path.read_bytes() if config._config_path.exists() else None) == before
