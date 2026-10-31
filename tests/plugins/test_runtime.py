from __future__ import annotations

import json
import shutil
import types
import zipfile
from pathlib import Path

import pytest

from src.plugins.runtime import manager as runtime
from src.plugins.runtime.manifest import ManifestError, parse_manifest
from src.plugins.runtime.registry import PluginState

ENTRY = """from src.plugin_sdk import PluginBase
class Plugin(PluginBase):
    def activate(self, ctx):
        self.calls = 0
        self.stops = 0
        ctx.events.on("game_changed", self.on_event)
        ctx.commands.register(ctx.plugin_id + ".command", lambda: self.calls)
        if ctx.has_permission("ui:views"):
            ctx.ui.register_page("page", "Page", lambda: object())
    def on_event(self, **payload):
        self.calls += 1
    def deactivate(self):
        self.stops += 1
"""


def write_plugin(base, pid="test.sample", **changes):
    directory = base / pid
    directory.mkdir(parents=True, exist_ok=True)
    data = dict(
        id=pid,
        name=pid,
        version="1.0.0",
        apiVersion="2.0",
        activationEvents=["onStartup"],
        entry="entry.py",
        permissions=["mods:read", "ui:views"],
        contributes={"commands": [{"id": pid + ".command"}]},
    )
    data.update(changes)
    (directory / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "entry.py").write_text(ENTRY, encoding="utf-8")
    return directory


class Host:
    def __init__(self):
        self.listeners = []

    def add_event_listener(self, callback):
        self.listeners.append(callback)

    def remove_event_listener(self, callback):
        self.listeners.remove(callback)


class UIHost:
    def __init__(self):
        self.pages = []

    def register_page(self, pid, *args):
        from src.plugin_sdk.disposable import Disposable

        self.pages.append(pid)
        return Disposable(lambda: self.cleanup_plugin(pid))

    def cleanup_plugin(self, pid):
        self.pages[:] = [p for p in self.pages if p != pid]


@pytest.fixture
def env(temp_data_dir, monkeypatch):
    external = temp_data_dir / "plugins"
    external.mkdir()
    monkeypatch.setattr(runtime, "get_plugins_dir", lambda: external)
    from src.plugins.runtime import installer

    monkeypatch.setattr(installer, "get_plugins_dir", lambda: external)
    host = Host()
    manager = runtime.PluginManager(host)
    manager.set_ui_host(UIHost())
    yield manager, external, host
    manager.shutdown()


def activate(manager, pid="test.sample"):
    manager.discover()
    assert manager.trust(pid)
    assert manager.enable(pid)
    assert manager.get(pid).state is PluginState.ACTIVE
    return manager.get(pid)


def test_rescan_releases_previous_instance_and_registrations(env):
    manager, external, _ = env
    write_plugin(external)
    old = activate(manager).instance
    assert manager.load_all() == 1
    manager.emit("game_changed", game_id="a")
    assert old.stops == 1
    assert old.calls == 0
    assert manager.get("test.sample").instance.calls == 1
    assert len(manager.contributions.commands()) == 1
    assert manager._ui_host.pages == ["test.sample"]
    assert manager.activate_enabled() == 1
    manager.emit("game_changed", game_id="a")
    assert manager.get("test.sample").instance.calls == 2


def test_install_preserves_unrelated_active_record(env, tmp_path):
    manager, external, _ = env
    write_plugin(external)
    original = activate(manager)
    package = write_plugin(tmp_path / "source", "test.other")
    archive = Path(shutil.make_archive(str(tmp_path / "other"), "zip", package))
    manager.install_archive(archive)
    assert manager.get("test.sample") is original
    assert original.state is PluginState.ACTIVE
    manager.emit("game_changed", game_id="b")
    assert original.instance.calls == 1
    assert manager.get("test.other").state is PluginState.DISCOVERED


def test_minimum_app_version_is_checked_before_import(env):
    manager, external, _ = env
    write_plugin(external, minAppVersion="999.0.0")
    manager.discover()
    manager.trust("test.sample")
    assert not manager.enable("test.sample")
    rec = manager.get("test.sample")
    assert rec.state is PluginState.ERROR and rec.instance is None
    assert "999.0.0" in rec.error


def test_permission_expansion_requires_new_trust_after_rescan(env):
    manager, external, _ = env
    write_plugin(external)
    old = activate(manager).instance
    write_plugin(external, version="2.0.0", permissions=["mods:read", "mods:manage"])
    assert manager.load_all() == 0
    rec = manager.get("test.sample")
    assert not rec.trusted and rec.instance is None
    assert old.stops == 1
    assert manager.trust(rec.id)
    assert manager.enable(rec.id)
    assert manager._persisted[rec.id]["granted_permissions"] == ["mods:manage", "mods:read"]


