"""验证教学示例标识与功能插件的区分、本地化和窄列表布局。"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel

from src.plugins.runtime import manager as runtime
from src.ui import theme
from src.ui.pages.plugins import PluginsPage
from src.utils import i18n
from tests.plugins.test_runtime import write_plugin

pytestmark = pytest.mark.parametrize(("language", "marker"), [("zh-CN", "教学示例"), ("en-US", "Tutorial example")])


@pytest.fixture
def example_page(qtbot, sample_plugins, monkeypatch, language):
    """扫描实际分发的五个插件，不启用业务逻辑，仅展示管理页面。"""
    root = Path(__file__).resolve().parents[2]
    for plugin_id in ("official.legacy-migration", "official.directory-permissions"):
        shutil.copytree(root / "plugins" / plugin_id, sample_plugins / plugin_id)
    write_plugin(sample_plugins, "com.example.inventory", name="教学示例")
    catalog = i18n.I18n(directory=root / "config/languages")
    assert catalog.set_language(language)
    monkeypatch.setattr(i18n, "_i18n", catalog)
    monkeypatch.setattr(runtime, "get_plugins_dir", lambda: sample_plugins)
    manager = runtime.PluginManager(SimpleNamespace(add_event_listener=lambda callback: None))
    manager.discover()
    page = PluginsPage()
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(page)
    page.resize(1100, 720)
    page.set_manager(manager)
    page.show()
    qtbot.wait(20)
    yield page, manager
    page.close()
    manager.shutdown()


@pytest.mark.parametrize(
    ("plugin_id", "is_example"),
    [
        ("example.mod-inventory", True),
        ("example.preset-audit", True),
        ("example.preset-cycle", True),
        ("official.legacy-migration", False),
        ("official.directory-permissions", False),
        ("com.example.inventory", False),
    ],
)
def test_example_badges_distinguish_plugins_in_library_and_details(example_page, qtbot, marker, plugin_id, is_example):
    """列表和详情标注示例用途，但不修改插件名称或误标其他命名空间。"""
    page, manager = example_page
    row = page._entries[plugin_id]
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)
    card = page._detail_cards[plugin_id]
    for widget in (row, card):
        badges = [label for label in widget.findChildren(QLabel, "pluginExampleBadge") if label.isVisibleTo(widget)]
        assert [badge.text() for badge in badges] == ([marker] if is_example else [])
    record = manager.get(plugin_id)
    if is_example:
        assert marker in row.accessibleName()
        assert marker in row.toolTip()
        assert marker not in record.name
        assert record.description.startswith(marker)
        assert record.description in [label.text() for label in card.findChildren(QLabel)]
    else:
        assert row.accessibleName() == record.name


@pytest.mark.parametrize("plugin_id", ["example.mod-inventory", "example.preset-audit", "example.preset-cycle"])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_example_badges_and_titles_fit_narrow_themed_library_rows(example_page, qtbot, qapp, marker, plugin_id, mode):
    """中英文标识完整显示，窄列表下不挤占插件标题或超出点击区域。"""
    page, manager = example_page
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()
    try:
        theme.set_theme(mode)
        theme.set_background_active(False)
        qapp.setStyleSheet(theme.render_qss(mode))
        page.retheme()
        page._splitter.setSizes([250, 750])
        qtbot.wait(20)
        row = page._entries[plugin_id]
        badge = row.findChild(QLabel, "pluginExampleBadge")
        assert badge is not None and badge.isVisibleTo(row)
        assert badge.text() == marker
        assert badge.fontMetrics().horizontalAdvance(marker) <= badge.contentsRect().width()
        assert row._name.text() == manager.get(plugin_id).name
        for control in (row._icon, row._name, badge):
            assert control.height() >= control.minimumSizeHint().height()
            assert row.rect().contains(control.mapTo(row, QPoint(0, 0)))
            assert row.rect().contains(control.mapTo(row, control.rect().bottomRight()))
        assert badge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    finally:
        mode, background, sheet = previous
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(sheet)


def test_example_badge_font_can_shrink_back_without_retaining_old_row_height(example_page, qtbot, qapp, marker):
    """标签字号恢复后，列表行高也应恢复，不能积累旧最小高度。"""
    page, _manager = example_page
    previous_sheet = qapp.styleSheet()
    try:
        qapp.setStyleSheet(theme.render_qss(theme.current_theme()))
        page.retheme()
        qtbot.wait(20)
        row = page._entries["example.mod-inventory"]
        badge = row.findChild(QLabel, "pluginExampleBadge")
        assert badge is not None and badge.text() == marker
        original_style = badge.styleSheet()
        original_height = row.height()
        original_badge_height = badge.minimumHeight()
        badge.setStyleSheet(original_style.replace("font-size: 12px", "font-size: 24px"))
        qtbot.wait(20)
        assert row.height() > original_height
        assert badge.minimumHeight() > original_badge_height
        badge.setStyleSheet(original_style)
        qtbot.wait(20)
        assert badge.minimumHeight() == original_badge_height
        assert row.height() == original_height
    finally:
        qapp.setStyleSheet(previous_sheet)
