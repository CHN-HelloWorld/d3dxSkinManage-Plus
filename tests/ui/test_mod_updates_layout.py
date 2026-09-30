from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QScrollArea, QWidget

from src.services.gamebanana.models import GbModUpdateEntry
from src.ui import theme
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


def show_dialog(qtbot, infos=None, **kwargs):
    dialog = ModUpdatesDialog(
        infos if infos is not None else [_info("m1"), _info("m2", "示例角色午西装")], enable_actions=True, **kwargs
    )
    keep_dialog(qtbot, dialog)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    return dialog


def row(dialog, mid):
    return next(widget for widget in dialog.findChildren(QWidget, "updateRow") if widget.property("modId") == mid)


def button(dialog, name, mid=None):
    return next(
        widget for widget in dialog.findChildren(QPushButton, name) if mid is None or widget.property("modId") == mid
    )


def test_search_filters_rows_without_changing_bulk_update_scope(qtbot):
    dialog = show_dialog(qtbot)
    search = dialog.findChild(QLineEdit, "updateSearch")
    assert search is not None, "A 方案缺少搜索框"
    search.setText("示例角色午")
    assert row(dialog, "m1").isHidden()
    assert row(dialog, "m2").isVisibleTo(dialog)
    assert dialog.pending_ids() == ["m1", "m2"]
    search.setText("不存在的 Mod")
    assert dialog.findChild(QLabel, "updateEmptyTitle").isVisibleTo(dialog)
    search.clear()
    assert row(dialog, "m1").isVisibleTo(dialog)
    assert row(dialog, "m2").isVisibleTo(dialog)


def test_log_expansion_preserves_literal_name_and_does_not_download(qtbot):
    info = _info("m1", '<b>A & B</b> "服装"')
    info.changelog = [
        GbModUpdateEntry(
            title="修复 <材质>",
            date_added_ts=2000,
            description_html="<p>保留 &lt;config&gt; 字段</p><ul><li>修复阴影</li></ul>"
            '<script>should-not-be-shown()</script><img src="https://example.invalid/a.png">',
            tags=["修复"],
            version="v1.3",
        )
    ]
    dialog = show_dialog(qtbot, [info])
    name = dialog.findChild(QLabel, "updateName")
    assert name.text() == '<b>A & B</b> "服装"'
    assert name.textFormat() == Qt.TextFormat.PlainText
    requested = []
    dialog.update_requested.connect(requested.append)
    expand = button(dialog, "updateSummary")
    expand.click()
    log = dialog.findChild(QLabel, "updateLogText")
    assert log.isVisibleTo(dialog)
    assert "保留 <config> 字段" in log.text() and "修复阴影" in log.text()
    assert "should-not-be-shown" not in log.text()
    assert requested == []
    expand.click()
    assert not log.isVisibleTo(dialog)


def test_menu_routes_each_action_to_its_own_mod(qtbot):
    dialog = show_dialog(qtbot)
    more = button(dialog, "updateMore", "m2")
    menu = more.menu()
    assert menu is not None, "行内的低频操作应收纳进更多菜单"
    received = []
    dialog.open_gb_requested.connect(lambda mid: received.append(("open", mid)))
    dialog.ignore_version_requested.connect(lambda mid: received.append(("version", mid)))
    dialog.ignore_mod_requested.connect(lambda mid: received.append(("mod", mid)))
    actions = {action.objectName(): action for action in menu.actions() if not action.isSeparator()}
    actions["updateOpenSource"].trigger()
    actions["updateIgnoreVersion"].trigger()
    actions["updateIgnoreMod"].trigger()
    assert received == [("open", "m2"), ("version", "m2"), ("mod", "m2")]
    assert all(
        not actions[name].icon().isNull() for name in ("updateOpenSource", "updateIgnoreVersion", "updateIgnoreMod")
    )


