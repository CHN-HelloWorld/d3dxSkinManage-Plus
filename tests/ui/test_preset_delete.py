from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QTimer

from tests.helpers import apply_preset
from tests.helpers import registered_games as registered_games
from tests.ui.test_disable_preset_lifecycle import _manager_window

pytestmark = pytest.mark.usefixtures("native_qt_teardown")


def test_declining_preset_delete_does_not_change_any_state(qtbot, tmp_path, monkeypatch):
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, _ = _make_enabled_mods(tmp_path, 1)
    try:
        target = mm.preset_manager.create_preset("game_a", "Keep me")
        window = _manager_window(qtbot, mm)
        before = mm.preset_manager._presets_file.read_bytes()
        seen = []
        monkeypatch.setattr(
            "src.ui.dialogs.preset_progress.confirm_preset_delete",
            lambda parent, presets, current: seen.extend(p.id for p in presets) or False,
        )
        window._on_preset_delete_batch([target.id])
        assert seen == [target.id]
        assert mm.preset_manager._presets_file.read_bytes() == before
        assert mm.preset_manager.get_preset(target.id) is target
    finally:
        mm.stop_ini_sync()


def test_deleting_current_preset_switches_and_cleans_in_background(qtbot, tmp_path, monkeypatch):
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, _ = _make_enabled_mods(tmp_path, 1)
    pm = mm.preset_manager
    default = pm.get_current("game_a")
    target = pm.create_preset("game_a", "Delete me", snapshot=default.character_mods)
    apply_preset(pm, target.id)
    default.character_mods = {}
    default.mod_variants = {}
    pm._save()
    window = _manager_window(qtbot, mm)
    monkeypatch.setattr("src.ui.dialogs.preset_progress.confirm_preset_delete", lambda *args: True)
    errors = []
    monkeypatch.setattr("src.ui.main_window.QMessageBox.critical", lambda *args: errors.append(args))
    entered, released = Event(), Event()
    original = mm._storage.remove_deploy_path

    def slow_remove(path):
        entered.set()
        assert released.wait(5)
        return original(path)

    monkeypatch.setattr(mm._storage, "remove_deploy_path", slow_remove)
    body_before = Path(mods[0].path).read_bytes()
    try:
        window._on_preset_delete_batch([target.id])
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: entered.is_set() and bool(heartbeat), timeout=5000)
        assert window._preset_delete_progress.isVisible()
        assert not window._game_page.isEnabled()
        released.set()
        qtbot.waitUntil(lambda: getattr(window, "_preset_delete_worker", None) is None, timeout=5000)
        assert not errors
        assert pm.get_preset(target.id) is None
        assert pm.get_current("game_a").id == default.id
        assert Path(mods[0].path).read_bytes() == body_before
        assert not mm.get_mod(mods[0].mod_id).deployed_path
    finally:
        released.set()
        qtbot.waitUntil(
            lambda: window._preset_apply_worker is None and getattr(window, "_preset_delete_worker", None) is None,
            timeout=5000,
        )
        mm.stop_ini_sync()
