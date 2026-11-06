from unittest.mock import patch

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QWidget

from src.core.config import Config
from src.ui.window_close_behavior import WindowCloseBehavior


def test_default_close_keeps_normal_shutdown(qtbot, temp_data_dir):
    window = QWidget()
    qtbot.addWidget(window)
    behavior = WindowCloseBehavior(window, Config())
    assert not behavior.handle_close(QCloseEvent())


def test_tray_close_hides_and_restore_shows_window(qtbot, temp_data_dir):
    window = QWidget()
    qtbot.addWidget(window)
    config = Config()
    config.set("ui.close_behavior", "tray")
    behavior = WindowCloseBehavior(window, config)
    window.show()
    event = QCloseEvent()
    with patch("src.ui.window_close_behavior.QSystemTrayIcon.isSystemTrayAvailable", return_value=True):
        assert behavior.handle_close(event)
    assert not event.isAccepted()
    assert not window.isVisible()
    assert behavior.tray.isVisible()
    behavior.restore()
    assert window.isVisible()
    behavior.dispose()


def test_missing_tray_falls_back_to_normal_close(qtbot, temp_data_dir):
    window = QWidget()
    qtbot.addWidget(window)
    config = Config()
    config.set("ui.close_behavior", "tray")
    behavior = WindowCloseBehavior(window, config)
    with patch("src.ui.window_close_behavior.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
        assert not behavior.handle_close(QCloseEvent())


def test_updater_exit_is_not_hidden_to_tray(qtbot, temp_data_dir):
    window = QWidget()
    qtbot.addWidget(window)
    config = Config()
    config.set("ui.close_behavior", "tray")
    behavior = WindowCloseBehavior(window, config)
    window.setProperty("applicationExitRequested", True)
    assert not behavior.handle_close(QCloseEvent())


def test_tray_exit_uses_normal_close_and_cancel_restores_behavior(qtbot, temp_data_dir):
    window = QWidget()
    qtbot.addWidget(window)
    config = Config()
    config.set("ui.close_behavior", "tray")
    behavior = WindowCloseBehavior(window, config)
    with patch.object(window, "close") as close:
        behavior.request_exit()
    close.assert_called_once()
    assert not behavior.handle_close(QCloseEvent())
    behavior.cancel_exit()
    with patch("src.ui.window_close_behavior.QSystemTrayIcon.isSystemTrayAvailable", return_value=True):
        assert behavior.handle_close(QCloseEvent())
    behavior.dispose()
