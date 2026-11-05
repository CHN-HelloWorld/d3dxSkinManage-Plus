"""验证真实更新线程的中断、会话事件隔离和界面宿主先销毁的安全性。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

import httpx
import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLabel, QWidget

from src.services.app_update import manager as manager_module
from src.services.app_update.checker import AppUpdateInfo


def flush_deletes():
    """投递跨线程结果并处理延迟销毁，区分正常退出和对象滞留。"""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


@pytest.fixture
def manager(qapp, temp_data_dir, monkeypatch):
    """以隔离配置运行真实管理器，只替换最终安装和模态错误提示。"""
    host = QWidget()
    instance = manager_module.AppUpdateManager(host)
    instance._config.set("app_update.download_source", "official")
    instance._config.set("gamebanana.proxy_config.active", False)
    monkeypatch.setattr(manager_module, "is_frozen", lambda: True)
    errors, installed = [], []
    monkeypatch.setattr(instance, "_error_box", lambda title, message: errors.append(message))
    monkeypatch.setattr(instance, "_install", lambda path: installed.append(path))
    yield instance, errors, installed
    worker = instance._download_worker
    if worker is not None and shiboken6.isValid(worker):
        worker.cancel()
        assert worker.wait(3000)
    if shiboken6.isValid(host):
        host.deleteLater()
    flush_deletes()


def update_info(payload):
    """为同一版本固定公开地址和摘要，测试安装器不会接收未验证结果。"""
    return AppUpdateInfo(
        has_update=True,
        latest_version="2.0",
        tag_name="v2.0",
        download_url="https://origin.test/v2/app.zip",
        download_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_manager_installs_only_verified_download(manager, qtbot, respx_mock):
    instance, errors, installed = manager
    payload = b"complete synthetic update"
    info = update_info(payload)
    respx_mock.get(info.download_url).respond(200, content=payload)
    instance._start_download(info)
    qtbot.waitUntil(lambda: bool(installed))
    qtbot.waitUntil(lambda: instance._download_worker is None)
    assert installed[0].read_bytes() == payload
    assert errors == []
    assert instance._progress is None


def test_cancel_waits_for_cleanup_before_allowing_new_download(manager, qtbot, respx_mock):
    instance, errors, installed = manager
    payload = b"first half and second half"
    info = update_info(payload)
    waiting, closing, release = threading.Event(), threading.Event(), threading.Event()

    class Body(httpx.AsyncByteStream):
        """关闭阶段暂时等待，验证界面不能在文件所有者退出前重新启动。"""

        async def __aiter__(self):
            yield payload[:5]
            waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            while not release.is_set():
                await asyncio.sleep(0.005)

    route = respx_mock.get(info.download_url).respond(200, stream=Body())
    instance._start_download(info)
    worker = instance._download_worker
    part = worker._session.cache.part_path
    try:
        qtbot.waitUntil(waiting.is_set)
        instance._on_download_cancel()
        qtbot.waitUntil(closing.is_set)
        assert instance._is_busy
        instance._start_download(info)
        assert instance._download_worker is worker
    finally:
        release.set()
        worker.cancel()
        assert worker.wait(3000)
    qtbot.waitUntil(lambda: instance._download_worker is None)
    assert not instance._is_busy and not installed
    assert not part.exists()
    route.respond(200, content=payload)
    instance._start_download(info)
    qtbot.waitUntil(lambda: bool(installed))
    assert "Range" not in route.calls[-1].request.headers
    assert installed[0].read_bytes() == payload
    assert errors == []


def test_stale_events_cannot_finish_or_change_new_session(manager, qtbot, respx_mock):
    instance, errors, installed = manager
    payload = b"complete synthetic update"
    info = update_info(payload)
    waiting = threading.Event()

    async def respond(request):
        """在响应头前停住当前会话，留出投递旧会话事件的窗口。"""
        waiting.set()
        await asyncio.Event().wait()

    respx_mock.get(info.download_url).mock(side_effect=respond)
    instance._start_download(info)
    qtbot.waitUntil(waiting.is_set)
    old_id = instance._download_id - 1
    progress = instance._progress
    before = progress._bar.value()
    instance._on_download_progress(old_id, 999, 1000)
    instance._on_download_finished(old_id, "old.zip")
    instance._on_download_error(old_id, "old failure")
    assert progress._bar.value() == before
    assert instance._progress is progress
    assert not errors and not installed


def test_destroying_host_cancels_orphaned_download_thread(manager, qtbot, respx_mock):
    instance, _errors, _installed = manager
    waiting = threading.Event()
    info = update_info(b"complete update")

    async def respond(request):
        """等待响应头期间销毁宿主，取消应中断工作线程的 await。"""
        waiting.set()
        await asyncio.Event().wait()

    respx_mock.get(info.download_url).mock(side_effect=respond)
    instance._start_download(info)
    worker = instance._download_worker
    qtbot.waitUntil(waiting.is_set)
    try:
        instance._win.deleteLater()
        flush_deletes()
        assert worker.wait(2000)
        flush_deletes()
        assert not shiboken6.isValid(instance)
        assert not shiboken6.isValid(worker)
        assert worker not in manager_module._active_workers
    finally:
        if shiboken6.isValid(worker):
            worker.cancel()
            assert worker.wait(3000)


def test_missing_digest_rejects_update_before_network(manager, respx_mock):
    instance, errors, installed = manager
    info = update_info(b"complete update")
    info.sha256 = ""
    instance._start_download(info)
    assert errors and not installed
    assert instance._download_worker is None
    assert not instance._is_busy
    assert respx_mock.calls.call_count == 0


def test_current_source_label_survives_progress_updates(manager, qtbot, respx_mock):
    instance, _errors, _installed = manager
    waiting = threading.Event()
    info = update_info(b"complete update")

    async def respond(request):
        """保持窗口可见，检查来源与字节进度各自显示。"""
        waiting.set()
        await asyncio.Event().wait()

    respx_mock.get(info.download_url).mock(side_effect=respond)
    instance._start_download(info)
    qtbot.waitUntil(waiting.is_set)
    qtbot.waitUntil(lambda: bool(instance._progress.findChild(QLabel, "downloadSource").text()))
    source = instance._progress.findChild(QLabel, "downloadSource")
    before = source.text()
    instance._on_download_progress(instance._download_id, 5, info.download_size)
    assert source.text() == before and "GitHub" in before


def test_cancel_before_worker_start_emits_no_result(qapp, tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadRoute
    from src.services.app_update.download_state import UpdateArtifact
    from src.services.app_update.download_worker import AppUpdateDownloadWorker

    artifact = UpdateArtifact("https://origin.test/a.zip", 5, hashlib.sha256(b"bytes").hexdigest())
    worker = AppUpdateDownloadWorker(7, artifact, tmp_path, [DownloadRoute("source", artifact.url)], automatic=False)
    received = []
    worker.succeeded.connect(lambda *args: received.append(args))
    worker.failed.connect(lambda *args: received.append(args))
    try:
        worker.cancel()
        worker.start()
        assert worker.wait(2000)
        flush_deletes()
        assert not received and respx_mock.calls.call_count == 0
        assert not list(Path(tmp_path).rglob("*.part"))
    finally:
        worker.cancel()
        assert worker.wait(2000)
        worker.deleteLater()
        flush_deletes()
