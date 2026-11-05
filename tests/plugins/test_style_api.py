"""验证插件语义样式的主题同步、权限与释放边界。"""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLineEdit, QPushButton

from src.plugin_sdk import PermissionDenied, PluginError
from src.plugin_sdk.ui_ext import UIExtensionPoints
from src.ui import settings_style, theme
from tests.plugins.test_appearance import palette as palette
from tests.plugins.test_qt_widgets import ui_host as ui_host


def test_style_tracks_theme_without_recreating_or_losing_input(ui_host, qtbot):
    widget = QLineEdit("保留的路径")
    qtbot.addWidget(widget)
    ui = UIExtensionPoints("example", None, ui_host, granted=frozenset({"ui:views"}))
    binding = ui.apply_style(widget, "input")
    first = widget.styleSheet()
    assert settings_style.color("text") in first
    theme.set_theme("dark")
    ui_host.retheme()
    assert widget.styleSheet() != first
    assert settings_style.color("text") in widget.styleSheet()
    assert widget.text() == "保留的路径"
    binding.dispose()
    detached = widget.styleSheet()
    theme.set_theme("light")
    ui_host.retheme()
    assert widget.styleSheet() == detached


def test_style_bindings_retire_on_widget_destruction_and_plugin_cleanup(ui_host, qtbot):
    button = QPushButton("开始迁移")
    qtbot.addWidget(button)
    ui = UIExtensionPoints("example", None, ui_host, granted=frozenset({"ui:views"}))
    ui.apply_style(button, "primary_button")
    assert settings_style.color("accent") in button.styleSheet()
    ui_host.cleanup_plugin("example")
    stopped = button.styleSheet()
    theme.set_theme("dark")
    ui_host.retheme()
    assert button.styleSheet() == stopped
    ui.apply_style(button, "button")
    button.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    ui_host.retheme()


def test_style_api_rejects_unauthorized_or_retired_context(ui_host, qtbot):
    button = QPushButton("开始迁移")
    qtbot.addWidget(button)
    ui = UIExtensionPoints("example", None, ui_host)
    with pytest.raises(PermissionDenied):
        ui.apply_style(button, "button")
    ui = UIExtensionPoints("example", None, ui_host, granted=frozenset({"ui:views"}))
    with pytest.raises(ValueError):
        ui.apply_style(button, "unknown")
    ui._close()
    with pytest.raises(PluginError):
        ui.apply_style(button, "button")