def test_reload_reparses_manifest_and_revokes_expanded_permissions(env):
    manager, external, _ = env
    write_plugin(external)
    activate(manager)
    write_plugin(external, permissions=["mods:read", "network"])
    manager.reload("test.sample")
    assert not manager.get("test.sample").trusted
    assert manager.get("test.sample").instance is None


def test_removed_or_broken_plugin_is_cleaned_up(env):
    manager, external, _ = env
    directory = write_plugin(external)
    old = activate(manager).instance
    (directory / "plugin.json").write_text("broken", encoding="utf-8")
    manager.load_all()
    assert manager.get("test.sample") is None
    assert old.stops == 1
    assert manager._ui_host.pages == []
    assert manager.contributions.commands() == []


def test_shutdown_disconnects_host_and_cancels_owner(env):
    manager, external, host = env
    write_plugin(external)
    rec = activate(manager)
    cancelled = []
    manager._operations = types.SimpleNamespace(cancel_owner=cancelled.append)
    instance = rec.instance
    manager.shutdown()
    manager.shutdown()
    assert host.listeners == []
    assert instance.stops == 1
    assert cancelled == [rec.id]
    assert rec.context is None and rec.instance is None


@pytest.mark.parametrize("entry", ["../outside.py", "/outside.py", "C:/outside.py", "..\\outside.py"])
def test_entry_cannot_escape_plugin_directory(tmp_path, entry):
    directory = write_plugin(tmp_path, entry=entry)
    (tmp_path / "outside.py").write_text(ENTRY, encoding="utf-8")
    with pytest.raises(ManifestError):
        parse_manifest(directory)


def test_plugin_without_entry_source_is_not_discovered(temp_data_dir):
    directory = write_plugin(temp_data_dir / "plugins", "test.missing-entry")
    (directory / "entry.py").unlink()
    manager = runtime.PluginManager(Host())
    try:
        assert manager.load_all() == 0
        assert manager.records == []
    finally:
        manager.shutdown()


def test_failed_install_preserves_existing_plugin(env, tmp_path):
    manager, external, _ = env
    directory = write_plugin(external)
    original = activate(manager)
    old_json = (directory / "plugin.json").read_text("utf-8")
    archive = tmp_path / "invalid.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("plugin.json", json.dumps(dict(id="test.sample", name="Broken")))
    from src.plugins.runtime.installer import InstallError

    with pytest.raises(InstallError):
        manager.install_archive(archive)
    assert (directory / "plugin.json").read_text("utf-8") == old_json
    assert manager.get("test.sample") is original
    assert original.state is PluginState.ACTIVE


def test_install_rejects_escaping_id_and_zip_paths(env, tmp_path):
    manager, external, _ = env
    from src.plugins.runtime.installer import InstallError

    for pid, member in [("../outside", "entry.py"), ("test.sample", "../plugins-neighbor/stolen.py")]:
        archive = tmp_path / "invalid.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(
                "plugin.json", json.dumps(dict(id=pid, name="Bad", version="1.0.0", apiVersion="1.0", entry="entry.py"))
            )
            zf.writestr(member, ENTRY)
        with pytest.raises(InstallError):
            manager.install_archive(archive)
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "plugins-neighbor").exists()


def test_shipped_examples_require_trust_before_activation(sample_plugins):
    manager = runtime.PluginManager(Host())
    try:
        assert manager.load_all() == 0
        assert {rec.id for rec in manager.records} == {
            "example.mod-inventory",
            "example.preset-cycle",
            "example.preset-audit",
        }
        assert all(not rec.trusted and rec.instance is None for rec in manager.records)
        for record in manager.records:
            assert record.manifest.directory == sample_plugins / record.id
            assert manager.trust(record.id)
            assert manager.enable(record.id), record.error
        assert {rec.id: rec.state for rec in manager.records} == {
            "example.mod-inventory": PluginState.READY,
            "example.preset-cycle": PluginState.READY,
            "example.preset-audit": PluginState.ACTIVE,
        }
        assert manager.uninstall("example.mod-inventory")
        assert not (sample_plugins / "example.mod-inventory").exists()
        assert manager.get("example.mod-inventory") is None
    finally:
        manager.shutdown()


