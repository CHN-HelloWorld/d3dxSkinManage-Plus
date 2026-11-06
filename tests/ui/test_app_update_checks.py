"""验证程序更新检查策略、检测反馈及结束后的界面恢复。"""

import time
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPropertyAnimation, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from src.core.config import Config
from src.services.app_update import manager as manager_module
from src.services.app_update.checker import AppUpdateChecker, AppUpdateInfo
from src.ui import anim
from src.ui.dialogs.app_update_dialog import UpdatePromptDialog
from src.ui.main_window import MainWindow
from src.ui.pages.settings import SettingsPage
from src.ui.widgets.spinner import Spinner
from src.utils.i18n import t as tr


@pytest.fixture
def update_manager(qtbot, temp_data_dir, monkeypatch):
    """保留真实更新检查线程与结果处理，仅替换网络读取和模态提示。"""
    release = {
        "tag_name": "v1.0.0",
        "name": "Version 1.0.0",
        "body": "Release notes",
        "published_at": "2026-10-27T02:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [],
    }

    def fetch_release(self, api_path, timeout, proxy):
        """提供版本和历史接口的固定响应，避免用例访问网络。"""
        if api_path.endswith("/latest"):
            return release
        return [release]

    monkeypatch.setattr(AppUpdateChecker, "_race_fetch", fetch_release)
    monkeypatch.setattr(manager_module, "APP_VERSION", "1.0.0")
    window = QWidget()
    qtbot.addWidget(window)
    config = Config()
    config.set("app_update.repo", "example/app")
    manager = manager_module.AppUpdateManager(window, config)
    monkeypatch.setattr(manager, "_prompt_update", lambda _info: None)
    try:
        yield manager
    finally:
        if manager._check_worker is not None:
            assert manager._check_worker.wait(1000)


def test_manual_check_ignores_startup_preference_and_throttle(qtbot, update_manager):
    config = Config()
    config.set("app_update.on_startup", False)
    config.set("app_update.last_check_time", time.time())
    started = QSignalSpy(update_manager.check_started)
    finished = QSignalSpy(update_manager.check_finished)

    update_manager.check_for_updates()

    assert started.count() == 1
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)
    result = finished.at(0)[0]
    assert result["error"] is None
    assert result["latest_version"] == "1.0.0"
    assert result["releases"][0]["changelog"] == "Release notes"


@pytest.mark.parametrize(
    "on_startup,elapsed,expected_checks",
    [(False, 86401, 0), (True, 60, 0), (True, 86401, 1)],
)
def test_startup_check_respects_preference_and_interval(qtbot, update_manager, on_startup, elapsed, expected_checks):
    config = Config()
    config.set("app_update.on_startup", on_startup)
    config.set("app_update.last_check_time", time.time() - elapsed)
    started = QSignalSpy(update_manager.check_started)
    finished = QSignalSpy(update_manager.check_finished)

    update_manager.check_on_startup()

    assert started.count() == expected_checks
    if expected_checks:
        qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)
        assert finished.at(0)[0]["latest_version"] == "1.0.0"


class _UpdateFeedbackHost(QWidget):
    """保留主窗口的程序更新接线和整页重建，仅承载真实程序更新页。"""

    _on_app_update_check_requested = MainWindow._on_app_update_check_requested
    _rebuild_settings_page_full = MainWindow._rebuild_settings_page_full

    def __init__(self, manager):
        """建立程序更新页与页面容器，复用主窗口对更新管理器的信号绑定。"""
        super().__init__()
        self._app_update_manager = manager
        self._shortcut_settings = SimpleNamespace()
        self._stack = QStackedWidget(self)
        QVBoxLayout(self).addWidget(self._stack)
        self._settings_page = SettingsPage(self._stack, shortcuts=self._shortcut_settings)
        self._stack.addWidget(self._settings_page)
        self._background_controller = SimpleNamespace(playback=self._ignore)
        for name in (
            "_on_settings_changed",
            "_on_theme_changed",
            "_on_font_changed",
            "_on_language_changed",
            "_on_background_changed",
            "_refresh_bg_overlay",
            "_on_bg_scrim_live",
            "_on_bg_blur_live",
            "_wire_settings_downloads",
        ):
            setattr(self, name, self._ignore)
        self._sync_app_update_check_state = MainWindow._sync_app_update_check_state.__get__(self)
        MainWindow._connect_app_update_manager(self)
        self._settings_page.app_update_check_requested.connect(self._on_app_update_check_requested)

    def _ignore(self, *args, **kwargs):
        """隔离与程序更新检测无关的设置通知和下载数据接线。"""


