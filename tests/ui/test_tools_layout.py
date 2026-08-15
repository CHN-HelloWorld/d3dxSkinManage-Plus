from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from src.plugins.runtime.contributions import Contribution
from src.ui.pages.tools import ToolsPage


def test_plugin_tools_only_exist_for_executable_contributions(qtbot):
    page = ToolsPage()
    qtbot.addWidget(page)
    executed = []
    blank = Contribution("p", "Example", "toolsPage", {"command": " \t"})
    for items in ([], [blank]):
        page.refresh_plugin_tools(items, executed.append)
        assert page._plugin_group is None
    first = Contribution("p", "<b>Example</b>", "toolsPage", {"command": "p.first", "title": "<b>First</b>"})
    second = Contribution("q", "Other", "toolsPage", {"command": "q.second", "title": "Second"})
    page.refresh_plugin_tools([blank, first, second], executed.append)
    buttons = page._plugin_group.findChildren(QPushButton)
    assert len(buttons) == 2
    for button in buttons:
        button.click()
    assert executed == ["p.first", "q.second"]
    labels = page._plugin_group.findChildren(QLabel)
    assert any(label.text() == first.title for label in labels)
    assert any(first.plugin_name in label.text() for label in labels)
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    with qtbot.waitSignal(page.manage_plugins_requested):
        page.findChild(QPushButton, "managePluginsButton").click()
    page.refresh_plugin_tools([blank], executed.append)
    assert page._plugin_group is None
    assert page.findChild(QPushButton, "managePluginsButton") is None
    page.refresh_plugin_tools([second], executed.append)
    page._plugin_group.findChild(QPushButton).click()
    assert executed == ["p.first", "q.second", "q.second"]


def test_maintenance_actions_remain_available_without_game_context(qtbot):
    page = ToolsPage()
    qtbot.addWidget(page)
    for name, signal in (
        ("rebuildIndexButton", page.rebuild_index_requested),
        ("viewLogsButton", page.view_logs_requested),
    ):
        button = page.findChild(QPushButton, name)
        assert button is not None and button.isEnabled()
        with qtbot.waitSignal(signal):
            button.click()
