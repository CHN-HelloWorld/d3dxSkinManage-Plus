"""确认迁移树包含问题，选中后在详情显示，不改变实际迁移范围。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QWidget

from tests.plugins.test_legacy_migration import load_migration as load_migration
from tests.plugins.test_legacy_migration import make_scan
from tests.plugins.test_legacy_migration import migration_context as migration_context
from tests.plugins.test_legacy_migration_ui import migration_host as migration_host
from tests.plugins.test_legacy_migration_ui import migration_ui_font as migration_ui_font
from tests.plugins.test_legacy_migration_ui import scan_in_wizard, select_mod, widget_rect


def test_review_shows_categories_mods_and_issues_without_a_view_picker(
    load_migration, migration_context, migration_host
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    issue = legacy.ScanIssue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", "对象乙")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(legacy, issues=(issue,)))
        assert not any(combo.findData("issues") >= 0 for combo in wizard.findChildren(QComboBox))
        assert wizard.tree.isVisible()
        assert wizard.tree_model.issue_index(0).isValid()
        model = wizard.tree_model
        clothing = next(
            model.index(row, 0) for row in range(model.rowCount()) if model.data(model.index(row, 0)) == "服装"
        )
        character = model.index(0, 0, clothing)
        assert model.data(clothing) == "服装"
        assert model.data(character) == "对象甲"
        assert model.rowCount(character) == 2
        assert model.data(model.index(0, 0, character)) == "外观 1"
        assert model.data(model.index(0, 2, character)) == "1.0 KiB"
        assert len(model.issues) == 1
        issue_index = model.issue_index(0)
        assert model.data(issue_index.siblingAtColumn(3)) == "无法导入"
        tooltip = model.data(issue_index, Qt.ItemDataRole.ToolTipRole)
        assert tooltip == "\n".join((issue.name, issue.message))
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_search_keeps_parent_categories_and_does_not_limit_migration(
    load_migration, migration_context, migration_host, qtbot
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        wizard.search_edit.setText("外观 2")
        proxy = wizard.tree.model()
        root = proxy.index(0, 0)
        character = proxy.index(0, 0, root)
        assert proxy.rowCount(character) == 1
        assert proxy.data(proxy.index(0, 0, character)) == "外观 2"
        wizard.tree.setCurrentIndex(proxy.index(0, 0, character))
        assert "外观 2" in wizard.detail_title.text()
        wizard.copy_button.click()
        qtbot.waitUntil(lambda: wizard.controller.state == "finished")
        assert wizard.controller.summary.succeeded == 2
        assert len(migration_context.mods.imports) == 2
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("width, orientation", [(680, Qt.Orientation.Vertical), (1100, Qt.Orientation.Horizontal)])
def test_narrow_preview_stacks_panels_instead_of_hiding_one(
    load_migration, migration_context, migration_host, qapp, width, orientation
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    try:
        scan_in_wizard(wizard, migration_context, make_scan(load_migration("legacy")))
        dialog.resize(width, 700)
        qapp.processEvents()
        assert wizard.preview_splitter.orientation() == orientation
        assert wizard.tree.isVisible()
        assert not wizard.tree_model.issue_index(0).isValid()
        assert wizard.detail_card.isVisible()
        assert not migration_context.mods.imports
    finally:
        plugin.deactivate()


def test_selecting_category_or_issue_hides_mod_file_choice_without_changing_it(
    load_migration, migration_context, migration_host
):
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    scan = make_scan(legacy, count=1)
    mod = replace(
        scan.mods[0], archive_path=scan.mods[0].source_path, cache_path="C:/legacy/cache/edited", cache_changed=True
    )
    issue = legacy.ScanIssue("warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", mod.name)
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(mod,), issues=(issue,)))
        root = wizard.tree.model().index(0, 0)
        character = wizard.tree.model().index(0, 0, root)
        wizard.tree.setCurrentIndex(wizard.tree.model().index(0, 0, character))
        assert wizard.source_combo.isVisible()
        wizard.source_combo.setCurrentIndex(wizard.source_combo.findData(True))
        assert wizard.controller.items[0].source.source_path == mod.cache_path
        wizard.tree.setCurrentIndex(character)
        assert not wizard.source_combo.isVisible()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
        assert "修改" in wizard.detail_text.text()
        assert not wizard.source_combo.isVisible()
        assert wizard.controller.items[0].source.source_path == mod.cache_path
    finally:
        plugin.deactivate()


def test_returning_to_the_same_mod_after_an_issue_restores_its_details(
    load_migration, migration_context, migration_host
):
    """点击问题后再点同一 Mod，不能残留问题详情或隐藏版本选择。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    scan = make_scan(legacy, count=1, issues=(legacy.ScanIssue("invalid", "未知诊断", "问题甲"),))
    source = replace(
        scan.mods[0], cache_path="C:/legacy/cache/mod", archive_path=scan.mods[0].source_path, cache_changed=True
    )
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(source,)))
        select_mod(wizard)
        assert wizard.source_combo.isVisible()
        wizard.tree.setCurrentIndex(wizard.tree_proxy.mapFromSource(wizard.tree_model.issue_index(0)))
        assert wizard.detail_title.text() == "问题甲"
        select_mod(wizard)
        assert wizard.detail_title.text() == source.name
        assert wizard.source_combo.isVisible()
    finally:
        plugin.deactivate()


