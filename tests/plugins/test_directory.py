import sys
from types import SimpleNamespace

import pytest

from src.plugins.runtime import manager as runtime
from src.plugins.runtime.registry import PluginState
from src.utils import paths
from tests.plugins.test_runtime import Host, write_plugin

_real_get_app_dir = paths.get_app_dir


def test_empty_directory_has_no_plugins_even_when_saved_plugins_are_enabled(temp_data_dir):
    manager = runtime.PluginManager(Host())
    manager._persisted = {
        "example.mod-inventory": {"enabled": True, "trusted": True, "granted_permissions": []},
    }
    try:
        assert manager.load_all() == 0
        assert manager.records == []
    finally:
        manager.shutdown()


@pytest.mark.parametrize("runtime_mode", ["development", "nuitka-standalone", "nuitka-onefile"])
def test_discovery_and_loading_use_application_directory_not_launch_directory(temp_data_dir, monkeypatch, runtime_mode):
    application = temp_data_dir / "application"
    binary_root = temp_data_dir / "unpacked" if runtime_mode == "nuitka-onefile" else application
    launched_from = temp_data_dir / "unrelated-launch-directory"
    launched_from.mkdir()
    monkeypatch.chdir(launched_from)
    monkeypatch.setattr(paths, "get_app_dir", _real_get_app_dir)
    monkeypatch.setattr(paths, "__file__", str(binary_root / "src/utils/paths.py"))
    monkeypatch.setattr(sys, "executable", str(binary_root / "manager.exe"))
    if runtime_mode.startswith("nuitka"):
        monkeypatch.setattr(
            paths,
            "__compiled__",
            SimpleNamespace(
                onefile=runtime_mode == "nuitka-onefile",
                containing_dir=str(application if runtime_mode == "nuitka-onefile" else application.parent),
            ),
            raising=False,
        )
    write_plugin(application / "plugins", "test.application")
    write_plugin(launched_from / "plugins", "test.unrelated")
    if runtime_mode == "nuitka-onefile":
        write_plugin(binary_root / "plugins", "test.unpacked")

    manager = runtime.PluginManager(Host())
    try:
        assert [record.id for record in manager.discover()] == ["test.application"]
        manager.trust("test.application")
        assert manager.enable("test.application")
        record = manager.get("test.application")
        assert record.state is PluginState.ACTIVE
        assert record.context.plugin_dir == application / "plugins/test.application"
    finally:
        manager.shutdown()