@pytest.fixture
def update_feedback(qtbot, update_manager, monkeypatch):
    """仅构建真实更新页，用受控线程屏障验证开始、结束及页面重建流程。"""
    state = SimpleNamespace(release=Event(), result=AppUpdateInfo(current_version="1.0.0", latest_version="1.0.0"))

    def setup_update_page(page):
        """保留真实更新控件与旋转动画，不构建其他设置分类。"""
        QVBoxLayout(page).addWidget(page._build_update_page())

    def skip_unrelated_settings(page):
        """跳过整页配置回填、自动保存接线及导航布局刷新。"""

    def check(checker):
        """等待用例释放检查线程，再返回业务结果或模拟检查器异常。"""
        if not state.release.wait(3):
            raise TimeoutError("检查测试未释放线程")
        if isinstance(state.result, Exception):
            raise state.result
        return state.result

    monkeypatch.setattr(SettingsPage, "_setup_ui", setup_update_page)
    for name in ("_load_settings", "_wire_autosave", "_refresh_font_layout"):
        monkeypatch.setattr(SettingsPage, name, skip_unrelated_settings)
    monkeypatch.setattr(AppUpdateChecker, "check", check)
    monkeypatch.setattr(update_manager, "_error_box", lambda *_args: None)
    host = _UpdateFeedbackHost(update_manager)
    qtbot.addWidget(host)
    try:
        yield host, state
    finally:
        state.release.set()
        worker = update_manager._check_worker
        if worker is not None:
            assert worker.wait(1000)


def _assert_check_idle(page):
    """检查按钮文案、可点击状态及旋转动画均已恢复。"""
    assert page._app_upd_check_btn.isEnabled()
    assert page._app_upd_check_btn.text() == tr("检查程序更新")
    assert page._app_upd_check_spinner.isHidden()
    assert page._app_upd_check_spinner._anim.state() == QPropertyAnimation.State.Stopped


def test_feedback_button_blocks_duplicates_until_the_check_finishes(qtbot, update_feedback):
    host, state = update_feedback
    page = host._settings_page
    manager = host._app_update_manager
    started = QSignalSpy(manager.check_started)
    finished = QSignalSpy(manager.check_finished)

    page._app_upd_check_btn.click()

    assert started.count() == 1
    assert not page._app_upd_check_btn.isEnabled()
    assert page._app_upd_check_btn.text() == tr("正在检查更新…")
    assert isinstance(page._app_upd_check_spinner, Spinner)
    assert page._app_upd_check_spinner.isVisibleTo(page)
    page._app_upd_check_btn.click()
    host._on_app_update_check_requested()
    assert started.count() == 1
    state.release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)
    _assert_check_idle(page)


@pytest.mark.parametrize("outcome", ["latest", "update", "error", "exception", "no_result"])
def test_feedback_restores_the_button_for_every_check_result(qtbot, update_feedback, monkeypatch, outcome):
    host, state = update_feedback
    manager = host._app_update_manager
    if outcome == "update":
        state.result = AppUpdateInfo(has_update=True, latest_version="2.0.0")
    elif outcome == "error":
        state.result = AppUpdateInfo(error="测试网络失败")
    elif outcome == "exception":
        state.result = RuntimeError("测试解析异常")
    elif outcome == "no_result":
        monkeypatch.setattr(manager_module._CheckWorker, "run", lambda self: state.release.wait(3))
    finished = QSignalSpy(manager.check_finished)

    host._settings_page._app_upd_check_btn.click()
    assert not host._settings_page._app_upd_check_btn.isEnabled()
    state.release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)

    assert bool(finished.at(0)[0]["error"]) == (outcome in {"error", "exception", "no_result"})
    assert not manager._is_busy
    _assert_check_idle(host._settings_page)