def test_event_dispatch_stops_remaining_callbacks_after_plugin_failure():
    from src.plugins.runtime.eventbus import EventBus

    called = []
    bus = EventBus(error_sink=lambda pid, *_: bus.unsubscribe_plugin(pid))

    def broken(**payload):
        raise RuntimeError("plugin failure")

    bus.subscribe("broken", "event", broken)
    bus.subscribe("broken", "event", lambda **kw: called.append("stale"))
    bus.subscribe("healthy", "event", lambda **kw: called.append("healthy"))
    bus.emit("event")
    assert called == ["healthy"]


def test_permission_reduction_does_not_require_retrust(env):
    manager, external, _ = env
    write_plugin(external, permissions=["mods:read", "mods:write"])
    activate(manager)
    write_plugin(external, version="2.0.0", permissions=["mods:read"])
    assert manager.load_all() == 1
    assert manager.get("test.sample").trusted
    assert not manager.get("test.sample").context.has_permission("mods:write")


def test_unrecognized_permission_is_rejected_before_discovery(env):
    manager, external, _ = env
    write_plugin(external, permissions=["mods:read", "future:dangerous"])
    manager.discover()
    assert manager.get("test.sample") is None
    assert any("future:dangerous" in error for error in manager.discovery_errors.values())


def test_install_replacement_failure_restores_old_directory(env, tmp_path, monkeypatch):
    manager, external, _ = env
    directory = write_plugin(external)
    original = activate(manager)
    replacement = write_plugin(tmp_path / "source", version="2.0.0")
    archive = Path(shutil.make_archive(str(tmp_path / "update"), "zip", replacement))
    rename = Path.rename

    def fail_new(path, destination):
        if path.name == "new":
            raise PermissionError("replacement denied")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_new)
    from src.plugins.runtime.installer import InstallError

    with pytest.raises(InstallError):
        manager.install_archive(archive)
    assert json.loads((directory / "plugin.json").read_text("utf-8"))["version"] == "1.0.0"
    assert manager.get("test.sample") is original


def test_failed_install_rollback_retains_recoverable_old_files(env, tmp_path, monkeypatch):
    manager, external, _ = env
    write_plugin(external)
    replacement = write_plugin(tmp_path / "source", version="2.0.0")
    archive = Path(shutil.make_archive(str(tmp_path / "update"), "zip", replacement))
    rename = Path.rename

    def fail_replacement_and_restore(path, destination):
        if path.name == "new" or path.name == "previous" or ".backup-" in path.name:
            raise PermissionError("destination locked")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_replacement_and_restore)
    from src.plugins.runtime.installer import InstallError

    with pytest.raises(InstallError):
        manager.install_archive(archive)
    previous = list(external.glob(".test.sample.backup-*/plugin.json"))
    assert len(previous) == 1
    assert json.loads(previous[0].read_text("utf-8"))["version"] == "1.0.0"


@pytest.mark.parametrize("grants", [None, "mods:read", {"mods:read": True}, [None, []]])
def test_malformed_saved_permissions_do_not_crash_or_grant_trust(env, grants):
    manager, external, _ = env
    write_plugin(external)
    manager._persisted["test.sample"] = {"trusted": True, "granted_permissions": grants}
    manager.discover()
    assert not manager.get("test.sample").trusted


@pytest.mark.parametrize("expanded_permissions", [False, True])
def test_install_update_reactivates_only_when_enabled_and_grants_suffice(env, tmp_path, expanded_permissions):
    manager, external, _ = env
    write_plugin(external)
    write_plugin(external, "test.other")
    original = activate(manager).instance
    manager.trust("test.other")
    assert manager.enable("test.other")
    other = manager.get("test.other")
    permissions = ["mods:read", "mods:write"] if expanded_permissions else ["mods:read"]
    package = write_plugin(tmp_path / "source", version="2.0.0", permissions=permissions)
    (package / "entry.py").write_text(ENTRY.replace("self.calls = 0", "self.calls = 42"), encoding="utf-8")
    archive = Path(shutil.make_archive(str(tmp_path / "update"), "zip", package))
    manager.install_archive(archive)
    current = manager.get("test.sample")
    assert original.stops == 1
    assert manager.get("test.other") is other
    assert other.state is PluginState.ACTIVE
    if expanded_permissions:
        assert current.enabled and not current.trusted and current.instance is None
        assert current.state is PluginState.DISCOVERED
    else:
        assert current.state is PluginState.ACTIVE
        assert current.instance is not original
        assert manager.execute_command("test.sample.command") == 42
