"""验证用户移除下载记录后，已完成任务及完整在线详情不再被信号闭包持有。"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import threading
import weakref

import httpx
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from src.services.gamebanana.downloader import GbDownloadManager, GbDownloadTask
from src.services.gamebanana.models import GbModProfile


@pytest.mark.parametrize("remove", ["delete_task", "clear_completed", "clear_all"])
def test_removed_completed_download_releases_task_and_profile(qtbot, temp_data_dir, tmp_path, respx_mock, remove):
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    task_refs = []
    profile_refs = []
    try:
        for index in range(4):
            url = f"https://example.invalid/archive-{index}.zip"
            respx_mock.get(url).respond(200, content=b"data")
            profile = GbModProfile.from_api(
                {
                    "_idRow": 201,
                    "_sName": "sample",
                    "_sText": "sample description",
                    "_aFiles": [{"_idRow": 101 + index, "_sFile": f"sample-{index}.zip", "_sDownloadUrl": url}],
                }
            )
            task = manager.request(profile, profile.files[0], game_id="sample")
            assert task is not None
            qtbot.waitUntil(lambda item=task: item.state == "done" and not item.is_running, timeout=3000)
            task_refs.append(weakref.ref(task))
            profile_refs.append(weakref.ref(profile))
            if remove == "delete_task":
                manager.delete_task(task.file.file_id)
            else:
                getattr(manager, remove)()
            del task, profile
            QCoreApplication.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.processEvents()
            gc.collect()
        assert manager.active() == []
        assert (
            sum(reference() is not None for reference in task_refs),
            sum(reference() is not None for reference in profile_refs),
        ) == (0, 0)
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_shutdown_finishes_pending_download_coroutine(qapp, temp_data_dir, tmp_path, respx_mock):
    """关闭专用循环前须取消并收集在途请求，确保文件上下文和最终状态已完成清理。"""
    entered = threading.Event()
    released = threading.Event()

    class PendingBody(httpx.AsyncByteStream):
        """在读取边界保持等待，直到下载任务收到取消请求。"""

        async def __aiter__(self):
            """以 finally 标记数据流退出，不依赖事件循环销毁后的垃圾回收。"""
            entered.set()
            try:
                await asyncio.Event().wait()
                yield b"data"
            finally:
                released.set()

    url = "https://example.invalid/pending.zip"
    respx_mock.get(url).respond(200, stream=PendingBody())
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    profile = GbModProfile.from_api(
        {"_idRow": 201, "_aFiles": [{"_idRow": 101, "_sFile": "pending.zip", "_sDownloadUrl": url}]}
    )
    task = manager.request(profile, profile.files[0], game_id="sample")
    try:
        assert entered.wait(3)
        manager.shutdown()
        assert released.is_set()
        assert task.state == "canceled" and not task.is_running
        assert task._aio_task is None
        assert not manager._worker_thread.is_alive()
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_removal_before_queued_completion_keeps_archive_result(qapp, temp_data_dir, tmp_path, respx_mock):
    """主线程尚未投递结果时删除记录，仍须完成归档登记，再释放任务的临时保活引用。"""
    body = b"data"
    digest = hashlib.md5(body).hexdigest()
    url = "https://example.invalid/queued-result.zip"
    respx_mock.get(url).respond(200, content=body)
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    settled = threading.Event()
    manager._task_settled.connect(lambda _task: settled.set(), Qt.ConnectionType.DirectConnection)
    profile = GbModProfile.from_api(
        {
            "_idRow": 201,
            "_aFiles": [{"_idRow": 101, "_sFile": "queued-result.zip", "_sDownloadUrl": url, "_sMd5Checksum": digest}],
        }
    )
    task = manager.request(profile, profile.files[0], game_id="sample")
    reference = weakref.ref(task)
    path = task.archive_path
    try:
        assert settled.wait(3)
        manager.delete_task(101)
        del task, profile
        gc.collect()
        assert reference() is not None
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        gc.collect()
        assert manager.has_local_md5(digest) == path
        assert reference() is None
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_repeated_shutdown_does_not_interrupt_async_cleanup(qapp, temp_data_dir, tmp_path):
    """首轮等待超时后再次关闭，必须等待同一轮收尾，不能再次停止正在清理的循环。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    started = threading.Event()
    cleaning = threading.Event()
    cleaned = threading.Event()
    release = threading.Event()

    async def pending():
        """取消后的异步清理超过首轮等待期限，再由独立事件允许完成。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            cleaned.set()

    future = asyncio.run_coroutine_threadsafe(pending(), manager._loop)
    releaser = threading.Timer(0.05, release.set)
    try:
        assert started.wait(3)
        manager.shutdown()
        assert cleaning.is_set() and not cleaned.is_set()
        assert manager._worker_thread.is_alive()
        profile = GbModProfile.from_api({"_idRow": 201, "_aFiles": [{"_idRow": 101, "_sFile": "closing.zip"}]})
        assert manager.request(profile, profile.files[0], game_id="sample") is None
        assert manager.active() == [] and manager._consumers == {}
        late = GbDownloadTask(profile, profile.files[0], tmp_path / "closing.zip", game_id="sample")
        manager._schedule(late)
        assert late.state == "canceled" and late._aio_task is None
        releaser.start()
        manager.shutdown()
        assert cleaned.is_set()
        assert future.done() and future.cancelled()
        assert not manager._worker_thread.is_alive()
    finally:
        release.set()
        if releaser.ident is not None:
            releaser.join(1)
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_request_after_shutdown_does_not_register_download(qapp, temp_data_dir, tmp_path):
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    profile = GbModProfile.from_api({"_idRow": 201, "_aFiles": [{"_idRow": 101, "_sFile": "closed.zip"}]})
    try:
        manager.shutdown()
        assert manager.request(profile, profile.files[0], game_id="sample", consumer="sample-owner") is None
        assert manager.active() == []
        assert manager._consumers == {}
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


@pytest.mark.parametrize("method", ["resume", "retry"])
@pytest.mark.parametrize("state", ["paused", "failed", "canceled"])
def test_restart_after_shutdown_preserves_terminal_or_paused_state(qapp, temp_data_dir, tmp_path, method, state):
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    profile = GbModProfile.from_api({"_idRow": 201, "_aFiles": [{"_idRow": 101, "_sFile": "closed.zip"}]})
    task = GbDownloadTask(profile, profile.files[0], tmp_path / "closed.zip", game_id="sample")
    task._set_state(state)
    manager._tasks[101] = task
    try:
        manager.shutdown()
        getattr(manager, method)(101)
        assert task.state == state
        assert task._aio_task is None
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
