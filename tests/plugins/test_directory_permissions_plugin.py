"""验证按需权限修复插件的扫描边界、取消以及宿主窗口生命周期。"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget

from src.plugin_sdk import BackgroundTask, Disposable, TaskContext, TaskResult
from src.plugin_sdk.i18n import TranslationFacade
from src.plugin_sdk.tasks import TaskCancelled
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.module_loader import PluginModuleLoader
from src.plugins.runtime.packaging import pack_plugin
from src.plugins.runtime.registry import PluginState
from src.plugins.runtime.validation import validate_package
from src.ui.plugin_ui_host import PluginUIHost
from src.utils import i18n
from src.utils.directory_permissions import restore_directory_inheritance
from tests.plugins.test_runtime import env as env

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins/official.directory-permissions"


@pytest.fixture
def repair_modules():
    """通过真实加载器载入独立插件，测试结束撤销命名空间。"""
    manifest = validate_package(PLUGIN_DIR)
    loader = PluginModuleLoader(manifest)
    try:
        entry = loader.load()
        permissions = importlib.import_module(loader.namespace + ".permissions")
        yield entry, permissions, manifest
    finally:
        loader.unload()


@pytest.fixture
def repair_root(tmp_path):
    """仅在隔离测试目录内恢复父目录继承，使旧私有 ACL 与正常目录可区分。"""
    if os.name != "nt" or sys.version_info < (3, 13):
        pytest.skip("Windows Python 3.13 起临时目录采用私有 ACL")
    restore_directory_inheritance(tmp_path.parent)
    restore_directory_inheritance(tmp_path)
    return tmp_path


def task_context(cancelled=None):
    """创建不依赖界面的真实任务上下文，丢弃未使用的进度。"""
    return TaskContext(cancelled or Event(), lambda progress: None)


def test_repair_is_limited_to_runtime_directories_and_is_idempotent(repair_modules, repair_root):
    _, permissions, _ = repair_modules
    parent = repair_root / "data" / "game" / "preview_cache"
    parent.mkdir(parents=True)
    old = Path(tempfile.mkdtemp(dir=parent))
    (old / "preview.png").write_bytes(b"preview")
    outside = Path(tempfile.mkdtemp(dir=repair_root))
    result = permissions.repair_permissions(task_context(), app_dir=repair_root)
    assert result.repaired == 1
    assert result.scanned == 4
    assert result.errors == ()
    assert (old / "preview.png").read_bytes() == b"preview"
    assert not restore_directory_inheritance(old, only_private=True)
    assert restore_directory_inheritance(outside, only_private=True)
    repeated = permissions.repair_permissions(task_context(), app_dir=repair_root)
    assert repeated.repaired == 0
    assert repeated.errors == ()


def test_cancelled_repair_does_not_modify_a_private_directory(repair_modules, repair_root):
    _, permissions, _ = repair_modules
    parent = repair_root / "data"
    parent.mkdir()
    old = Path(tempfile.mkdtemp(dir=parent))
    cancelled = Event()
    cancelled.set()
    with pytest.raises(TaskCancelled):
        permissions.repair_permissions(task_context(cancelled), app_dir=repair_root)
    assert restore_directory_inheritance(old, only_private=True)


def test_repair_does_not_follow_directory_links(repair_modules, repair_root):
    """目录联接无需符号链接权限，也必须在扫描重解析点时跳过。"""
    import _winapi

    _, permissions, _ = repair_modules
    outside = Path(tempfile.mkdtemp(dir=repair_root))
    (repair_root / "data").mkdir()
    _winapi.CreateJunction(str(outside), str(repair_root / "data" / "linked"))
    result = permissions.repair_permissions(task_context(), app_dir=repair_root)
    assert result.scanned == 1
    assert result.repaired == 0
    assert restore_directory_inheritance(outside, only_private=True)


@pytest.mark.parametrize("access", ["/grant", "/deny"])
def test_custom_access_entries_are_not_changed(repair_modules, repair_root, access):
    """额外的允许或拒绝条目不能被误判为旧临时目录权限。"""
    _, permissions, _ = repair_modules
    parent = repair_root / "data"
    parent.mkdir()
    old = Path(tempfile.mkdtemp(dir=parent))
    subprocess.run(["icacls.exe", str(old), access, "*S-1-5-11:(OI)(CI)(WD)"], check=True, capture_output=True)
    result = permissions.repair_permissions(task_context(), app_dir=repair_root)
    assert result.scanned == 2
    assert result.repaired == 0
    assert result.errors == ()
    current = subprocess.run(["icacls.exe", str(old)], check=True, capture_output=True)
    assert b"(I)" not in current.stdout


class RepairHost:
    """托管真实 Qt 对话框，仅替代应用级导航与任务调度边界。"""

    def __init__(self):
        """记录命令和任务，供测试显式投递宿主最终结果。"""
        self.commands = {}
        self.jobs = []
        self.cancel_requests = []
        self.dialogs = []

    def register(self, command, callback):
        """保存插件注册的按需命令。"""
        self.commands[command] = callback

    def apply_style(self, widget, role):
        """界面尺寸不依赖配色，返回真实可释放订阅。"""
        return Disposable(lambda: None)

    def show_dialog(self, widget, title="", modal=False):
        """将插件控件挂到实际对话框并交由 Qt 测试夹具清理。"""
        dialog = QDialog()
        dialog.setWindowTitle(title)
        QVBoxLayout(dialog).addWidget(widget)
        self.dialogs.append(dialog)
        dialog.show()
        return dialog

    def run(self, worker, *, title):
        """保持任务未完成状态，避免测试启动真实工作线程。"""
        cancelled = Event()
        task = BackgroundTask(title=title, cancel=cancelled.set)
        self.jobs.append((worker, task))
        self.cancel_requests.append(cancelled)
        return task


@pytest.fixture
def repair_session(repair_modules, qapp, tmp_path):
    """构造插件最小宿主边界，保留实际任务订阅和控件行为。"""
    entry, permissions, manifest = repair_modules
    host = RepairHost()
    ctx = SimpleNamespace(
        plugin_id=manifest.id,
        plugin_dir=tmp_path / "plugins" / manifest.id,
        commands=host,
        ui=host,
        tasks=host,
        i18n=TranslationFacade(PluginLocalizer(manifest)),
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    plugin = entry.Plugin()
    plugin.activate(ctx)
    try:
        yield plugin, host, permissions, ctx
    finally:
        plugin.deactivate()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


def test_activation_and_opening_do_not_start_a_scan(repair_session):
    plugin, host, _, _ = repair_session
    assert host.jobs == []
    dialog = plugin._open()
    assert dialog is plugin._open()
    assert len(host.dialogs) == 1
    assert host.jobs == []
    panel = plugin._panel
    assert panel.start_button.isEnabled()
    assert panel.close_button.isEnabled()
    panel.start_button.click()
    assert not panel.start_button.isEnabled()
    assert panel.close_button.isEnabled()
    assert len(host.jobs) == 1


@pytest.mark.parametrize("language", ["zh-CN", "en-US"])
def test_repair_dialog_is_wide_compact_and_centered_in_the_real_host(
    repair_session, monkeypatch, qtbot, qapp, language
):
    """真实宿主中的初始内容与最终弹窗均保持横向比例，不能依赖显示后才修正窄长布局。"""
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", language)
    plugin, _, _, ctx = repair_session
    window = QWidget()
    qtbot.addWidget(window)
    window.setGeometry(120, 100, 1000, 700)
    window.show()
    host = PluginUIHost(window)
    ctx.ui = SimpleNamespace(
        apply_style=partial(host.apply_style, ctx.plugin_id),
        show_dialog=partial(host.show_dialog, ctx.plugin_id),
    )
    try:
        dialog = plugin._open()
        qapp.processEvents()
        panel = plugin._panel
        assert panel.sizeHint().width() >= panel.sizeHint().height() * 1.75
        assert dialog.width() >= dialog.height() * 1.75
        available = window.screen().availableGeometry()
        assert available.contains(dialog.frameGeometry())
        assert (dialog.frameGeometry().center() - window.frameGeometry().center()).manhattanLength() <= 2
        assert panel.start_button.isVisible()
        assert panel.rect().contains(panel.start_button.geometry())
        assert panel.close_button.text() == ("Close" if language == "en-US" else "关闭")
        assert panel.close_button.isVisible()
        assert panel.rect().contains(panel.close_button.geometry())
        assert panel.details.height() >= 100
    finally:
        plugin.deactivate()
        host.cleanup_plugin(ctx.plugin_id)
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_repair_result_and_errors_remain_visible_and_can_be_retried(repair_session):
    plugin, host, permissions, ctx = repair_session
    plugin._open()
    panel = plugin._panel
    panel.start_button.click()
    result = permissions.RepairResult(scanned=12, repaired=3, errors=("data/cache: denied",))
    host.jobs[-1][1]._finish(TaskResult(value=result))
    assert panel.start_button.isEnabled()
    assert panel.close_button.isEnabled()
    assert panel.status_label.text() == ctx.i18n.t(
        "已检查 {scanned} 个目录，修复 {repaired} 个，失败 {errors} 个。", scanned=12, repaired=3, errors=1
    )
    assert "data/cache: denied" in panel.details.toPlainText()
    panel.start_button.click()
    assert len(host.jobs) == 2


@pytest.mark.parametrize("phase", ["idle", "running", "finished"])
def test_close_button_remains_available_and_stops_only_pending_work(repair_session, phase):
    """关闭在空闲、扫描及完成时均可用，扫描时关闭通过宿主任务的取消信号停止后续工作。"""
    plugin, host, permissions, _ = repair_session
    dialog = plugin._open()
    panel = plugin._panel
    if phase != "idle":
        panel.start_button.click()
        if phase == "finished":
            host.jobs[-1][1]._finish(TaskResult(value=permissions.RepairResult(1, 1, ())))
    assert panel.close_button.isEnabled()
    assert not any(button.text() in {"取消", "Cancel"} for button in panel.findChildren(QPushButton))
    panel.close_button.click()
    assert not dialog.isVisible()
    assert plugin._panel is None
    assert plugin._dialog is None
    if phase != "idle":
        assert host.cancel_requests[-1].is_set() == (phase == "running")
    else:
        assert host.jobs == []


def test_closing_a_running_dialog_ignores_late_task_results(repair_session, qapp):
    plugin, host, permissions, _ = repair_session
    dialog = plugin._open()
    panel = plugin._panel
    panel.start_button.click()
    task = host.jobs[-1][1]
    dialog.close()
    assert plugin._panel is None
    assert plugin._dialog is None
    task._finish(TaskResult(value=permissions.RepairResult(1, 1, ())))
    reopened = plugin._open()
    assert reopened is not dialog
    assert plugin._panel.start_button.isEnabled()
    assert len(host.jobs) == 1
    qapp.processEvents()


def test_zip_install_enable_and_uninstall_keep_repair_opt_in(env, tmp_path, monkeypatch):
    """真实安装和授权后仍等待命令，卸载可选插件无需启动任何修复。"""
    manager, external, _ = env
    archive = pack_plugin(PLUGIN_DIR, tmp_path / "permission-repair.zip")
    installed = manager.install_archive(archive)
    assert installed.directory == external / "official.directory-permissions"
    record = manager.get(installed.plugin_id)
    assert record.instance is None
    assert manager.trust(record.id)
    assert manager.enable(record.id)
    assert record.state is PluginState.READY
    assert record.instance is None
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    assert record.name == "Directory Permission Repair"
    assert manager.uninstall(record.id)
    assert not installed.directory.exists()
