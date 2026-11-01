"""验证手动程序更新检查独立于启动策略，启动检查仍遵守偏好与节流。"""

import time

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QWidget

from src.core.config import Config
from src.services.app_update import manager as manager_module
from src.services.app_update.checker import AppUpdateChecker


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
