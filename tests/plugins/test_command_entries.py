"""验证插件详情中的命令归属、按需激活和停用后的入口清理。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from src.ui.pages.plugins import PluginCard, PluginsPage
from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_runtime import env as env


def test_plugin_details_execute_only_their_own_commands_without_eager_activation(env, qtbot):
    manager, directory, _ = env
    for plugin_id in ("test.first", "test.second"):
        lazy_plugin(
            directory,
            plugin_id,
            contributes={"commands": [{"id": plugin_id + ".run", "title": "Run " + plugin_id}]},
        )
    prepare(manager, "test.first", "test.second")
    page = PluginsPage()
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(page)
    page.set_manager(manager)
    page.show()
    card = next(card for card in page.findChildren(PluginCard) if card.isVisible())
    buttons = card.findChildren(QPushButton, "pluginCommandButton")
    assert [button.text() for button in buttons] == ["Run test.first"]
    assert all(record.instance is None for record in manager.records)

    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    assert manager.get("test.first").instance.calls == ["command"]
    assert manager.get("test.second").instance is None

    manager.disable("test.first")
    qtbot.wait(20)
    assert not [button for button in page.findChildren(QPushButton, "pluginCommandButton") if button.isVisible()]
    page.close()
