import subprocess
import sys

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QTextBrowser, QWidget

from src.services.app_update.checker import AppUpdateInfo
from src.ui.dialogs.app_update_dialog import UpdatePromptDialog
from src.ui.dialogs.app_update_widgets import UpdateNoticeDialog
from tests.qt_dialogs import interact
from tests.ui.test_transient_ui_reuse import _flush

pytestmark = pytest.mark.usefixtures("temp_data_dir")


def test_dialog_can_be_imported_before_the_update_manager():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.ui.dialogs.app_update_dialog import UpdatePromptDialog; "
            "from src.services.app_update import AppUpdateManager",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def version_info(*, has_update=True):
    return {
        "has_update": has_update,
        "current_version": "1.0.0" if has_update else "2.0.0",
        "latest_version": "2.0.0",
        "release_name": "Spring release",
        "changelog": "Latest improvements",
        "download_url": "https://example.com/2.0.0/app.zip",
        "download_size": 1024,
        "releases": [
            {
                "version": "2.0.0",
                "tag_name": "v2.0.0",
                "release_name": "Spring release",
                "changelog": "Latest improvements",
                "published_at": "2026-10-27T02:00:00Z",
            },
            {
                "version": "1.0.0",
                "tag_name": "v1.0.0",
                "release_name": "Original release",
                "changelog": "Older release notes",
                "published_at": "2026-09-01T02:00:00Z",
            },
        ],
    }


def test_selecting_old_release_changes_notes_but_keeps_latest_update_action(qtbot):
    dialog = UpdatePromptDialog(version_info())
    qtbot.addWidget(dialog)
    dialog.show()
    versions = dialog.findChild(QListWidget)
    assert versions is not None, "The update prompt needs a release history selector"
    qtbot.mouseClick(
        versions.viewport(), Qt.MouseButton.LeftButton, pos=versions.visualItemRect(versions.item(1)).center()
    )
    assert "Older release notes" in dialog.findChild(QTextBrowser).toPlainText()
    action = dialog.findChild(QPushButton, "updateAction")
    assert "2.0.0" in action.text()
    qtbot.mouseClick(action, Qt.MouseButton.LeftButton)
    assert dialog.user_action == "update"
    assert dialog._info["download_url"] == "https://example.com/2.0.0/app.zip"


def test_current_version_offers_history_without_install_or_skip(qtbot):
    dialog = UpdatePromptDialog(version_info(has_update=False))
    qtbot.addWidget(dialog)
    dialog.show()
    versions = dialog.findChild(QListWidget)
    assert versions is not None
    versions.setCurrentRow(1)
    assert "Older release notes" in dialog.findChild(QTextBrowser).toPlainText()
    assert not dialog.findChild(QPushButton, "updateAction").isVisible()
    assert not dialog.findChild(QPushButton, "skipAction").isVisible()


def test_legacy_info_without_history_keeps_latest_notes(qtbot):
    info = version_info()
    del info["releases"]
    dialog = UpdatePromptDialog(info)
    qtbot.addWidget(dialog)
    assert "Latest improvements" in dialog.findChild(QTextBrowser).toPlainText()


@pytest.mark.parametrize("finish", ["close", "reject", "cancel"])
def test_user_cancel_is_reported_once(qtbot, finish):
    from src.ui.dialogs.app_update_widgets import UpdateProgressDialog

    dialog = UpdateProgressDialog("下载更新", "正在下载更新包…", cancellable=True)
    qtbot.addWidget(dialog)
    dialog.show()
    cancelled = QSignalSpy(dialog.canceled)
    getattr(dialog, finish)()
    dialog.close()
    assert cancelled.count() == 1
    assert not dialog.isVisible()


def test_successful_progress_close_does_not_cancel_installation(qtbot):
    from src.ui.dialogs.app_update_widgets import UpdateProgressDialog

    dialog = UpdateProgressDialog("下载更新", "正在下载更新包…", cancellable=True)
    qtbot.addWidget(dialog)
    dialog.show()
    cancelled = QSignalSpy(dialog.canceled)
    dialog.setValue(100)
    assert dialog.isVisible(), "100% downloaded still needs integrity validation"
    dialog.finish()
    assert cancelled.count() == 0
    assert not dialog.isVisible()


