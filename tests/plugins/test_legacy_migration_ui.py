"""用真实宿主样式和对话框验证旧版迁移窗口的显示及资源归属。"""

from __future__ import annotations

from dataclasses import replace
from os import environ
from pathlib import Path
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QModelIndex, QPoint, QRect, Qt
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import QFileDialog, QPushButton, QStyle, QStyleOptionComboBox, QWidget

from src.plugin_sdk import OperationResult, TaskProgress, TaskResult
from src.plugin_sdk.i18n import TranslationFacade
from src.plugin_sdk.ui_ext import UIExtensionPoints
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.manifest import Manifest
from src.ui import fonts, theme
from src.ui.background_style import appearance_stylesheet, install_background_style
from src.ui.plugin_ui_host import PluginUIHost
from src.utils import i18n
from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context


@pytest.fixture
def migration_host(migration_context, qtbot, qapp, migration_ui_font, monkeypatch):
    """保留真实 UIHost 和样式注册，数据操作仍由可控 SDK 边界完成。"""
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: True)
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
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()
    theme.set_theme("light")
    theme.set_background_active(False)
    qapp.setStyleSheet(appearance_stylesheet())
    surfaces = install_background_style(qapp)
    surfaces.apply(False, theme_name="light")
    yield host
    host.cleanup_plugin(migration_context.plugin_id)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(previous[0])
    theme.set_background_active(previous[1])
    qapp.setStyleSheet(previous[2])
    surfaces.apply(previous[1], theme_name=previous[0])


