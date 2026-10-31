import json

import pytest

from src.core.config import Config
from src.core.shortcuts import ShortcutCommand, ShortcutStore
from src.plugins.runtime.manifest import ManifestError, parse_manifest
from tests.plugins.test_runtime import activate, write_plugin
from tests.plugins.test_runtime import env as env


def shortcut_command():
    return ShortcutCommand(
        "test.sample.command", "plugin:test.sample", "Example", "Run example", "Ctrl+Alt+L", frozenset({"window"})
    )


def colliding_command(owner):
    return ShortcutCommand(
        owner + ".hello", "plugin:" + owner, owner, "Run " + owner, "Ctrl+Alt+H", frozenset({"window"})
    )


def write_shortcut_plugin(directory, **changes):
    data = dict(
        permissions=["mods:read", "ui:shortcuts"],
        contributes={
            "commands": [{"id": "test.sample.command", "title": "Example"}],
            "shortcuts": [{"command": "test.sample.command", "default": "Ctrl+Alt+L", "context": "window"}],
        },
    )
    data.update(changes)
    return write_plugin(directory, **data)


def test_plugin_bindings_share_conflicts_and_survive_temporary_deactivation(temp_data_dir):
    store = ShortcutStore(Config())
    command = shortcut_command()
    store.register_plugin("test.sample", "Example plugin", [command])
    assert store.binding(command.id) == "Ctrl+Alt+L"
    assert [item.id for item in store.conflicts(command.id, "Ctrl+C")] == ["edit.copy"]
    assert not store.save_binding(command.id, "Ctrl+C")
    assert store.save_binding(command.id, "Ctrl+C", replace=True)
    assert store.binding("edit.copy") is None
    assert store.binding(command.id) == "Ctrl+C"
    store.unregister_plugin("test.sample")
    assert command.id not in store.bindings
    assert store.save_binding("view.refresh", "Ctrl+Alt+F")
    assert Config().get("shortcuts.overrides")[command.id] == "Ctrl+C"
    store.register_plugin("test.sample", "Example plugin", [command])
    assert store.binding(command.id) == "Ctrl+C"


def test_new_plugin_default_cannot_steal_incumbent_across_reload_and_restart(temp_data_dir):
    incumbent = colliding_command("z.existing")
    newcomer = colliding_command("a.new")
    store = ShortcutStore(Config())

    store.register_plugin("z.existing", "Existing", [incumbent])
    store.register_plugin("a.new", "New", [newcomer])

    assert store.binding(incumbent.id) == "Ctrl+Alt+H"
    assert store.binding(newcomer.id) is None

    store.unregister_plugin("z.existing")
    store.register_plugin("z.existing", "Existing", [incumbent])
    assert store.binding(incumbent.id) == "Ctrl+Alt+H"
    assert store.binding(newcomer.id) is None

    Config._instance = None
    restarted = ShortcutStore(Config())
    restarted.register_plugin("a.new", "New", [newcomer])
    restarted.register_plugin("z.existing", "Existing", [incumbent])
    assert restarted.binding(incumbent.id) == "Ctrl+Alt+H"
    assert restarted.binding(newcomer.id) is None


def test_explicit_conflict_replacement_survives_reenable_and_restart(temp_data_dir):
    incumbent = colliding_command("z.existing")
    newcomer = colliding_command("a.new")
    store = ShortcutStore(Config())
    store.register_plugin("z.existing", "Existing", [incumbent])
    store.register_plugin("a.new", "New", [newcomer])

    assert store.binding(incumbent.id) == "Ctrl+Alt+H"
    assert store.binding(newcomer.id) is None
    assert store.save_binding(newcomer.id, "Ctrl+Alt+H", replace=True)
    assert store.binding(incumbent.id) is None
    assert store.binding(newcomer.id) == "Ctrl+Alt+H"

    store.unregister_plugin("a.new")
    store.register_plugin("a.new", "New", [newcomer])
    assert store.binding(incumbent.id) is None
    assert store.binding(newcomer.id) == "Ctrl+Alt+H"

    Config._instance = None
    restarted = ShortcutStore(Config())
    restarted.register_plugin("z.existing", "Existing", [incumbent])
    restarted.register_plugin("a.new", "New", [newcomer])
    assert restarted.binding(incumbent.id) is None
    assert restarted.binding(newcomer.id) == "Ctrl+Alt+H"