def test_hiding_install_preparation_does_not_emit_cancel(qtbot):
    from src.ui.dialogs.app_update_widgets import UpdateProgressDialog

    dialog = UpdateProgressDialog("安装更新", "正在准备安装…", cancellable=False)
    qtbot.addWidget(dialog)
    dialog.show()
    cancelled = QSignalSpy(dialog.canceled)
    dialog.close()
    assert cancelled.count() == 0
    assert not dialog.isVisible()


def test_manual_current_version_check_opens_release_history(qtbot, monkeypatch):
    from src.services.app_update.manager import AppUpdateManager

    opened = []

    def read_dialog(dialog):
        qtbot.addWidget(dialog)
        opened.append(dialog.findChild(QTextBrowser).toPlainText())
        return 0

    monkeypatch.setattr(UpdatePromptDialog, "exec", read_dialog)
    manager = AppUpdateManager()
    # 避免模态提示阻塞，以检查实际选择的处理路径。
    monkeypatch.setattr(manager, "_info_box", lambda *_: None)
    info = AppUpdateInfo(**version_info(has_update=False))
    manager._on_check_finished(info)
    assert opened == ["Latest improvements"]
    manager.deleteLater()


def test_skip_while_browsing_old_notes_skips_latest_version(qtbot, monkeypatch):
    from src.services.app_update.manager import AppUpdateManager

    def choose_skip(dialog):
        qtbot.addWidget(dialog)
        selector = dialog.findChild(QListWidget)
        assert selector is not None
        selector.setCurrentRow(1)
        dialog.findChild(QPushButton, "skipAction").click()
        return dialog.result()

    monkeypatch.setattr(UpdatePromptDialog, "exec", choose_skip)
    manager = AppUpdateManager()
    manager._prompt_update(AppUpdateInfo(**version_info()))
    assert manager._config.get("app_update.skipped_version") == "2.0.0"
    manager.deleteLater()


def test_update_state_labels_use_no_emoji(qtbot):
    from src.ui.dialogs.app_update_widgets import UpdateNoticeDialog

    dialogs = [UpdatePromptDialog(version_info()), UpdateNoticeDialog("下载失败", "无法下载更新包。", error=True)]
    for dialog in dialogs:
        qtbot.addWidget(dialog)
        for label in dialog.findChildren(QLabel):
            assert not any(0x1F000 <= ord(char) <= 0x1FAFF or ord(char) in (0x2705, 0x274C) for char in label.text())


def test_download_speed_is_visible_even_when_total_size_is_unknown(qtbot):
    from src.ui.dialogs.app_update_widgets import UpdateProgressDialog

    dialog = UpdateProgressDialog("下载更新", "正在下载更新包…")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.setMaximum(0)
    dialog.setDownloadSpeed(2.5 * 1024 * 1024)
    assert "2.5 MB/s" in dialog.findChild(QLabel, "downloadSpeed").text()
    dialog.setDownloadSpeed(0)
    assert "0 B/s" in dialog.findChild(QLabel, "downloadSpeed").text()


@pytest.mark.parametrize("kind", ["info", "error"])
def test_update_notices_release_closed_dialogs(qapp, temp_data_dir, kind):
    from src.services.app_update.manager import AppUpdateManager

    host = QWidget()
    manager = AppUpdateManager(host)
    dialogs = []

    def close(dialog):
        """记录真实更新通知并通过关闭路径退出模态循环。"""
        dialogs.append(dialog)
        dialog.reject()

    try:
        for _ in range(4):
            with interact(UpdateNoticeDialog, close):
                getattr(manager, f"_{kind}_box")("标题", "正文")
            _flush(qapp)

        assert not host.findChildren(UpdateNoticeDialog)
        assert all(not shiboken6.isValid(dialog) for dialog in dialogs)
    finally:
        manager.deleteLater()
        host.deleteLater()
        _flush(qapp)
