"""验证真实宿主采用紧凑尺寸，无法导入和需留意条目也出现在树中。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QWidget

from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard


@pytest.mark.parametrize("available", [QRect(0, 0, 1920, 1080), QRect(-3840, -861, 3840, 2160)])
def test_host_uses_same_compact_size_on_hd_and_4k_including_before_it_shows(
    load_migration, migration_context, migration_host, monkeypatch, qapp, available
):
    module = load_migration("wizard")
    monkeypatch.setattr(module.MigrationWizard, "_available_geometry", lambda self, window: available)
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        for iteration in range(6):
            qapp.processEvents()
        assert dialog.size().toTuple() == (720, 480)
        assert available.contains(dialog.frameGeometry())
        assert wizard.sizeHint().toTuple() == (688, 448)
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        for iteration in range(6):
            qapp.processEvents()
        assert dialog.size().toTuple() == (1160, 780)
        assert wizard.sizeHint().toTuple() == (1128, 748)
        assert available.contains(dialog.frameGeometry())
    finally:
        plugin.deactivate()


def test_blocked_objects_and_warning_rows_are_visible_and_cannot_be_imported(
    load_migration, migration_context, migration_host, qtbot
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issues = (
        legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "对象乙", "分类甲\n分类乙"),
        legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", "外观 1"),
        legacy.ScanIssue("warning", "已按旧版规则近似匹配缩略图，请检查结果", "服装"),
    )
    scan = make_scan(legacy, issues=issues)
    try:
        scan_in_wizard(wizard, migration_context, scan)
        model = wizard.tree_model
        assert model.issue_index(0).isValid()
        assert model.data(model.issue_index(0)) == "对象乙"
        index = model.issue_index(0)
        assert model.data(model.index(index.row(), 1, index.parent())) == "角色 / 对象"
        assert model.data(model.index(index.row(), 3, index.parent())) == "无法导入"
        warning = model.issue_index(1)
        assert warning.parent().internalPointer().item_index == 0
        assert model.data(model.index(warning.row(), 3, warning.parent())) == "需留意"
        assert model.issue_index(2).parent().internalPointer().category.path == ("服装",)
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(index))
        assert wizard.detail_title.text() == "对象乙"
        assert wizard.detail_text.text() == issues[0].message
        assert not wizard.source_combo.isVisible()
        assert len(wizard.controller.items) == 2
        assert len(wizard.controller.categories) == 2
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert len(migration_context.mods.imports) == 2
        assert len(migration_context.mods.categories) == 2
    finally:
        plugin.deactivate()


def test_copy_diagnostics_and_rescan_do_not_leave_stale_issue_rows(
    load_migration, migration_context, migration_host, qapp, qtlog
):
    from PySide6.QtTest import QAbstractItemModelTester

    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    scan = make_scan(legacy)
    tester = QAbstractItemModelTester(wizard.tree_model, QAbstractItemModelTester.FailureReportingMode.Warning, wizard)
    try:
        scan_in_wizard(wizard, migration_context, scan)
        wizard.controller.state = "importing"
        wizard.controller.migration_issues.append(legacy.ScanIssue("warning", "复制诊断", "外观 1"))
        wizard.tree_model.refresh()
        assert not wizard.tree_model.issue_index(0).isValid()
        assert tester.model() is wizard.tree_model
        assert not [record.message for record in qtlog.records if "FAIL!" in record.message]
        wizard.controller.state = "finished"
        wizard.tree_model.refresh()
        assert wizard.tree_model.issue_index(0).isValid()
        wizard.controller.state = "ready"
        wizard.controller.scan = replace(scan)
        wizard.controller.migration_issues = []
        wizard._render()
        qapp.processEvents()
        assert not wizard.tree_model.issue_index(0).isValid()
    finally:
        plugin.deactivate()
