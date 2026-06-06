from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from src.core.mod_variant_store import ModVariantStore
from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.ui.test_mod_variant_panel import row
from tests.ui.test_mod_variant_wiring import _VariantHost


@pytest.fixture
def variant_host(env, qtbot, native_qt_teardown):
    manager, mod, current = enable(env)
    window = _VariantHost(manager)
    qtbot.addWidget(window)
    window.resize(1100, 760)
    page = window._overview_page
    page.set_mod_manager(manager)
    manager.add_event_listener(window._on_core_event)
    page._selected_mod = manager.get_mod(mod.mod_id)
    page._selected_ids = {mod.mod_id}
    page._show_mod_panel(page._selected_mod)
    page._detail_tabs.setCurrentIndex(2)
    window.show()
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    yield window, current
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    manager._event_listeners.remove(window._on_core_event)
    window.close()


def test_save_update_delete_controls_commit_named_variant_and_references(env, variant_host, qtbot):
    window, current = variant_host
    manager, mod = env["mm"], env["mod"]
    body_before = Path(mod.path).read_bytes()
    page = window._overview_page
    controller = window._variant_controller
    _write_user_ini(env["user_ini"], "2")
    manager.variants.collect(trigger="ui_test")
    qtbot.waitUntil(lambda: not controller._busy)
    page._variant_panel.save_button.click()
    dialog = controller._dialog
    dialog.name_input.setText("清晨")
    dialog.confirm_button.click()
    qtbot.waitUntil(lambda: not controller._busy)
    assert controller._dialog is None
    saved = manager.variants.list_variants(mod.mod_id)[0]
    assert saved.name == "清晨" and saved.variables[0].value == "2"
    assert current.mod_variants[mod.mod_id] == saved.variant_id
    assert "清晨" in page._panel_variant_label._full_text
    copy = manager.preset_manager.create_preset(
        "g1", "摄影组合", snapshot=current.character_mods, copied_from=current.id
    )
    _write_user_ini(env["user_ini"], "4")
    manager.variants.collect(trigger="ui_test")
    qtbot.waitUntil(lambda: not controller._busy)
    page._variant_panel.update_requested.emit(mod.mod_id, saved.variant_id)
    dialog = controller._dialog
    assert {label.text() for label in dialog.findChildren(QLabel, "variantReference")} == {current.name, copy.name}
    dialog.confirm_button.click()
    qtbot.waitUntil(lambda: not controller._busy)
    assert ModVariantStore(mod.path).get(saved.variant_id).variables[0].value == "4"
    page._variant_panel.delete_requested.emit(mod.mod_id, saved.variant_id)
    dialog = controller._dialog
    assert not dialog.findChildren(QLabel, "variantReference")
    dialog.confirm_button.click()
    qtbot.waitUntil(lambda: not controller._busy)
    assert controller._dialog is None
    assert not manager.variants.list_variants(mod.mod_id)
    assert current.mod_variants[mod.mod_id] is None and copy.mod_variants[mod.mod_id] is None
    assert _override_value(mod) == "0"
    assert "Mod 默认" in page._panel_variant_label._full_text
    assert page._detail_tabs.currentIndex() == 2
    assert Path(mod.path).read_bytes() == body_before


def test_real_incomplete_capture_requires_explicit_default_button(env, variant_host, qtbot):
    window, current = variant_host
    manager, mod = env["mm"], env["mod"]
    env["user_ini"].write_text("[Constants]\n", encoding="utf-8")
    manager.variants.collect(trigger="ui_test")
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    page = window._overview_page
    assert page._variant_panel._state["can_save"] is False
    assert page._variant_panel.save_button.isEnabled()
    page._variant_panel.save_button.click()
    dialog = window._variant_controller._dialog
    dialog.name_input.setText("默认副本")
    dialog.confirm_button.click()
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    assert not manager.variants.list_variants(mod.mod_id)
    assert dialog.defaults_button.isVisible()
    dialog.defaults_button.click()
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    saved = manager.variants.list_variants(mod.mod_id)[0]
    assert saved.variables[0].value == "0"
    assert current.mod_variants[mod.mod_id] == saved.variant_id


def test_left_click_switches_real_preferences_and_default_clears_them(env, variant_host, qtbot):
    window, current = variant_host
    manager, mod = env["mm"], env["mod"]
    body_before = Path(mod.path).read_bytes()
    _write_user_ini(env["user_ini"], "2")
    first = manager.variants.save_variant(mod.mod_id, "清晨")
    _write_user_ini(env["user_ini"], "4")
    manager.variants.save_variant(mod.mod_id, "夜色")
    window._variant_controller.refresh(mod.mod_id)
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    panel = window._overview_page._variant_panel

    qtbot.mouseClick(row(panel, first.variant_id).findChild(QPushButton, "variantSelect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    assert current.mod_variants[mod.mod_id] == first.variant_id
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert row(panel, first.variant_id).property("inUse") is True

    qtbot.mouseClick(row(panel, None).findChild(QPushButton, "variantSelect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window._variant_controller._busy)
    assert current.mod_variants[mod.mod_id] is None
    assert _KEY not in _user_keys(env["user_ini"])
    assert row(panel, None).property("inUse") is True
    assert _override_value(mod) == "0"
    assert Path(mod.path).read_bytes() == body_before
