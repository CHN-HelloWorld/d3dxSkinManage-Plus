"""验证插件存储的原子性与声明式设置的可观察行为。"""

import json
from pathlib import Path

import pytest

from src.plugin_sdk.errors import PermissionDenied, PluginError
from src.plugin_sdk.storage import PluginStorage
from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_runtime import env as env

SETTINGS = [
    {"key": "limit", "type": "int", "default": 3, "minimum": 1, "maximum": 10},
    {"key": "mode", "type": "str", "default": "brief", "enum": ["brief", "full"], "enumTitles": ["简要", "完整"]},
]


def test_failed_storage_replace_does_not_publish_unsaved_values(tmp_path, monkeypatch):
    storage = PluginStorage(tmp_path)
    storage.set("value", {"items": [1]})
    original = (tmp_path / "store.json").read_bytes()

    def fail_replace(_source, _destination):
        """模拟原子替换失败，验证内存不会先于磁盘发布。"""
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        storage.set("value", {"items": [2]})
    assert storage.get("value") == {"items": [1]}
    assert (tmp_path / "store.json").read_bytes() == original
    with pytest.raises(OSError):
        storage.delete("value")
    assert storage.get("value") == {"items": [1]}


def test_storage_copies_values_on_both_read_and_write(tmp_path):
    storage = PluginStorage(tmp_path)
    value = {"items": [1]}
    storage.set("value", value)
    value["items"].append(2)
    storage.get("value")["items"].append(3)
    storage.all()["value"]["items"].append(4)
    assert storage.get("value") == {"items": [1]}
    assert PluginStorage(tmp_path).get("value") == {"items": [1]}


def test_malformed_store_is_reported_without_overwriting_it(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(PluginError):
        PluginStorage(tmp_path).set("setting", True)
    assert path.read_text("utf-8") == "[]"


def test_configuration_defaults_validation_reset_and_subscriptions(tmp_path):
    from src.plugin_sdk.configuration import ConfigurationFacade

    storage = PluginStorage(tmp_path)
    configuration = ConfigurationFacade(storage, SETTINGS)
    changes = []
    handle = configuration.on_changed(changes.append)
    assert configuration.get("limit") == 3
    for invalid in (True, 0, 11, "3"):
        with pytest.raises(ValueError):
            configuration.set("limit", invalid)
    with pytest.raises(ValueError):
        configuration.set("mode", "unknown")
    configuration.set("limit", 5)
    configuration.set("limit", 5)
    assert [(event.key, event.previous, event.value) for event in changes] == [("limit", 3, 5)]
    configuration.reset("limit")
    assert configuration.get("limit") == 3
    assert storage.get("settings.limit") is None
    assert len(changes) == 2
    handle.dispose()
    configuration.set("limit", 7)
    assert len(changes) == 2
    assert json.loads((tmp_path / "store.json").read_text("utf-8"))["settings.limit"] == 7


def test_configuration_rejects_unknown_keys_and_invalid_saved_values_use_default(tmp_path):
    from src.plugin_sdk.configuration import ConfigurationFacade

    storage = PluginStorage(tmp_path)
    storage.set("settings.limit", -9)
    configuration = ConfigurationFacade(storage, SETTINGS)
    assert configuration.get("limit") == 3
    with pytest.raises(KeyError):
        configuration.set("typo", True)
    assert storage.get("settings.limit") == -9


def test_ready_configuration_is_shared_with_activated_plugin_and_closes_on_disable(env):
    manager, directory, _ = env
    lazy_plugin(
        directory,
        permissions=["storage", "ui:settings"],
        contributes={"commands": [{"id": "test.lazy.run"}], "settings": SETTINGS},
    )
    prepare(manager, "test.lazy")
    configuration = manager.get_configuration("test.lazy")
    configuration.set("limit", 8)
    assert manager.get("test.lazy").instance is None
    manager.execute_command("test.lazy.run")
    context = manager.get("test.lazy").context
    assert context.configuration is configuration
    assert context.configuration.get("limit") == 8
    manager.disable("test.lazy")
    with pytest.raises(PluginError):
        configuration.set("limit", 6)
    with pytest.raises(PluginError):
        context.storage.get("settings.limit")


def test_context_storage_requires_permission(env):
    manager, directory, _ = env
    lazy_plugin(directory)
    prepare(manager, "test.lazy")
    manager.execute_command("test.lazy.run")
    with pytest.raises(PermissionDenied):
        manager.get("test.lazy").context.storage.set("key", "value")
