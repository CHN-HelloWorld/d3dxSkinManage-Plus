"""验证分步迁移的用户操作，不使用真实库写入。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QWidget

from src.plugin_sdk import OperationResult
from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard, select_mod


def test_folder_picker_requires_choice_between_multiple_data_sets(
    load_migration, migration_context, migration_host, monkeypatch
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    environment = make_scan(load_migration("legacy")).environment
    second = replace(environment, key="second", name="第二份数据", path="C:/legacy/home/second")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: "C:/legacy")
    try:
        assert not wizard.tree.isVisible()
        assert not wizard.copy_button.isVisible()
        wizard.browse_button.click()
        migration_context.tasks.finish((environment, second))
        assert wizard.environment_combo.currentIndex() == -1
        assert not wizard.scan_button.isEnabled()
        wizard.environment_combo.setCurrentIndex(1)
        assert wizard.scan_button.isEnabled()
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_pasted_folder_continues_to_preview_without_separate_discovery_action(
    load_migration, migration_context, migration_host
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"))
    try:
        wizard.path_edit.setText(scan.environment.root)
        assert wizard.scan_button.isEnabled()
        wizard.scan_button.click()
        migration_context.tasks.finish((scan.environment,))
        assert wizard.controller.state == "scanning"
        migration_context.tasks.finish(scan)
        assert wizard.tree.isVisible()
        assert not wizard.path_edit.isVisible()
        assert wizard.copy_button.isEnabled()
        assert not migration_context.mods.imports
        wizard.back_button.click()
        assert wizard.path_edit.isVisible()
        assert not wizard.tree.isVisible()
        wizard.scan_button.click()
        assert wizard.controller.state == "scanning"
        refreshed = replace(scan)
        migration_context.tasks.finish(refreshed)
        assert wizard.tree.isVisible()
        assert wizard.controller.scan is refreshed
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("has_changes", [False, True])
def test_file_version_choice_only_appears_for_changed_files(
    load_migration, migration_context, migration_host, has_changes
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=1)
    source = scan.mods[0]
    source = replace(
        source, archive_path=source.source_path, cache_path="C:/legacy/cache/mod", cache_changed=has_changes
    )
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(source,)))
        assert not wizard.source_combo.isVisible()
        select_mod(wizard)
        assert wizard.source_combo.isVisible() == has_changes
        assert wizard.controller.items[0].source.source_path == source.archive_path
    finally:
        plugin.deactivate()


def test_finished_page_separates_outcomes_from_optional_details(
    load_migration, migration_context, migration_host, qtbot
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=2)
    migration_context.mods.responses.extend(
        (OperationResult("game-a", ("imported",)), OperationResult("game-a", error="copy failed"))
    )
    try:
        scan_in_wizard(wizard, migration_context, scan)
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert not wizard.tree.isVisible()
        assert not wizard.path_edit.isVisible()
        assert wizard.result_label.isVisible()
        assert "已导入 1" in wizard.result_label.text()
        assert "未完成 1" in wizard.result_label.text()
        assert wizard.retry_button.isVisible()
        wizard.details_button.click()
        assert wizard.tree.isVisible()
        wizard.details_button.click()
        assert not wizard.tree.isVisible()
    finally:
        plugin.deactivate()


def test_category_ambiguity_shows_original_diagnostic_without_rewriting(
    load_migration, migration_context, migration_host
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "对象乙", "角色/*;新增/*")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
        assert wizard.tree.isVisible()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
        assert wizard.detail_text.text() == issue.message
        model = wizard.tree_model
        issue_index = model.issue_index(0)
        assert model.data(issue_index.siblingAtColumn(3)) == "无法导入"
        tooltip = model.data(issue_index, Qt.ItemDataRole.ToolTipRole)
        assert tooltip == "\n".join((issue.name, issue.message, issue.source))
        assert wizard.copy_button.isEnabled()
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("issue_code", ["invalid", "warning"])
def test_finished_title_does_not_claim_complete_migration_when_scan_excluded_items(
    load_migration, migration_context, migration_host, qtbot, issue_code
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue(issue_code, "对象匹配多个旧版分类，已跳过相关 Mod", "旧版条目")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, count=1, issues=(issue,)))
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert wizard.controller.summary.succeeded == 1
        if issue_code == "invalid":
            assert "未迁移" in wizard.page_title.text()
        else:
            assert "完成" in wizard.page_title.text()
    finally:
        plugin.deactivate()


def test_safety_reminder_is_visible_without_scrolling_before_confirmation(
    load_migration, migration_context, migration_host, qapp
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy"), count=29))
        dialog.resize(680, 520)
        qapp.processEvents()
        assert wizard.safety_label.visibleRegion().boundingRect() == wizard.safety_label.rect()
    finally:
        plugin.deactivate()


def test_category_only_cancellation_is_not_presented_as_complete(
    load_migration, migration_context, migration_host, qtbot
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    migration_context.mods.immediate = False
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy"), count=0))
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: bool(migration_context.mods.tasks))
        wizard.stop_button.click()
        migration_context.mods.tasks[-1]._finish(OperationResult("game-a", cancelled_ids=("cancelled",)))
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert "已停止" in wizard.page_title.text()
        assert "已完成" in wizard.result_help.text()
        assert "处理完成" not in wizard.result_help.text()
    finally:
        plugin.deactivate()


def test_committed_mod_with_cleanup_error_keeps_visible_attention(
    load_migration, migration_context, migration_host, qtbot
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    migration_context.mods.responses.append(OperationResult("game-a", ("saved",), error="cleanup failed"))
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy"), count=1))
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert wizard.controller.summary.succeeded == 1
        assert "检查" in wizard.page_title.text()
        wizard.details_button.click()
        index = select_mod(wizard)
        status = wizard.tree_model.index(index.row(), 3, index.parent())
        assert "提示" in wizard.tree_model.data(status)
        assert "cleanup failed" in wizard.tree_model.data(status, Qt.ItemDataRole.ToolTipRole)
        assert "cleanup failed" in wizard.detail_text.text()
    finally:
        plugin.deactivate()


def test_issue_explanations_wrap_instead_of_requiring_hover_to_read(
    load_migration, migration_context, migration_host, qapp
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "对象乙")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
        dialog.resize(680, 520)
        assert wizard.tree.isVisible()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
        qapp.processEvents()
        assert wizard.detail_text.wordWrap()
        assert wizard.detail_text.text() == issue.message
    finally:
        plugin.deactivate()
