"""验证重复检查和结束更新流程不会被长期管理器累积持有。"""

from __future__ import annotations

import gc
import weakref

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtWidgets import QWidget

from src.core.config import Config
from src.services.app_update import manager as manager_module
from src.services.app_update.checker import AppUpdateInfo
from src.ui.dialogs.app_update_dialog import UpdatePromptDialog
from src.ui.dialogs.app_update_widgets import UpdateNoticeDialog, UpdateProgressDialog


def _flush_deletes() -> None:
    """完成排队结果和延迟销毁后再统计对象，避免把正常退场时间误判为泄漏。"""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()


@pytest.fixture
def manager(qapp, temp_data_dir, monkeypatch):
    """建立隔离配置与真实 Qt 父窗口，外部版本检查返回固定无更新结果。"""
    window = QWidget()
    manager = manager_module.AppUpdateManager(window, Config())
    monkeypatch.setattr(manager_module.AppUpdateChecker, "check", lambda self: AppUpdateInfo())
    yield manager
    for worker in window.findChildren(QThread):
        assert worker.wait(3000)
    window.deleteLater()
    _flush_deletes()


def test_repeated_update_checks_release_finished_threads(manager):
    references = []
    for _ in range(4):
        manager.check_for_updates(silent=True)
        worker = manager._check_worker
        references.append(weakref.ref(worker))
        assert worker.wait(3000)
        del worker
        _flush_deletes()
        assert not manager._is_busy
    assert manager.findChildren(manager_module._CheckWorker) == []
    assert all(reference() is None for reference in references)


def test_failed_install_attempts_release_finished_threads(manager, monkeypatch, tmp_path):
    """安装准备失败后允许继续尝试，但不能保留每轮线程及其安装器。"""

    class Installer:
        """在空间检查阶段返回受控失败，不执行真实解压或外部安装。"""

        def __init__(self, path):
            """接收隔离的归档路径，保持真实安装器的构造契约。"""

        def check_disk_space(self):
            """用固定业务失败结束安装准备。"""
            return False, "synthetic failure"

        def cleanup_extract(self):
            """受控安装器没有提取文件。"""

    monkeypatch.setattr(manager_module, "AppUpdateInstaller", Installer)
    monkeypatch.setattr(manager, "_error_box", lambda *args: None)
    references = []
    for _ in range(4):
        manager._install(tmp_path / "update.zip")
        worker = manager._install_worker
        references.append(weakref.ref(worker))
        assert worker.wait(3000)
        del worker
        _flush_deletes()
    assert manager.findChildren(manager_module._InstallWorker) == []
    assert all(reference() is None for reference in references)


@pytest.mark.parametrize("kind", ["prompt", "notice", "error"])
def test_update_modal_dialogs_do_not_accumulate_after_close(manager, monkeypatch, kind):
    dialog_type = UpdatePromptDialog if kind == "prompt" else UpdateNoticeDialog
    original_exec = dialog_type.exec

    def close_dialog(dialog):
        """让真实模态对话框关闭，保留 Qt 父对象对窗口的所有权行为。"""
        QTimer.singleShot(0, dialog.reject)
        return original_exec(dialog)

    monkeypatch.setattr(dialog_type, "exec", close_dialog)
    for _ in range(4):
        if kind == "prompt":
            manager._prompt_update(AppUpdateInfo())
        elif kind == "notice":
            manager._info_box("sample", "sample")
        else:
            manager._error_box("sample", "sample")
        _flush_deletes()
    assert len(manager._win.findChildren(dialog_type)) <= 1


@pytest.mark.parametrize("kind", ["download", "install", "cancel"])
def test_ended_update_progress_releases_its_dialog(manager, kind):
    for _ in range(4):
        dialog = UpdateProgressDialog("sample", "sample", manager._win, cancellable=kind != "install")
        if kind == "install":
            manager._install_progress = dialog
            manager._close_install_progress()
        else:
            manager._progress = dialog
            if kind == "cancel":
                dialog.canceled.connect(manager._on_download_cancel)
                dialog.cancel()
            else:
                manager._close_progress()
        del dialog
        _flush_deletes()
    assert manager._win.findChildren(UpdateProgressDialog) == []
