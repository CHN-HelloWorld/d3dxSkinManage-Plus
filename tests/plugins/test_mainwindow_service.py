from collections import deque
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import (
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

from src.ui.main_window import MainWindow
from src.ui.pages.plugins import PluginsPage
from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings
from src.ui.widgets.task_center import TaskCenterStack
from tests.plugins.test_operations import library as library

pytestmark = pytest.mark.usefixtures("trusted_sample_plugins")


class PluginWindow(MainWindow):
    def __init__(self, manager):
        QMainWindow.__init__(self)
        self._mod_manager = manager
        self._game_service = manager._game_service
        self._game_page = QWidget(self)
        self._stack = QStackedWidget(self)
        self._stack.addWidget(QWidget())
        self._nav = QListWidget(self)
        self._plugins_page = PluginsPage(self)
        self._panel_stack = TaskCenterStack()
        self._deploy_environment_dirty = False
        self._pending_gb_installs = deque()
        self._watcher_changed_paths = set()
        self._mod_update_service = SimpleNamespace(active_update_ids=lambda **kw: [])
        self.refreshes = []
        self.messages = []
        self.preset_refreshes = 0
        self._shortcut_settings = ShortcutSettings(self)
        self._shortcut_dispatcher = ShortcutDispatcher(self._shortcut_settings, self)
        self._init_plugins()

    def _refresh_plugin_tools(self):
        pass

    def _reload_overview_level(self, game_id):
        self.refreshes.append(game_id)

    def _suppress_watcher(self):
        pass

    def _refresh_store_installed_state(self):
        pass

    def _refresh_gallery(self):
        """精简宿主未构造画廊，插件完成信号只验证其余反馈入口。"""
        pass

    def _refresh_preset_combo(self):
        self.preset_refreshes += 1

    def _ensure_toast(self):
        return SimpleNamespace(done=lambda message, **kw: self.messages.append(message))

    def showEvent(self, event):
        """显示真实插件控件，通知仍保留在队列中供测试观察。"""
        QMainWindow.showEvent(self, event)

    def closeEvent(self, event):
        if not self._plugin_operations.shutdown():
            event.ignore()
            return
        self._plugin_manager.shutdown()
        from src.ui import plugin_menu

        plugin_menu.set_provider(None)
        QMainWindow.closeEvent(self, event)


def test_metadata_only_preset_result_refreshes_preset_selector(library, qtbot):
    manager, _ = library
    window = PluginWindow(manager)
    qtbot.addWidget(window)
    assert window._plugin_manager.activate("example.preset-cycle")
    context = window._plugin_manager.get("example.preset-cycle").context
    task = context.presets.create("Created by plugin")
    assert not task.done and not window._game_page.isEnabled()
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.succeeded_ids == ()
    assert context.presets.get_preset(task.result.preset_id).name == "Created by plugin"
    assert window.preset_refreshes == 1
    assert window.refreshes == []
    assert window._game_page.isEnabled()
    window.close()
