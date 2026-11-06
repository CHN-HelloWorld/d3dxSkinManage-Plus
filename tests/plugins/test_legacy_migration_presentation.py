"""验证迁移预览的窗口尺寸、数字概览、诊断呈现与树列布局。"""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QListView, QPushButton, QWidget

from src.plugin_sdk.i18n import TranslationFacade
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.manifest import Manifest
from src.utils import i18n
from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard, select_mod, widget_rect


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_review_displays_original_diagnostics_in_details_tooltips_and_search(
    load_migration, migration_context, migration_host, monkeypatch, qapp, language
):
    """已知和未知诊断均使用现有翻译接口，占位符从扫描数据读取。"""
    directory = Path(__file__).resolve().parents[2] / "plugins/official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.3.5", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issues = (
        legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "对象乙", "分类甲\n分类乙"),
        legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", "外观 1"),
        legacy.ScanIssue("warning", "未识别的诊断：{name}（{source}）", "条目乙", "C:/legacy/metadata.json"),
    )
    expected_messages = (
        (
            "The object matches multiple legacy categories. Its mods were skipped.",
            "The cache differs from the original archive, or their equality cannot be verified. "
            "You can choose to recover cache changes.",
        )
        if language == "en-US"
        else (
            "对象匹配多个旧版分类，已跳过相关 Mod",
            "缓存与原包不同或无法确认一致，可选择恢复缓存修改",
        )
    ) + ("未识别的诊断：条目乙（C:/legacy/metadata.json）",)
    forbidden = (
        "哪些内容会迁移？",
        "所有可用内容都会迁移，无需逐项勾选。",
        "未迁移条目已列出原因；其他可用内容可以继续迁移。",
        "需要留意的内容",
        "What gets migrated?",
        "All available contents will be migrated.",
        "Reasons are listed for excluded items.",
        "Things to check",
    )
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=issues))
        for iteration in range(6):
            qapp.processEvents()
        assert dialog.windowTitle() == migration_context.i18n.t("d3dxSkinManage数据迁移")
        texts = [widget.text() for widget in wizard.findChildren(QLabel)]
        texts.extend(widget.text() for widget in wizard.findChildren(QPushButton))
        assert not any(value in text for value in forbidden for text in texts)
        assert wizard.findChild(QListView, "legacyMigrationProblems") is None
        assert wizard.preview_splitter.widget(1).layout().count() == 1
        for row, (issue, message) in enumerate(zip(issues, expected_messages, strict=True)):
            index = wizard.tree_model.issue_index(row)
            assert index.isValid()
            status = wizard.tree_model.index(index.row(), 3, index.parent())
            expected = "无法导入" if issue.code == "invalid" else "需留意"
            assert wizard.tree_model.data(status) == migration_context.i18n.t(expected)
            wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(index))
            assert wizard.detail_title.text() == issue.name
            assert wizard.detail_text.text() == message
            assert wizard.tree_model.data(index, Qt.ItemDataRole.ToolTipRole) == "\n".join(
                value for value in (issue.name, message, issue.source) if value
            )
            assert wizard.tree_model.data(index, wizard.tree_model.SearchRole) == issue.name + "\n" + message
            assert not wizard.source_combo.isVisible()
        report = load_migration("report").migration_report(wizard.controller)
        assert all(message in report for message in expected_messages)
        assert wizard.controller.scan.issues == issues
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_default_window_matches_category_preset_reference_size(
    load_migration, migration_context, migration_host, monkeypatch
):
    wizard_module = load_migration("wizard")
    available = QRect(120, 40, 1920, 1080)
    monkeypatch.setattr(
        wizard_module.MigrationWizard, "_available_geometry", lambda self, window: available, raising=False
    )
    migration_host._w.setGeometry(160, 100, 1000, 700)
    migration_host._w.show()
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    try:
        assert dialog.size().toTuple() == (720, 480)
        assert available.contains(dialog.frameGeometry())
        assert (dialog.frameGeometry().center() - migration_host._w.frameGeometry().center()).manhattanLength() <= 2
    finally:
        plugin.deactivate()