@pytest.fixture(scope="session")
def migration_ui_font(qapp):
    """Windows offscreen 没有系统字体回退，显式载入真实宿主可用的中文字体。"""
    previous = qapp.font()
    font_path = Path(environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    font_id = -1
    if qapp.platformName() == "offscreen" and font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            fonts.apply_font(qapp, fonts.resolve_font(families[0]))
    yield
    if font_id >= 0:
        fonts.apply_font(qapp, previous)
        QFontDatabase.removeApplicationFont(font_id)


def scan_in_wizard(wizard, ctx, scan):
    """操作来源输入和实际扫描按钮，确认 UI 与控制器保持一致。"""
    wizard.path_edit.setText(scan.environment.root)
    wizard.path_edit.returnPressed.emit()
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


def mod_indexes(wizard):
    """遍历真实树中的 Mod，分类行不计入迁移数量。"""
    pending = [QModelIndex()]
    result = []
    while pending:
        parent = pending.pop()
        for row in range(wizard.tree_model.rowCount(parent)):
            index = wizard.tree_model.index(row, 0, parent)
            if index.internalPointer().item_index >= 0:
                result.append(index)
            else:
                pending.append(index)
    return result


def select_mod(wizard, item_index=0):
    """按真实条目下标选择 Mod，不依赖当前搜索或分类顺序。"""
    index = next(index for index in mod_indexes(wizard) if index.internalPointer().item_index == item_index)
    visible = wizard.tree_proxy.mapFromSource(index)
    assert visible.isValid()
    wizard.tree.setCurrentIndex(visible)
    return index


def widget_rect(widget, ancestor):
    """将真实控件几何映射到同一坐标系，不依赖布局实现。"""
    return QRect(widget.mapTo(ancestor, QPoint()), widget.size())


def test_rescan_painted_preview_does_not_read_stale_model_rows(
    load_migration, migration_context, migration_host, qapp, qtlog
):
    """已绘制的预览再次读取环境时，样式重入不能访问被清空的上一批条目。"""
    from PySide6.QtTest import QAbstractItemModelTester

    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    testers = [
        QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Warning, wizard)
        for model in (wizard.tree_model, wizard.tree_proxy)
    ]
    try:
        dialog.resize(840, 700)
        issues = tuple(legacy.ScanIssue("warning", "长诊断说明-" * 30, f"条目 {index}") for index in range(11))
        for count in (29, 5, 0):
            scan = make_scan(legacy, count=count, issues=issues)
            scan_in_wizard(wizard, migration_context, scan)
            wizard.scroll_area.verticalScrollBar().setValue(wizard.scroll_area.verticalScrollBar().maximum())
            for step in range(10):
                qapp.processEvents()
            assert not dialog.grab().isNull()
            assert len(mod_indexes(wizard)) == count
        assert len(testers) == 2
        assert not [record.message for record in qtlog.records if "FAIL!" in record.message]
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("window_size", [(632, 392), (732, 632), (840, 700), (1024, 768)])
@pytest.mark.parametrize("count", [0, 29, 33, 2000])
def test_preview_controls_do_not_overlap_in_host_dialog(
    load_migration, migration_context, migration_host, qapp, window_size, count
):
    """宿主边距和真实样式下，诊断入口及来源控件不能盖住表格。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    long_path = "D:/CHN/个人项目/测试/d3dx-all/resources/mods/" + "长路径与资源名称-" * 30
    issues = tuple(legacy.ScanIssue("invalid", "长诊断说明-" * 80, f"条目 {index}", long_path) for index in range(11))
    scan = make_scan(legacy, count=count, issues=issues)
    scan = replace(
        scan,
        environment=replace(scan.environment, root=long_path, path=long_path + "/home/gi", name="旧版环境-" * 60),
        mods=tuple(replace(mod, name="长资源名称-" * 40, source_path=long_path) for mod in scan.mods),
    )
    try:
        scan_in_wizard(wizard, migration_context, scan)
        dialog.resize(*window_size)
        qapp.processEvents()
        assert dialog.size().toTuple() == window_size
        assert len(mod_indexes(wizard)) == count
        tree_rect = widget_rect(wizard.tree, wizard)
        for widget in (wizard.source_label, wizard.source_combo, wizard.detail_card):
            if widget.isVisible():
                assert not tree_rect.intersects(widget_rect(widget, wizard)), widget.objectName()
        assert wizard.tree.parentWidget().rect().contains(wizard.tree.geometry())
        for button in wizard.findChildren(QPushButton):
            if button.isVisible() and button.parentWidget() is wizard:
                assert dialog.rect().contains(widget_rect(button, dialog))
        assert wizard.tree.isVisible()
        assert wizard.tree_model.issue_index(0).isValid()
        qapp.processEvents()
        assert len(wizard.tree_model.issues) == 11
        assert long_path in wizard.tree_model.data(wizard.tree_model.issue_index(0), Qt.ItemDataRole.ToolTipRole)
        assert not wizard.source_label.isVisible()
        assert not wizard.source_combo.isVisible()
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_long_task_diagnostic_keeps_full_text_and_actions_accessible(
    load_migration, migration_context, migration_host, qapp
):
    """多行错误必须完整排版，不能通过压缩标签或遮挡表格适配窗口。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        wizard.path_edit.setText("D:/CHN/个人项目/测试/d3dx-all")
        wizard.path_edit.returnPressed.emit()
        message = "\n".join("无法读取旧版资源：" + "很长的路径与诊断说明-" * 30 for index in range(11))
        migration_context.tasks.calls[-1].task._finish(TaskResult(error=message))
        dialog.resize(732, 632)
        qapp.processEvents()
        assert dialog.size().toTuple() == (732, 632)
        assert message not in wizard.status_label.text()
        assert not wizard.diagnostic_label.isVisible()
        wizard.diagnostic_button.click()
        qapp.processEvents()
        assert message in wizard.diagnostic_label.text()
        assert wizard.diagnostic_label.height() >= wizard.diagnostic_label.heightForWidth(
            wizard.diagnostic_label.width()
        )
        assert dialog.rect().contains(widget_rect(wizard.scan_button, dialog))
        scroll_bar = wizard.scroll_area.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        scroll_bar.setValue(scroll_bar.maximum())
        qapp.processEvents()
        assert widget_rect(wizard.diagnostic_label, wizard.scroll_area.viewport()).bottom() < (
            wizard.scroll_area.viewport().height()
        )
    finally:
        plugin.deactivate()


