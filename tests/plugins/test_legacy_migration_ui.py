"""用真实宿主样式和对话框验证旧版迁移窗口的显示及资源归属。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QFileDialog, QWidget

from src.plugin_sdk import TaskProgress, TaskResult
from src.plugin_sdk.i18n import TranslationFacade
from src.plugin_sdk.ui_ext import UIExtensionPoints
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.manifest import Manifest
from src.ui import theme
from src.ui.plugin_ui_host import PluginUIHost
from src.utils import i18n
from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context


@pytest.fixture
def migration_host(migration_context, qtbot, monkeypatch):
    """保留真实 UIHost 和样式注册，数据操作仍由可控 SDK 边界完成。"""
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    window = QWidget()
    qtbot.addWidget(window)
    host = PluginUIHost(window)
    migration_context.ui = UIExtensionPoints(
        migration_context.plugin_id,
        SimpleNamespace(),
        host,
        granted=frozenset({"ui:views"}),
        subscriptions=migration_context.subscriptions,
    )
    previous = theme.current_theme(), theme.is_background_active()
    theme.set_theme("light")
    theme.set_background_active(False)
    yield host
    host.cleanup_plugin(migration_context.plugin_id)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(previous[0])
    theme.set_background_active(previous[1])


def scan_in_wizard(wizard, ctx, scan):
    """操作来源输入和实际扫描按钮，确认 UI 与控制器保持一致。"""
    wizard.path_edit.setText(scan.environment.root)
    wizard.read_button.click()
    ctx.tasks.finish((scan.environment,))
    assert wizard.scan_button.isEnabled(), (
        wizard.controller.state,
        wizard.environment_combo.currentIndex(),
        wizard.environment_combo.currentData(),
        wizard.controller.target_game_id,
        wizard.path_edit.text(),
        wizard.controller.root,
    )
    wizard.scan_button.click()
    ctx.tasks.finish(scan)


def test_close_and_immediate_reopen_keep_replacement_dialog_owned(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    try:
        first = plugin._open()
        first.close()
        replacement = plugin._open()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(first)
        assert plugin._open() is replacement
    finally:
        plugin.deactivate()


def test_dialog_close_stops_its_scan_and_ignores_late_result(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    wizard.path_edit.setText("C:/legacy")
    wizard.read_button.click()
    task = migration_context.tasks.calls[-1]
    dialog.close()
    assert task.cancelled.is_set()
    migration_context.tasks.finish(())
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(wizard)
    plugin.deactivate()


def test_directory_picker_deactivation_does_not_start_discovery(
    load_migration, migration_context, migration_host, monkeypatch
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")

    def choose_after_deactivation(*args, **kwargs):
        """模拟原生目录对话框处理事件时插件已被停用。"""
        plugin.deactivate()
        return "C:/legacy"

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", choose_after_deactivation)
    wizard.browse_button.click()
    assert not migration_context.tasks.calls


def test_open_preview_rethemes_without_losing_scan_or_inputs(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"))
    try:
        scan_in_wizard(wizard, migration_context, scan)
        assert wizard.copy_button.isEnabled()
        styles = wizard.table.styleSheet(), wizard.copy_button.styleSheet(), wizard.path_edit.styleSheet()
        theme.set_theme("dark")
        migration_host.retheme()
        assert wizard is dialog.findChild(QWidget, "legacyMigrationWizard")
        assert wizard.path_edit.text() == "C:/legacy"
        assert wizard.controller.scan is scan
        assert wizard.copy_button.isEnabled()
        for before, control in zip(styles, (wizard.table, wizard.copy_button, wizard.path_edit), strict=True):
            assert before != control.styleSheet()
        assert wizard.table.model().rowCount() == 2
        assert not wizard.table.model().flags(wizard.table.model().index(0, 0)) & Qt.ItemFlag.ItemIsEditable
    finally:
        plugin.deactivate()


def test_changing_source_disables_copy_until_new_preview(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        assert wizard.copy_button.isEnabled()
        wizard.path_edit.setText("C:/another-legacy")
        assert not wizard.copy_button.isEnabled()
        wizard.copy_button.click()
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_english_preview_includes_translated_missing_resource_diagnostic(
    load_migration, migration_context, migration_host, monkeypatch
):
    directory = Path(__file__).resolve().parents[2] / "plugins" / "official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.1.0", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue(
        "missing", "旧版 Mod 本体缺失，仅有索引或解压缓存", "缺失条目", "C:/legacy/resources/missing"
    )
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
        assert wizard.copy_button.text() == "Confirm and copy"
        assert wizard.target_label.text().startswith("Target game:")
        wizard.issues_button.click()
        model = wizard.table.model()
        assert model.rowCount() == 1
        assert model.data(model.index(0, 0)) == "Missing file"
        assert model.data(model.index(0, 3)) == (
            "The original Mod archive is missing; only an index or extracted cache remains."
        )
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("outcome", ["ready", "error", "cancelled"])
def test_scan_terminal_state_replaces_its_previous_progress_message(
    load_migration, migration_context, migration_host, outcome
):
    """后台结束后必须展示预览、错误或取消状态，不能残留上一条扫描进度。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"))
    try:
        wizard.path_edit.setText(scan.environment.root)
        wizard.read_button.click()
        migration_context.tasks.finish((scan.environment,))
        wizard.scan_button.click()
        task = migration_context.tasks.calls[-1].task
        progress_text = "这条扫描进度应在阶段结束时消失"
        task._report(TaskProgress(0.5, progress_text))
        assert wizard.status_label.text() == progress_text
        if outcome == "cancelled":
            wizard.stop_button.click()
            assert wizard.status_label.text() != progress_text
            result = TaskResult(cancelled=True)
        elif outcome == "error":
            result = TaskResult(error="无法读取旧版文件或目录")
        else:
            result = TaskResult(value=scan)
        task._finish(result)
        assert wizard.controller.state == outcome
        assert wizard.status_label.text() != progress_text
        assert bool(wizard.status_label.text())
        assert wizard.copy_button.isEnabled() == (outcome == "ready")
    finally:
        plugin.deactivate()