def test_update_state_blocks_duplicates_and_reenables_failed_item(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", {"phase": "preparing", "error": ""})
    assert not button(dialog, "updateSingle", "m1").isEnabled()
    assert not dialog._update_all_btn.isEnabled()
    dialog.set_update_failed("m1", "网络连接中断")
    assert button(dialog, "updateSingle", "m1").isEnabled()
    assert dialog._update_all_btn.isEnabled()
    dialog.set_task_snapshot("m1", {"phase": "preparing", "error": ""})
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    dialog.set_updates([_info("m2", "示例角色午西装")], failures={})
    assert row(dialog, "m1").isVisibleTo(dialog), "本次已完成的更新应先保留成功提示"
    assert not button(dialog, "updateSingle", "m1").isEnabled()
    assert dialog.pending_ids() == ["m2"]
    dialog.set_task_snapshot("m2", {"phase": "done", "error": ""})
    assert dialog._update_all_btn.isEnabled()
    assert dialog._update_all_btn.accessibleName() == "完成"


def test_recheck_keeps_search_and_expansion_and_reports_failures(qtbot):
    dialog = show_dialog(qtbot)
    search = dialog.findChild(QLineEdit, "updateSearch")
    assert search is not None
    search.setText("示例角色丁")
    button(dialog, "updateSummary", "m1").click()
    dialog.set_checking(True)
    assert not dialog._recheck_btn.isEnabled()
    changed = _info("m1", new="v1.5")
    dialog.set_updates([changed, _info("m2", "示例角色午西装")], failures={"m3": "连接超时"})
    dialog.set_checking(False)
    assert search.text() == "示例角色丁"
    assert button(dialog, "updateSummary", "m1").isChecked()
    assert row(dialog, "m2").isHidden()
    assert dialog._check_panel.detail.isVisibleTo(dialog)
    assert dialog._recheck_btn.isEnabled()


@pytest.mark.parametrize("dark", [False, True])
def test_long_content_stays_inside_scroll_area_and_footer_stays_clickable(qtbot, dark):
    previous = theme.current_theme()
    theme.set_theme("dark" if dark else "light")
    try:
        info = _info("m1", "VeryLongUnbrokenModName" * 35)
        info.latest_file = replace(info.latest_file, size_bytes=2 * 1024 * 1024)
        info.changelog = [
            GbModUpdateEntry(
                title="长日志",
                date_added_ts=2000,
                description_html="<p>" + "LongUnbrokenDescription" * 160 + "</p>",
                tags=[],
            )
        ]
        dialog = show_dialog(qtbot, [info] + [_info(str(i)) for i in range(8)])
        dialog.resize(620, 430)
        expand = button(dialog, "updateSummary", "m1")
        expand.click()
        qtbot.wait(20)
        scroll = dialog.findChild(QScrollArea, "updatesScroll")
        assert scroll is not None
        assert scroll.horizontalScrollBar().maximum() == 0
        assert scroll.verticalScrollBar().maximum() > 0
        action = dialog._update_all_btn
        action_rect = QRect(action.mapTo(dialog, QPoint()), action.size())
        assert dialog.rect().contains(action_rect)
        assert action_rect.top() >= scroll.mapTo(dialog, scroll.rect().bottomLeft()).y()
        assert dialog.width() == 620 and dialog.height() == 430
        assert not action.icon().isNull()
    finally:
        theme.set_theme(previous)


def test_failed_empty_check_does_not_claim_everything_is_current(qtbot):
    dialog = show_dialog(qtbot, [], failures={"m1": "连接失败"})
    empty = dialog.findChild(QLabel, "updateEmptyTitle")
    assert empty is not None
    assert "最新" not in empty.text()
    assert dialog._check_panel.detail.isVisibleTo(dialog)
    assert not dialog._update_all_btn.isEnabled()


def test_styles_preserve_readable_action_sizes(qtbot):
    dialog = show_dialog(qtbot)
    qtbot.wait(20)
    assert dialog._update_all_btn.width() >= 132
    assert dialog._update_all_btn.height() >= 36
    assert button(dialog, "updateSingle", "m1").width() >= 66
    assert button(dialog, "updateMore", "m1").width() >= 30
    dialog.set_updates([])
    qtbot.wait(20)
    assert dialog._empty_action.width() >= 100
    assert dialog._empty_action.height() >= 34


@pytest.mark.parametrize("width", [420, 620, 848])
def test_long_versions_keep_all_row_actions_inside_the_window(qtbot, width):
    info = _info(
        "m1", cur="Complete_Costume_Collection_2026_September_1", new="Complete_Costume_Collection_2026_September_2"
    )
    dialog = show_dialog(qtbot, [info])
    dialog.resize(width, 430)
    qtbot.wait(20)
    assert dialog._scroll.horizontalScrollBar().maximum() == 0
    for name in ("updateSingle", "updateMore"):
        action = button(dialog, name, "m1")
        assert dialog.rect().contains(QRect(action.mapTo(dialog, QPoint()), action.size()))


def test_application_theme_keeps_summary_and_footer_dimensions(qtbot, qapp):
    previous = qapp.styleSheet()
    try:
        qapp.setStyleSheet(theme.render_qss("light"))
        dialog = show_dialog(qtbot)
        qtbot.wait(20)
        summary = button(dialog, "updateSummary", "m1")
        assert summary.contentsRect().height() >= summary.fontMetrics().height()
        assert dialog._update_all_btn.size().width() >= 132
        assert dialog._update_all_btn.size().height() == 36
    finally:
        qapp.setStyleSheet(previous)


@pytest.mark.parametrize("failures", [{}, {"m1": "连接失败"}])
def test_small_empty_window_keeps_copy_separate_and_recheck_reachable(qtbot, failures):
    dialog = show_dialog(qtbot, [], failures=failures)
    dialog.resize(420, 340)
    qtbot.wait(20)
    parts = (dialog._empty_icon, dialog._empty_title, dialog._empty_description, dialog._empty_action)
    for previous, current in zip(parts, parts[1:]):
        before = QRect(previous.mapTo(dialog, QPoint()), previous.size())
        after = QRect(current.mapTo(dialog, QPoint()), current.size())
        assert before.bottom() < after.top()
    page = dialog._stack.currentWidget()
    if isinstance(page, QScrollArea):
        page.ensureWidgetVisible(dialog._empty_action)
        qtbot.wait(20)
        action = QRect(dialog._empty_action.mapTo(page.viewport(), QPoint()), dialog._empty_action.size())
        assert page.viewport().rect().contains(action)