def test_empty_scan_keeps_disabled_copy_and_preview_accessible(load_migration, migration_context, migration_host, qapp):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = replace(make_scan(load_migration("legacy"), count=0), categories=())
    try:
        scan_in_wizard(wizard, migration_context, scan)
        dialog.resize(632, 392)
        qapp.processEvents()
        assert not wizard.copy_button.isEnabled()
        assert wizard.tree_empty.isVisible()
        assert wizard.tree_model.rowCount() == 0
        wizard.scroll_area.ensureWidgetVisible(wizard.tree)
        qapp.processEvents()
        assert widget_rect(wizard.tree, wizard.scroll_area.viewport()).intersects(wizard.scroll_area.viewport().rect())
        assert not wizard.source_label.isVisible()
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_finished_retry_and_report_actions_fit_small_host_dialog(
    load_migration, migration_context, migration_host, qtbot, qapp, monkeypatch, language, theme_name
):
    """长游戏名及英文重试按钮不能撑大宿主窗口，结束操作始终可见。"""
    directory = Path(__file__).resolve().parents[2] / "plugins" / "official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.1.0", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    monkeypatch.setattr(migration_context.game, "current_game_name", lambda: "LongGameNameWithoutSpaces" * 60)
    theme.set_theme(theme_name)
    install_background_style(qapp).apply(False, theme_name=theme_name)
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=1)
    migration_context.mods.responses.append(OperationResult("game-a", error="无法复制：" + "长诊断-" * 50))
    try:
        scan_in_wizard(wizard, migration_context, scan)
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        dialog.resize(632, 392)
        qapp.processEvents()
        assert dialog.size().toTuple() == (632, 392)
        assert wizard.scroll_area.widget().width() <= wizard.scroll_area.viewport().width()
        assert wizard.retry_button.isVisible()
        assert wizard.report_button.isVisible()
        assert not wizard.copy_button.isVisible()
        buttons = [
            button
            for button in wizard.findChildren(QPushButton)
            if button.isVisible() and button.parentWidget() is wizard
        ]
        for index, button in enumerate(buttons):
            assert dialog.rect().contains(widget_rect(button, dialog))
            assert button.width() >= button.minimumSizeHint().width()
            for other in buttons[index + 1 :]:
                assert not widget_rect(button, dialog).intersects(widget_rect(other, dialog))
        assert wizard.target_label.height() >= wizard.target_label.heightForWidth(wizard.target_label.width())
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_source_options_fit_after_selecting_painted_preview(
    load_migration, migration_context, migration_host, qapp, monkeypatch, language
):
    """来源选择器首次显示为空，随后填入原包与缓存时仍须完整显示选项。"""
    directory = Path(__file__).resolve().parents[2] / "plugins" / "official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.1.0", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=1)
    mod = replace(
        scan.mods[0], archive_path=scan.mods[0].source_path, cache_path="C:/legacy/cache/test", cache_changed=True
    )
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(mod,)))
        dialog.resize(632, 392)
        qapp.processEvents()
        assert not dialog.grab().isNull()
        select_mod(wizard)
        qapp.processEvents()
        assert wizard.source_combo.isEnabled()
        for index in range(wizard.source_combo.count()):
            wizard.source_combo.setCurrentIndex(index)
            qapp.processEvents()
            option = QStyleOptionComboBox()
            wizard.source_combo.initStyleOption(option)
            field = wizard.source_combo.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, wizard.source_combo
            )
            assert (
                wizard.source_combo.fontMetrics().horizontalAdvance(wizard.source_combo.currentText()) <= field.width()
            )
        assert wizard.controller.items[0].source.source_path == mod.cache_path
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


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
    wizard.path_edit.returnPressed.emit()
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
        styles = wizard.tree.styleSheet(), wizard.copy_button.styleSheet(), wizard.path_edit.styleSheet()
        theme.set_theme("dark")
        migration_host.retheme()
        assert wizard is dialog.findChild(QWidget, "legacyMigrationWizard")
        assert wizard.path_edit.text() == "C:/legacy"
        assert wizard.controller.scan is scan
        assert wizard.copy_button.isEnabled()
        for before, control in zip(styles, (wizard.tree, wizard.copy_button, wizard.path_edit), strict=True):
            assert before != control.styleSheet()
        assert len(mod_indexes(wizard)) == 2
        assert not wizard.tree_model.flags(mod_indexes(wizard)[0]) & Qt.ItemFlag.ItemIsEditable
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
        assert wizard.copy_button.text() == "Start migration"
        assert wizard.target_label.text().startswith("Import into:")
        assert wizard.tree_model.headerData(0, Qt.Orientation.Horizontal) == "Categories and Mods"
        assert wizard.tree_model.headerData(1, Qt.Orientation.Horizontal) == "Type"
        assert wizard.tree_model.headerData(2, Qt.Orientation.Horizontal) == "Count / size"
        assert wizard.detail_title.text() == "Select an item to see its details"
        assert wizard.tree.isVisible()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
        assert wizard.detail_title.text() == issue.name
        assert (
            wizard.detail_text.text()
            == "The Mod resource is missing; only an index or an unusable extracted cache remains."
        )
        model = wizard.tree_model
        assert len(model.issues) == 1
        issue_index = model.issue_index(0)
        assert model.data(issue_index.siblingAtColumn(3)) == "Cannot import"
        tooltip = model.data(issue_index, Qt.ItemDataRole.ToolTipRole)
        assert tooltip == "\n".join((issue.name, wizard.detail_text.text(), issue.source))
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
        wizard.path_edit.returnPressed.emit()
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
        if outcome != "ready":
            assert bool(wizard.status_label.text())
        assert wizard.copy_button.isEnabled() == (outcome == "ready")
    finally:
        plugin.deactivate()