def test_review_has_centered_steps_numeric_totals_and_aligned_panel_bottoms(
    load_migration, migration_context, migration_host, monkeypatch, qapp
):
    monkeypatch.setattr(
        load_migration("wizard").MigrationWizard, "_available_geometry", lambda self, window: QRect(0, 0, 1920, 1080)
    )
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    scan = make_scan(legacy, issues=(legacy.ScanIssue("invalid", "未知诊断", "问题甲"),))
    try:
        scan_in_wizard(wizard, migration_context, scan)
        select_mod(wizard)
        for iteration in range(8):
            qapp.processEvents()
        assert all(label.alignment() == Qt.AlignmentFlag.AlignCenter for label in wizard.step_labels)
        assert [label.text() for label in wizard.stat_values] == ["2", "1", "1", "1"]
        assert [label.text() for label in wizard.stat_labels] == [
            "可迁移 Mod",
            "可导入对象",
            "可导入分类",
            "无法迁移项",
        ]
        summary_width = wizard.summary_label.fontMetrics().horizontalAdvance(wizard.summary_label.text())
        assert wizard.summary_label.width() >= summary_width
        assert abs(widget_rect(wizard.tree, wizard).bottom() - widget_rect(wizard.detail_card, wizard).bottom()) <= 1
        wizard.search_edit.setText("外观 2")
        assert [label.text() for label in wizard.stat_values] == ["2", "1", "1", "1"]
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_source_and_result_use_compact_pages_without_hiding_controls(
    load_migration, migration_context, migration_host, monkeypatch, qapp, qtbot, language
):
    module = load_migration("wizard")
    monkeypatch.setattr(module.MigrationWizard, "_available_geometry", lambda self, window: QRect(0, 0, 1920, 1080))
    directory = Path(__file__).resolve().parents[2] / "plugins/official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.3.4", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=1)
    try:
        wizard.path_edit.setText(scan.environment.root)
        wizard.path_edit.returnPressed.emit()
        another = replace(scan.environment, key="another", name="Another legacy environment")
        migration_context.tasks.finish((scan.environment, another))
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (720, 480)
        assert wizard.scroll_area.verticalScrollBar().maximum() == 0
        assert wizard.environment_combo.isVisible()
        viewport = wizard.scroll_area.viewport()
        assert viewport.rect().contains(widget_rect(wizard.environment_combo, viewport))
        wizard.environment_combo.setCurrentIndex(0)
        wizard.scan_button.click()
        migration_context.tasks.finish(scan)
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (1160, 780)
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (720, 480)
        assert wizard.scroll_area.verticalScrollBar().maximum() == 0
        assert dialog.rect().contains(widget_rect(wizard.close_button, dialog))
        wizard.details_button.click()
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (1160, 780)
        assert wizard.tree.isVisible()
        wizard.details_button.click()
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (720, 480)
    finally:
        plugin.deactivate()


def test_type_and_state_use_separate_columns_for_all_tree_rows(load_migration, migration_context, migration_host):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        model = wizard.tree_model
        assert model.columnCount() == 4
        assert [model.headerData(column, Qt.Orientation.Horizontal) for column in range(4)] == [
            "分类与 Mod",
            "类型",
            "数量 / 大小",
            "状态",
        ]
        category = model.index(0, 0)
        character = model.index(0, 0, category)
        mod = model.index(0, 0, character)
        assert model.data(model.index(category.row(), 1)) == "分类"
        assert model.data(model.index(character.row(), 1, category)) == "角色 / 对象"
        assert model.data(model.index(mod.row(), 1, character)) == "Mod"
        assert model.data(model.index(mod.row(), 2, character)) == "1.0 KiB"
        assert model.data(model.index(category.row(), 3)) == "待迁移"
        assert model.data(model.index(character.row(), 3, category)) == "待迁移"
        wizard.controller._category_errors[("服装", "对象甲")] = "无法创建"
        assert model.data(model.index(character.row(), 3, category)) == "无法迁移"
        wizard.controller._category_errors.clear()
        wizard.controller._nodes[("服装", "对象甲")] = "existing-character"
        assert model.data(model.index(character.row(), 3, category)) == "已就绪"
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_compact_review_keeps_panels_visible_with_long_details_and_file_choices(
    load_migration, migration_context, migration_host, monkeypatch, qapp, tmp_path, language
):
    module = load_migration("wizard")
    monkeypatch.setattr(module.MigrationWizard, "_available_geometry", lambda self, window: QRect(0, 0, 1920, 1080))
    directory = Path(__file__).resolve().parents[2] / "plugins/official.legacy-migration"
    manifest = Manifest("official.legacy-migration", "旧版迁移", "0.3.4", "2.1", "entry.py", directory=directory)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    migration_context.i18n = TranslationFacade(PluginLocalizer(manifest))
    image_path = tmp_path / "preview.png"
    image = QImage(300, 140, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(image_path))
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "无法导入对象")
    scan = make_scan(legacy, count=1, issues=(issue,))
    mod = replace(
        scan.mods[0],
        description="详细说明 / Long description\n" * 120,
        preview_paths=(str(image_path),),
        archive_path=scan.mods[0].source_path,
        cache_path="C:/legacy/cache/test",
        cache_changed=True,
    )
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(mod,)))
        select_mod(wizard)
        for iteration in range(8):
            qapp.processEvents()
        assert dialog.size().toTuple() == (1160, 780)
        assert wizard.scroll_area.verticalScrollBar().maximum() == 0
        assert wizard.detail_scroll.verticalScrollBar().maximum() > 0
        assert wizard.detail_preview.pixmap() is not None
        assert wizard.detail_card.rect().contains(widget_rect(wizard.detail_scroll, wizard.detail_card))
        detail_viewport = wizard.detail_scroll.viewport()
        assert detail_viewport.rect().contains(widget_rect(wizard.source_combo, detail_viewport))
        for widget in (wizard.tree, wizard.detail_card):
            assert wizard.scroll_area.viewport().rect().contains(widget_rect(widget, wizard.scroll_area.viewport()))
        assert abs(widget_rect(wizard.tree, wizard).bottom() - widget_rect(wizard.detail_card, wizard).bottom()) <= 1
        wizard.detail_scroll.verticalScrollBar().setValue(wizard.detail_scroll.verticalScrollBar().maximum())
        qapp.processEvents()
        assert widget_rect(wizard.detail_text, detail_viewport).bottom() <= detail_viewport.rect().bottom()
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()
