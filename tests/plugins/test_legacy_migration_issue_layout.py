"""验证问题树与可滚动详情布局，使用内存数据而不访问真实来源。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard, select_mod, widget_rect


@pytest.fixture
def issue_wizard(load_migration, migration_context, migration_host):
    """使用真实宿主对话框和样式，配合内存迁移资源库。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    yield dialog, wizard
    plugin.deactivate()


def settle_layout(qapp):
    """处理布局事件，等待分隔器和换行详情标签完成尺寸调整。"""
    for cycle in range(6):
        qapp.processEvents()


@pytest.mark.parametrize("width", [680, 940, 1100])
@pytest.mark.parametrize("name", ["W" * 80, "😀外观" * 40], ids=["long-name", "unicode-name"])
def test_issue_details_preserve_long_names_and_original_diagnostics(
    issue_wizard, load_migration, migration_context, qtbot, qapp, width, name
):
    dialog, wizard = issue_wizard
    legacy = load_migration("legacy")
    issues = tuple(
        legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", name)
        for issue_number in range(12)
    )
    scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=issues))
    dialog.resize(width, 850)
    settle_layout(qapp)
    qtbot.waitUntil(lambda: wizard.tree_model.issue_index(11).isValid())
    for row in (0, 11):
        index = wizard.tree_model.issue_index(row)
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(index))
        settle_layout(qapp)
        assert wizard.detail_title.text() == name
        assert wizard.detail_title.wordWrap()
        assert wizard.detail_text.wordWrap()
        assert wizard.detail_text.text() == issues[row].message
        assert wizard.detail_scroll.verticalScrollBar().value() == 0
    assert wizard.detail_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert not wizard.detail_scroll.horizontalScrollBar().isVisible()
    assert wizard.controller.scan.issues == issues
    assert not migration_context.mods.imports


def test_one_tall_issue_can_scroll_to_its_diagnostic(issue_wizard, load_migration, migration_context, qtbot, qapp):
    dialog, wizard = issue_wizard
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", "旧版条目名称 " * 120)
    scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
    dialog.resize(900, 850)
    settle_layout(qapp)
    wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
    view = wizard.detail_scroll
    qtbot.waitUntil(lambda: view.verticalScrollBar().maximum() > 0)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    settle_layout(qapp)
    text_rect = widget_rect(wizard.detail_text, view.viewport())
    assert text_rect.bottom() <= view.viewport().rect().bottom()
    assert text_rect.bottom() > 0
    assert not view.horizontalScrollBar().isVisible()
    assert wizard.detail_title.text() == issue.name
    assert "修改" in wizard.detail_text.text()
    assert not migration_context.mods.imports


@pytest.mark.parametrize("description_lines", [0, 60])
def test_wide_issue_panel_keeps_selected_detail_title_in_the_initial_viewport(
    issue_wizard, load_migration, migration_context, qtbot, qapp, description_lines
):
    dialog, wizard = issue_wizard
    legacy = load_migration("legacy")
    issues = tuple(legacy.ScanIssue("invalid", "未知诊断", f"问题 {issue_number}") for issue_number in range(80))
    scan = make_scan(legacy, issues=issues)
    scan = replace(
        scan, mods=tuple(replace(mod, description="旧版详细说明\n" * description_lines) for mod in scan.mods)
    )
    scan_in_wizard(wizard, migration_context, scan)
    dialog.resize(1100, 850)
    select_mod(wizard)
    settle_layout(qapp)
    viewport = wizard.scroll_area.viewport()
    assert wizard.scroll_area.verticalScrollBar().value() == 0
    assert viewport.rect().contains(widget_rect(wizard.detail_title, viewport))
    assert all(wizard.tree_model.issue_index(row).isValid() for row in range(80))
    assert abs(widget_rect(wizard.tree, wizard).bottom() - widget_rect(wizard.detail_card, wizard).bottom()) <= 1
    if description_lines:
        assert scan.mods[0].description in wizard.detail_text.text()
        assert wizard.detail_scroll.verticalScrollBar().maximum() > 0
        assert wizard.scroll_area.verticalScrollBar().maximum() == 0
    assert not migration_context.mods.imports


def test_busy_render_does_not_update_hidden_details(issue_wizard, load_migration, migration_context, monkeypatch):
    dialog, wizard = issue_wizard
    scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
    select_mod(wizard)
    updates = []
    monkeypatch.setattr(wizard, "_render_detail", lambda: updates.append(True))
    wizard.controller.state = "importing"
    wizard._render()
    assert not updates
    wizard.controller.state = "finished"
    wizard._render()
    assert updates == [True]