@pytest.mark.parametrize("failure_at", ["checker", "thread_start"])
def test_feedback_restores_the_button_when_starting_fails(update_feedback, monkeypatch, failure_at):
    host, _state = update_feedback
    manager = host._app_update_manager

    def fail(*args, **kwargs):
        """模拟创建检查器或启动线程时的同步异常。"""
        raise RuntimeError("测试启动异常")

    if failure_at == "checker":
        monkeypatch.setattr(manager_module, "AppUpdateChecker", fail)
    else:
        monkeypatch.setattr(manager_module._CheckWorker, "start", fail)
    finished = QSignalSpy(manager.check_finished)
    host._settings_page._app_upd_check_btn.click()

    assert finished.count() == 1
    assert finished.at(0)[0]["error"]
    assert not manager._is_busy
    _assert_check_idle(host._settings_page)


def test_feedback_ignoring_an_existing_download_does_not_enter_checking(update_feedback):
    host, _state = update_feedback
    manager = host._app_update_manager
    manager._is_busy = True
    started = QSignalSpy(manager.check_started)
    host._settings_page._app_upd_check_btn.click()
    assert started.count() == 0
    _assert_check_idle(host._settings_page)
    manager._is_busy = False


@pytest.mark.parametrize("on_startup", [False, True])
def test_feedback_startup_checks_do_not_leave_the_button_busy(qtbot, update_feedback, on_startup):
    host, state = update_feedback
    manager = host._app_update_manager
    Config().set("app_update.on_startup", on_startup)
    finished = QSignalSpy(manager.check_finished)
    manager.check_on_startup()
    if on_startup:
        assert not host._settings_page._app_upd_check_btn.isEnabled()
        state.release.set()
        qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)
    else:
        assert finished.count() == 0
    _assert_check_idle(host._settings_page)


def test_feedback_rebuilt_settings_page_inherits_only_the_active_check(qtbot, update_feedback):
    host, state = update_feedback
    manager = host._app_update_manager
    finished = QSignalSpy(manager.check_finished)
    host._settings_page._app_upd_check_btn.click()
    old = host._settings_page

    host._rebuild_settings_page_full()

    page = host._settings_page
    assert page is not old
    assert not page._app_upd_check_btn.isEnabled()
    assert not page._app_upd_check_spinner.isHidden()
    qtbot.wait(1)
    state.release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=1000)
    _assert_check_idle(page)
    host._rebuild_settings_page_full()
    _assert_check_idle(host._settings_page)


def test_feedback_spinner_stops_when_hidden_and_when_finished(qtbot, update_feedback, monkeypatch):
    host, state = update_feedback
    monkeypatch.setattr(anim, "is_enabled", lambda: True)
    host.show()
    host._settings_page._app_upd_check_btn.click()
    spinner = host._settings_page._app_upd_check_spinner
    qtbot.waitUntil(lambda: spinner._anim.state() == QPropertyAnimation.State.Running, timeout=1000)
    initial_angle = spinner.angle
    qtbot.waitUntil(lambda: spinner.angle != initial_angle, timeout=1000)
    host.hide()
    assert spinner._anim.state() == QPropertyAnimation.State.Stopped
    host.show()
    assert spinner._anim.state() == QPropertyAnimation.State.Running
    state.release.set()
    qtbot.waitUntil(lambda: host._settings_page._app_upd_check_btn.isEnabled(), timeout=1000)
    _assert_check_idle(host._settings_page)


def test_feedback_cancelling_the_update_prompt_does_not_restore_busy_state(qtbot, update_feedback, monkeypatch):
    host, state = update_feedback
    manager = host._app_update_manager
    state.result = AppUpdateInfo(has_update=True, latest_version="2.0.0")
    original_exec = UpdatePromptDialog.exec
    dismissed = []

    def dismiss(dialog):
        """真实提示框显示前验证按钮已恢复，再通过拒绝动作模拟用户取消。"""
        _assert_check_idle(host._settings_page)
        QTimer.singleShot(0, dialog.reject)
        result = original_exec(dialog)
        dismissed.append(result)
        return result

    monkeypatch.setattr(UpdatePromptDialog, "exec", dismiss)
    monkeypatch.setattr(manager, "_prompt_update", manager_module.AppUpdateManager._prompt_update.__get__(manager))
    host._settings_page._app_upd_check_btn.click()
    state.release.set()
    qtbot.waitUntil(lambda: bool(dismissed), timeout=1000)
    _assert_check_idle(host._settings_page)
