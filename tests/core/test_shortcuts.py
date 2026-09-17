import json

import pytest


def make_store():
    from src.core.config import Config
    from src.core.shortcuts import ShortcutStore

    return ShortcutStore(Config())


def test_existing_commands_keep_their_default_bindings(temp_data_dir):
    from src.core.shortcuts import COMMANDS

    expected = {
        "edit.copy": "Ctrl+C",
        "edit.cut": "Ctrl+X",
        "edit.paste": "Ctrl+V",
        "edit.select_all": "Ctrl+A",
        "edit.delete": "Delete",
        "view.refresh": "F5",
        "mod.import": "Ctrl+I",
    }
    commands = {command.id: command for command in COMMANDS}
    assert {key: commands[key].default for key in expected} == expected
    assert "preview" not in commands["edit.paste"].contexts
    assert "preset" not in commands["edit.delete"].contexts


def test_saved_custom_binding_has_priority_over_a_new_default(temp_data_dir):
    from src.core.config import Config

    config = Config()
    config.set_atomic("shortcuts.overrides", {"mod.import": "Ctrl+F"})
    before = config._config_path.read_bytes()
    store = make_store()
    assert store.binding("mod.import") == "Ctrl+F"
    assert store.bindings.get("view.search", "missing") is None
    assert config._config_path.read_bytes() == before


@pytest.mark.parametrize(
    "key, expected",
    [
        ("shift+ctrl+k", "Ctrl+Shift+K"),
        ("ALT+CTRL+Delete", "Ctrl+Alt+Delete"),
        ("Ctrl+,", "Ctrl+,"),
        ("F12", "F12"),
        ("Ctrl+PgUp", "Ctrl+PageUp"),
    ],
)
def test_binding_normalization(key, expected):
    from src.core.shortcuts import normalize_binding

    assert normalize_binding(key) == expected


@pytest.mark.parametrize(
    "key",
    [
        "A",
        "Shift+A",
        "1",
        "Ctrl",
        "Ctrl+Ctrl+A",
        "Ctrl+A, Ctrl+B",
        "Meta+K",
        "Alt+Tab",
        "Alt+F4",
        "Ctrl+Alt+Delete",
        "Ctrl+Shift+Esc",
        "Esc",
        "Enter",
        "Shift+Tab",
        "Space",
        "Delete",
        "F13",
    ],
)
def test_window_bindings_reject_reserved_or_typing_keys(key):
    from src.core.shortcuts import COMMAND_BY_ID, validate_binding

    assert validate_binding(key, COMMAND_BY_ID["view.refresh"])


@pytest.mark.parametrize("key", ["Ctrl+Shift+K", "Alt+1", "F1", "F12", "Ctrl+,", "Ctrl+PageUp"])
def test_supported_single_combination(key):
    from src.core.shortcuts import COMMAND_BY_ID, validate_binding

    assert validate_binding(key, COMMAND_BY_ID["mod.import"]) == ""


def test_conflicts_include_shared_text_context(temp_data_dir):
    store = make_store()
    assert [c.id for c in store.conflicts("edit.copy", "Ctrl+X")] == ["edit.cut"]
    assert [c.id for c in store.conflicts("view.refresh", "Ctrl+V")] == ["edit.paste"]
    assert store.conflicts("edit.paste", "Ctrl+V") == []


def test_replacement_is_one_persisted_transaction_and_undo_preserves_other_settings(temp_data_dir):
    from src.core.config import Config

    store = make_store()
    before = store.bindings
    assert not store.save_binding("view.refresh", "Ctrl+I")
    assert store.bindings == before
    assert store.save_binding("view.refresh", "Ctrl+I", replace=True)
    assert store.binding("view.refresh") == "Ctrl+I"
    assert store.binding("mod.import") is None
    saved = json.loads((temp_data_dir / "config/settings.json").read_text(encoding="utf-8"))
    assert saved["shortcuts"]["overrides"] == {"view.refresh": "Ctrl+I", "mod.import": None}
    Config().set("general.language", "en-US")
    Config().reload()
    assert make_store().bindings == store.bindings
    assert store.restore(before)
    assert store.bindings == before
    assert Config().get("general.language") == "en-US"


def test_reset_restores_all_shortcuts_without_touching_other_settings(temp_data_dir):
    from src.core.config import Config

    store = make_store()
    before = store.bindings
    assert store.save_binding("edit.copy", "Ctrl+Shift+C")
    assert store.custom_count == 1
    Config().set("mods.delete_method", "recycle_bin")
    assert store.restore_defaults()
    assert store.bindings == before
    assert store.custom_count == 0
    assert Config().get("mods.delete_method") == "recycle_bin"


@pytest.mark.parametrize("operation", ["save", "reset", "undo"])
def test_failed_write_keeps_disk_and_all_effective_bindings(temp_data_dir, monkeypatch, operation):
    from src.core import config as config_module

    store = make_store()
    initial = store.bindings
    assert store.save_binding("edit.copy", "Ctrl+Shift+C")
    before = store.bindings
    path = temp_data_dir / "config/settings.json"
    disk = path.read_bytes()

    def denied(*_args):
        raise PermissionError("read-only settings")

    monkeypatch.setattr(config_module, "write_json_atomic", denied)
    if operation == "save":
        result = store.save_binding("view.refresh", "Ctrl+I", replace=True)
    elif operation == "reset":
        result = store.restore_defaults()
    else:
        result = store.restore(initial)
    assert not result
    assert store.bindings == before
    assert path.read_bytes() == disk


def test_invalid_saved_values_do_not_break_other_bindings_or_rewrite_file(temp_data_dir):
    from src.core.config import Config

    Config().set(
        "shortcuts.overrides",
        {
            "edit.copy": "Ctrl+Shift+C",
            "edit.cut": 99,
            "edit.paste": "Ctrl+Alt+Delete",
            "mod.import": None,
        },
    )
    path = temp_data_dir / "config/settings.json"
    before = path.read_bytes()
    store = make_store()
    assert store.binding("edit.copy") == "Ctrl+Shift+C"
    assert store.binding("edit.cut") == "Ctrl+X"
    assert store.binding("edit.paste") == "Ctrl+V"
    assert store.binding("mod.import") is None
    assert path.read_bytes() == before


def test_manually_duplicated_binding_cannot_trigger_two_commands(temp_data_dir):
    from src.core.config import Config

    Config().set("shortcuts.overrides", {"view.refresh": "Ctrl+I"})
    store = make_store()
    assert sum(value == "Ctrl+I" for value in store.bindings.values()) == 1


def test_unbinding_is_not_an_edit_action(temp_data_dir):
    store = make_store()
    before = store.bindings
    assert not store.save_binding("mod.import", None)
    assert store.bindings == before
