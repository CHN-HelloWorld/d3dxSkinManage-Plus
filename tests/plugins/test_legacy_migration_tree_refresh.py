"""验证隐藏迁移树不随复制逐项刷新，状态变化不重新执行整库搜索。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import QAbstractItemModel, QPersistentModelIndex, Qt
from PySide6.QtWidgets import QComboBox, QWidget

from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard, select_mod


def test_wizard_owns_only_the_displayed_tree_model_and_its_filter(load_migration, migration_context, migration_host):
    """预览仅维护实际绑定视图的模型，不为格式化或诊断创建隐藏 Qt 模型。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        assert wizard.tree.model() is wizard.tree_proxy
        assert wizard.tree_proxy.sourceModel() is wizard.tree_model
        combo_models = {combo.model() for combo in wizard.findChildren(QComboBox)}
        assert set(wizard.findChildren(QAbstractItemModel)) - combo_models == {
            wizard.tree_model,
            wizard.tree_proxy,
        }
    finally:
        plugin.deactivate()


@pytest.mark.parametrize(
    "status, text",
    [
        ("pending", "待迁移"),
        ("running", "正在导入"),
        ("succeeded", "已导入"),
        ("skipped", "已存在，不重复导入"),
        ("failed", "未完成，可重试"),
        ("cancelled", "未完成，已停止"),
    ],
)
def test_mod_tree_columns_show_the_current_copy_status(load_migration, migration_context, migration_host, status, text):
    """树的原生列读取最新结果，完整分类和源路径仍在工具提示中保留。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        index = select_mod(wizard)
        wizard.controller._set_item(0, status)
        wizard.tree_model.refresh()
        assert [index.siblingAtColumn(column).data() for column in range(4)] == [
            "外观 1",
            "Mod",
            "1.0 KiB",
            text,
        ]
        tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
        assert "服装 / 对象甲" in tooltip
        assert "C:/legacy/resources/0.zip" in tooltip
        assert text in tooltip
        assert index.data(Qt.ItemDataRole.DecorationRole) is None
        assert index.data(Qt.ItemDataRole.CheckStateRole) is None
    finally:
        plugin.deactivate()


def test_mod_tree_keeps_copy_errors_and_warnings_in_its_tooltip(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", "外观 1")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
        index = select_mod(wizard)
        wizard.controller._set_item(0, "failed", error="无法复制来源文件", warnings=("预览图未复制",))
        wizard.tree_model.refresh()
        assert index.siblingAtColumn(3).data() == "未完成，可重试 · 有提示，请查看报告 · 需留意"
        tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
        assert "无法复制来源文件" in tooltip
        assert "预览图未复制" in tooltip
        assert "C:/legacy/resources/0.zip" in tooltip
        assert index.data() == "外观 1"
    finally:
        plugin.deactivate()


def test_importing_defers_tree_updates_and_finished_changes_only_the_modified_row(
    load_migration, migration_context, migration_host, monkeypatch
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy"), count=2000))
        wizard.search_edit.setText("完全不匹配的名称")
        assert wizard.tree_proxy.rowCount() == 0
        updates = []
        wizard.tree_model.dataChanged.connect(lambda *args: updates.append(args))
        reads = []
        original = wizard.tree_model.data

        def track_reads(*args):
            """记录树模型的数据读取，验证迁移刷新不遍历隐藏条目。"""
            reads.append(args)
            return original(*args)

        monkeypatch.setattr(wizard.tree_model, "data", track_reads)
        wizard.controller.state = "importing"
        wizard.controller._set_item(0, "succeeded")
        for notification in range(20):
            wizard.tree_model.refresh()
        assert not updates
        assert not reads
        wizard.controller.state = "finished"
        wizard.tree_model.refresh()
        mod_updates = [update for update in updates if update[0].internalPointer().item_index >= 0]
        assert len(mod_updates) == 1
        first, last, roles = mod_updates[0]
        assert first.column() == last.column() == 3
        assert first.internalPointer().item_index == last.internalPointer().item_index == 0
        assert Qt.ItemDataRole.DisplayRole in roles
        assert len(reads) < 30
        assert wizard.tree_proxy.rowCount() == 0
    finally:
        plugin.deactivate()


def test_file_choice_updates_cached_size_without_changing_search_identity(
    load_migration, migration_context, migration_host
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=2)
    source = replace(
        scan.mods[1],
        archive_path=scan.mods[1].source_path,
        cache_path="C:/legacy/cache/edited",
        cache_changed=True,
        cache_size_bytes=4096,
    )
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(scan.mods[0], source)))
        assert wizard.tree_model.total_bytes == 2048
        wizard.search_edit.setText(source.name)
        index = select_mod(wizard, 1)
        before = wizard.tree_model.data(index, wizard.tree_model.SearchRole)
        updates = []
        wizard.tree_model.dataChanged.connect(lambda *args: updates.append(args))
        wizard.source_combo.setCurrentIndex(wizard.source_combo.findData(True))
        assert wizard.tree_model.total_bytes == 5120
        assert index.siblingAtColumn(2).data() == "4.0 KiB"
        assert index.siblingAtColumn(3).data() == "待迁移 · 使用解压后的文件"
        assert "C:/legacy/cache/edited" in index.data(Qt.ItemDataRole.ToolTipRole)
        assert wizard.tree_model.data(index, wizard.tree_model.SearchRole) == before
        assert all(
            first.internalPointer().item_index == last.internalPointer().item_index == 1
            for first, last, roles in updates
        )
        assert all(wizard.tree_model.SearchRole not in roles for first, last, roles in updates)
    finally:
        plugin.deactivate()


def test_copy_diagnostics_are_deferred_until_results_without_losing_new_issues(
    load_migration, migration_context, migration_host
):
    """复制时暂缓树更新，结果增量加入全部诊断，替换诊断不使已有 Mod 索引失效。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy))
        model = wizard.tree_model
        mod_index = QPersistentModelIndex(select_mod(wizard))
        resets = []
        insertions = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsInserted.connect(lambda *args: insertions.append(args))
        wizard.controller.state = "importing"
        for index in range(50):
            wizard.controller.migration_issues.append(legacy.ScanIssue("warning", "复制说明", f"条目 {index}"))
            model.refresh()
        assert not resets
        assert not insertions
        assert not model.issues
        wizard.controller.state = "finished"
        model.refresh()
        assert insertions
        assert not resets
        assert len(model.issues) == 50
        assert all(model.issue_index(index).isValid() for index in range(50))
        assert model.data(model.issue_index(49)) == "条目 49"
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(model.issue_index(49)))
        assert wizard.detail_title.text() == "条目 49"
        wizard.controller.migration_issues = [legacy.ScanIssue("warning", "新的复制说明", "新条目")]
        model.refresh()
        assert len(model.issues) == 1
        assert model.data(model.issue_index(0)) == "新条目"
        assert not model.issue_index(1).isValid()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(model.issue_index(0)))
        assert wizard.detail_title.text() == "新条目"
        assert mod_index.isValid()
        assert mod_index.data() == "外观 1"
        assert not resets
    finally:
        plugin.deactivate()
