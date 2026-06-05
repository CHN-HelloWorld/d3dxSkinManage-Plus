from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QPushButton

pytestmark = pytest.mark.usefixtures("temp_data_dir")


def variant(variant_id, name):
    return SimpleNamespace(
        variant_id=variant_id,
        name=name,
        updated_at="2026-10-19T10:00:00",
        created_at="2026-10-18T10:00:00",
        variables=(),
    )


def state(**changes):
    result = dict(
        mod_id="mod-one",
        enabled=True,
        selected_variant_id="first",
        status="clean",
        can_save=True,
        reason="",
        captured_at="2026-10-19T10:00:00",
        variable_count=3,
        confirmed_count=3,
    )
    result.update(changes)
    return result


def make_panel(qtbot, **changes):
    from src.ui.widgets.mod_variant_panel import ModVariantPanel

    panel = ModVariantPanel()
    qtbot.addWidget(panel)
    panel.resize(380, 360)
    panel.set_state(
        state(**changes),
        [variant("first", "晨光"), variant("other", "夜色")],
        reference_counts={"first": 2, "other": 1},
    )
    panel.show()
    return panel


def row(panel, variant_id):
    return next(
        widget
        for widget in panel.findChildren(QFrame)
        if widget.objectName() == "modVariantRow" and widget.property("variantId") == variant_id
    )


def action(menu, name):
    return menu.findChild(QAction, name)


def test_right_click_unselected_row_targets_it_without_selecting_or_applying(qtbot):
    panel = make_panel(qtbot)
    applied, renamed = QSignalSpy(panel.apply_requested), QSignalSpy(panel.rename_requested)
    target = row(panel, "other")
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5), target.mapToGlobal(QPoint(5, 5)))
    QApplication.sendEvent(target, event)
    menu = panel.findChild(QMenu, "acrylicMenu")
    assert menu is not None and menu.isVisible()
    action(menu, "variantRename").trigger()
    menu.close()
    assert [renamed.at(index) for index in range(renamed.count())] == [["mod-one", "other"]]
    assert applied.count() == 0
    assert row(panel, "first").property("selected") is True


def test_more_button_and_right_click_offer_same_actions(qtbot):
    panel = make_panel(qtbot)
    target = row(panel, "other")
    qtbot.mouseClick(target.findChild(QPushButton, "variantMore"), Qt.MouseButton.LeftButton)
    menu = panel.findChild(QMenu, "acrylicMenu")
    first = [(item.objectName(), item.isEnabled()) for item in menu.actions()]
    assert first[0] == ("variantApply", True)
    menu.close()
    QApplication.sendEvent(
        target, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5), target.mapToGlobal(QPoint(5, 5)))
    )
    menu = panel.findChild(QMenu, "acrylicMenu")
    assert [(item.objectName(), item.isEnabled()) for item in menu.actions()] == first
    menu.close()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("variant_id", ["other", None])
def test_clicking_row_requests_variant_switch(qtbot, enabled, variant_id):
    panel = make_panel(qtbot, enabled=enabled)
    applied = QSignalSpy(panel.apply_requested)
    qtbot.mouseClick(row(panel, variant_id).findChild(QPushButton, "variantSelect"), Qt.MouseButton.LeftButton)
    assert [applied.at(index) for index in range(applied.count())] == [["mod-one", variant_id]]
    assert row(panel, "first").property("inUse") is True


def test_disabled_mod_disables_save_update_but_allows_private_selection(qtbot):
    panel = make_panel(
        qtbot,
        enabled=False,
        can_save=False,
        status="unconfirmed",
        reason="工作状态尚未完整确认；请先在加载器中刷新保存并等待落盘",
    )
    assert not panel.notice.isVisible()
    assert not panel.findChild(QPushButton, "saveNewVariant").isEnabled()
    selected, enabled = QSignalSpy(panel.apply_requested), QSignalSpy(panel.enable_requested)
    menu = panel.menu_for_variant("other")
    assert not action(menu, "variantUpdate").isEnabled()
    assert all(action(menu, name).isEnabled() for name in ("variantRename", "variantCopy", "variantDelete"))
    action(menu, "variantApply").trigger()
    action(menu, "variantEnable").trigger()
    assert [selected.at(index) for index in range(selected.count())] == [["mod-one", "other"]]
    assert [enabled.at(index) for index in range(enabled.count())] == [["mod-one", "other"]]


def test_default_row_has_no_named_variant_management(qtbot):
    panel = make_panel(qtbot)
    menu = panel.menu_for_variant(None)
    assert action(menu, "variantApply") is not None
    assert all(
        action(menu, name) is None for name in ("variantUpdate", "variantRename", "variantCopy", "variantDelete")
    )


def test_clearing_context_closes_menu_and_invalidates_old_actions(qtbot):
    panel = make_panel(qtbot)
    deleted = QSignalSpy(panel.delete_requested)
    menu = panel.menu_for_variant("other")
    delete = action(menu, "variantDelete")
    menu.popup(panel.mapToGlobal(QPoint(0, 0)))
    panel.clear_context()
    assert not menu.isVisible()
    delete.trigger()
    assert deleted.count() == 0


def test_unchanged_refresh_keeps_selected_row_and_scroll(qtbot):
    panel = make_panel(qtbot)
    variants = [variant(str(i), f"Variant {i}") for i in range(24)]
    panel.set_state(state(selected_variant_id="0"), variants)
    selected = row(panel, "23")
    qtbot.mouseClick(selected.findChild(QPushButton, "variantSelect"), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: panel.scroll_area.verticalScrollBar().maximum() > 0)
    panel.scroll_area.verticalScrollBar().setValue(210)
    panel.set_state(state(selected_variant_id="23", status="modified"), variants)
    assert row(panel, "23") is selected
    assert selected.property("selected") is True
    assert panel.scroll_area.verticalScrollBar().value() == 210


def test_clicking_busy_or_stale_row_cannot_switch_variant(qtbot):
    panel = make_panel(qtbot)
    applied = QSignalSpy(panel.apply_requested)
    button = row(panel, "other").findChild(QPushButton, "variantSelect")
    panel.set_busy(True)
    button.click()
    panel.clear_context()
    button.click()
    assert applied.count() == 0


def test_unconfirmed_values_can_open_save_for_explicit_recheck(qtbot):
    panel = make_panel(qtbot, can_save=False, status="unconfirmed", confirmed_count=1, reason="工作状态尚未完整确认")
    assert panel.notice.isVisible()
    requested = QSignalSpy(panel.save_requested)
    qtbot.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    assert [requested.at(index) for index in range(requested.count())] == [["mod-one"]]
    assert action(panel.menu_for_variant("other"), "variantUpdate").isEnabled()


def test_no_variables_and_read_failure_have_distinct_empty_states(qtbot):
    panel = make_panel(qtbot)
    panel.set_state(state(variable_count=0, can_save=False, reason="此 Mod 没有可保存的持久化数值变量"), [])
    assert panel._info_button.isVisible()
    assert "暂无可保存" in panel._empty_label.text()
    assert not panel.save_button.isVisible()
    panel.set_state(state(variable_count=0, can_save=False, reason="无法读取 Mod 配置文件"), [])
    assert panel.notice.isVisible()
    assert "无法读取" in panel.notice.text()
    assert not panel._info_button.isVisible()