def test_search_file_choice_updates_the_matching_mod_not_the_first_row(
    load_migration, migration_context, migration_host
):
    """搜索只剩第二个 Mod 时，修改文件版本仍对应第二个真实条目。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=2)
    mods = tuple(
        replace(source, cache_path=f"C:/legacy/cache/{index}", archive_path=source.source_path, cache_changed=True)
        for index, source in enumerate(scan.mods)
    )
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=mods))
        wizard.search_edit.setText("外观 2")
        select_mod(wizard, 1)
        wizard.source_combo.setCurrentIndex(wizard.source_combo.findData(True))
        assert wizard.controller.items[0].source.source_path == mods[0].archive_path
        assert wizard.controller.items[1].source.source_path == mods[1].cache_path
        assert len(wizard.controller.items) == 2
    finally:
        plugin.deactivate()


def test_selected_preview_is_bounded_and_metadata_stays_plain_text(
    load_migration, migration_context, migration_host, tmp_path
):
    """本地图显示为有界预览，旧版文字不能被当成富文本链接。"""
    from PySide6.QtGui import QImage

    image_path = tmp_path / "preview.png"
    image = QImage(800, 400, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(image_path))
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    scan = make_scan(load_migration("legacy"), count=1)
    source = replace(scan.mods[0], preview_paths=(str(image_path),), description="<a href='file:///secret'>说明</a>")
    try:
        scan_in_wizard(wizard, migration_context, replace(scan, mods=(source,)))
        select_mod(wizard)
        assert not wizard.detail_preview.pixmap().isNull()
        assert wizard.detail_preview.pixmap().width() <= 300
        assert wizard.detail_preview.pixmap().height() <= 140
        assert wizard.detail_text.textFormat() == Qt.TextFormat.PlainText
        assert source.description in wizard.detail_text.text()
        clothing = wizard.tree_proxy.index(0, 0)
        wizard.tree.setCurrentIndex(wizard.tree_proxy.index(0, 0, clothing))
        assert wizard.detail_preview.pixmap().isNull()
        assert "无法显示" in wizard.detail_preview.text()
    finally:
        plugin.deactivate()


def test_wide_review_keeps_tree_problems_and_selected_details_in_view(
    load_migration, migration_context, migration_host, qapp
):
    """宽窗口的详情不能被长问题区挤出初始可视区域。"""
    plugin = load_migration("entry").Plugin()
    plugin.activate(migration_context)
    dialog = plugin._open()
    wizard = dialog.findChild(QWidget, "legacyMigrationWizard")
    legacy = load_migration("legacy")
    scan = make_scan(
        legacy, issues=tuple(legacy.ScanIssue("invalid", "未知诊断", f"问题 {index}") for index in range(11))
    )
    scan = replace(scan, mods=tuple(replace(source, description="旧版详细说明\n" * 60) for source in scan.mods))
    try:
        scan_in_wizard(wizard, migration_context, scan)
        dialog.resize(1100, 850)
        qapp.processEvents()
        select_mod(wizard)
        qapp.processEvents()
        viewport = wizard.scroll_area.viewport()
        assert widget_rect(wizard.detail_card, viewport).intersected(viewport.rect()).height() >= 140
        assert viewport.rect().contains(widget_rect(wizard.detail_title, viewport))
        assert all(wizard.tree_model.issue_index(row).isValid() for row in range(11))
        assert widget_rect(wizard.tree, viewport).intersected(viewport.rect()).height() >= 180
    finally:
        plugin.deactivate()