def test_failed_default_arbitration_does_not_publish_catalog_or_dispatch(temp_data_dir, qtbot, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from src.plugins.runtime.commands import CommandTable
    from src.plugins.runtime.shortcuts import PluginShortcutHost
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    window = QWidget()
    qtbot.addWidget(window)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    incumbent = colliding_command("z.existing")
    settings.register_plugin("z.existing", "Existing", [incumbent])

    plugin = write_shortcut_plugin(
        temp_data_dir / "plugins",
        id="a.new",
        name="New",
        contributes={
            "commands": [{"id": "a.new.hello", "title": "New"}],
            "shortcuts": [{"command": "a.new.hello", "default": "Ctrl+Alt+H", "context": "window"}],
        },
    )
    manifest = parse_manifest(plugin)
    commands = CommandTable()
    commands.register("a.new", "a.new.hello", lambda: None)
    host = PluginShortcutHost(settings, dispatcher)
    monkeypatch.setattr(settings.store.config, "set_atomic", lambda *_args: False)

    with pytest.raises(RuntimeError):
        host.register(manifest, commands, lambda _owner, text: text)

    assert "a.new.hello" not in settings.store.command_by_id
    assert "a.new.hello" not in dispatcher._plugin_actions
    assert settings.binding(incumbent.id) == "Ctrl+Alt+H"


@pytest.mark.parametrize(
    "shortcut",
    [
        {"command": "foreign.command", "default": "Ctrl+Alt+L"},
        {"command": "test.sample.command", "default": "Alt+F4"},
        {"command": "test.sample.command", "default": "A"},
        {"command": "test.sample.command", "default": "Ctrl+Alt+L", "context": "text"},
    ],
)
def test_invalid_or_foreign_shortcut_is_rejected_during_manifest_parse(tmp_path, shortcut):
    directory = write_shortcut_plugin(tmp_path)
    path = directory / "plugin.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["contributes"]["shortcuts"] = [shortcut]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ManifestError):
        parse_manifest(directory)


def test_shortcut_permission_is_required(tmp_path):
    directory = write_shortcut_plugin(tmp_path, permissions=["mods:read"])
    with pytest.raises(ManifestError):
        parse_manifest(directory)


def test_runtime_shortcuts_dispatch_and_cleanup_with_the_plugin(env, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    from src.plugins.runtime.shortcuts import PluginShortcutHost
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    manager, directory, _ = env
    write_shortcut_plugin(directory)
    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    button = QPushButton("Focus")
    layout.addWidget(button)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    manager.set_shortcut_host(PluginShortcutHost(settings, dispatcher))
    record = activate(manager)
    called = []
    record.context.commands.register("test.sample.command", lambda: called.append("called"))
    window.show()
    window.activateWindow()
    button.setFocus()
    qtbot.waitUntil(button.hasFocus)
    modifier = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    qtbot.keyClick(button, Qt.Key.Key_L, modifier)
    assert called == ["called"]
    assert settings.save_binding("test.sample.command", "Ctrl+Alt+M")
    qtbot.keyClick(button, Qt.Key.Key_L, modifier)
    qtbot.keyClick(button, Qt.Key.Key_M, modifier)
    assert called == ["called", "called"]
    manager.disable(record.id)
    qtbot.keyClick(button, Qt.Key.Key_M, modifier)
    assert called == ["called", "called"]
    assert "test.sample.command" not in settings.bindings
    assert manager.enable(record.id)
    assert settings.binding("test.sample.command") == "Ctrl+Alt+M"


@pytest.mark.parametrize("operation", ["reload", "uninstall", "command_error"])
def test_runtime_shortcut_teardown_tracks_plugin_lifecycle(env, qtbot, operation):
    from PySide6.QtWidgets import QWidget

    from src.plugins.runtime.registry import PluginState
    from src.plugins.runtime.shortcuts import PluginShortcutHost
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    manager, directory, _ = env
    write_shortcut_plugin(directory)
    window = QWidget()
    qtbot.addWidget(window)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    manager.set_shortcut_host(PluginShortcutHost(settings, dispatcher))
    record = activate(manager)
    command_id = "test.sample.command"
    old_action = dispatcher._plugin_actions[command_id][1]

    if operation == "reload":
        assert manager.reload(record.id)
        assert command_id in settings.bindings
        assert dispatcher._plugin_actions[command_id][1] is not old_action
    elif operation == "uninstall":
        assert manager.uninstall(record.id)
        assert command_id not in settings.bindings
        assert command_id not in dispatcher._plugin_actions
    else:
        record.context.commands.register(command_id, lambda: 1 / 0)
        manager.execute_command(command_id)
        assert manager.get(record.id).state is PluginState.ERROR
        assert command_id not in settings.bindings
        assert command_id not in dispatcher._plugin_actions


def test_settings_page_tracks_dynamic_plugin_commands(temp_data_dir, qtbot):
    from src.ui.pages.shortcuts import ShortcutsPage
    from src.ui.shortcut_manager import ShortcutSettings

    settings = ShortcutSettings()
    page = ShortcutsPage(settings)
    qtbot.addWidget(page)
    command = shortcut_command()
    settings.register_plugin("test.sample", "Example plugin", [command])
    assert command.id in page.binding_buttons
    page.open_editor(command.id)
    assert page._dialog.command.id == command.id
    settings.unregister_plugin("test.sample")
    assert command.id not in page.binding_buttons
    assert page._dialog is None
