import gc
import weakref
from threading import Event, get_ident

from PySide6.QtCore import QTimer

from src.core.mod_variant_manager import ModVariantManager
from tests.core.test_named_variant_manager import save
from tests.core.test_variant_preset_switch import _KEY, _user_keys
from tests.core.test_variant_preset_switch import env as env
from tests.ui.test_mod_variant_integration import variant_host as variant_host


def test_switch_keeps_gui_responsive_during_declaration_scan(env, variant_host, qtbot, monkeypatch):
    window, current = variant_host
    mod = env["mod"]
    first = save(env, "晨光", "2")
    save(env, "夜色", "4")
    controller = window._variant_controller
    controller.refresh(mod.mod_id)
    qtbot.waitUntil(lambda: not controller._busy)
    owner = get_ident()
    released = Event()
    responsive, threads = [], []
    original = ModVariantManager.build_index

    def slow_scan(service, *args, **kwargs):
        threads.append(get_ident())
        if not responsive:
            responsive.append(released.wait(0.5))
        return original(service, *args, **kwargs)

    monkeypatch.setattr(ModVariantManager, "build_index", slow_scan)
    QTimer.singleShot(0, released.set)
    controller.apply(mod.mod_id, first.variant_id)
    qtbot.waitUntil(lambda: not controller._busy)

    assert responsive == [True], "the Qt event loop stalled during variant file IO"
    assert threads and all(thread != owner for thread in threads)
    assert current.mod_variants[mod.mod_id] == first.variant_id
    assert _user_keys(env["user_ini"])[_KEY] == "2"


def test_switch_result_does_not_reopen_a_closed_mod_panel(env, variant_host, qtbot, monkeypatch):
    window, current = variant_host
    mod = env["mod"]
    first = save(env, "晨光", "2")
    save(env, "夜色", "4")
    controller = window._variant_controller
    controller.refresh(mod.mod_id)
    qtbot.waitUntil(lambda: not controller._busy)
    page = window._overview_page
    released = Event()
    original = ModVariantManager.build_index

    def slow_scan(service, *args, **kwargs):
        released.wait(0.5)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(ModVariantManager, "build_index", slow_scan)
    controller.apply(mod.mod_id, first.variant_id)
    page._hide_panel()
    released.set()
    qtbot.waitUntil(lambda: not controller._busy)

    assert page._variant_panel.mod_id == ""
    assert current.mod_variants[mod.mod_id] == first.variant_id
    assert _user_keys(env["user_ini"])[_KEY] == "2"


def test_finished_switch_releases_captured_models(env, variant_host, qtbot, monkeypatch):
    window, _ = variant_host
    first = save(env, "晨光", "2")
    controller = window._variant_controller
    qtbot.waitUntil(lambda: not controller._busy)
    released = Event()
    original = ModVariantManager.build_index

    def slow_scan(service, *args, **kwargs):
        released.wait(2)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(ModVariantManager, "build_index", slow_scan)
    controller.apply(env["mod"].mod_id, first.variant_id)
    snapshot = weakref.ref(controller._worker.task)
    released.set()
    qtbot.waitUntil(lambda: not controller._busy)
    gc.collect()

    assert snapshot() is None, "finished workers retained a full variant/library snapshot"


def test_close_waits_asynchronously_for_an_inflight_write(env, variant_host, qtbot, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from src.ui.main_window import MainWindow

    window, current = variant_host
    first = save(env, "晨光", "2")
    save(env, "夜色", "4")
    controller = window._variant_controller
    qtbot.waitUntil(lambda: not controller._busy)
    entered, released = Event(), Event()
    original = ModVariantManager.build_index

    def slow_scan(service, *args, **kwargs):
        entered.set()
        released.wait(2)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(ModVariantManager, "build_index", slow_scan)
    controller.apply(env["mod"].mod_id, first.variant_id)
    try:
        qtbot.waitUntil(entered.is_set)
        event = QCloseEvent()
        MainWindow.closeEvent(window, event)
        assert not event.isAccepted()
        assert controller.running
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: bool(heartbeat))
    finally:
        released.set()
    qtbot.waitUntil(lambda: not controller.running)
    assert current.mod_variants[env["mod"].mod_id] == first.variant_id
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert env["mm"]._library_protection.snapshot() == (set(), set())
