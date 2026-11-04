"""验证用户移除下载记录后，已完成任务及完整在线详情不再被信号闭包持有。"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import threading
import weakref
from pathlib import Path

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


def _complete_synthetic_download(
    manager: GbDownloadManager,
    directory,
    file_id: int,
    *,
    consumer: str | None = None,
):
    """建立带完整详情的已完成任务，并按真实完成入口写入下载历史。"""
    body = f"archive-{file_id}".encode()
    digest = hashlib.md5(body).hexdigest()
    profile = GbModProfile.from_api(
        {
            "_idRow": 2000 + file_id,
            "_sName": f"sample-{file_id}",
            "_sText": "large synthetic profile payload " * 20,
            "_aFiles": [
                {
                    "_idRow": file_id,
                    "_sFile": f"sample-{file_id}.zip",
                    "_sDownloadUrl": f"https://example.invalid/{file_id}.zip",
                    "_sMd5Checksum": digest,
                }
            ],
        }
    )
    archive = directory / f"{digest}__{file_id}__sample-{file_id}.zip"
    archive.write_bytes(body)
    task = GbDownloadTask(profile, profile.files[0], archive, game_id="sample")
    task._set_state("done")
    owner = consumer or f"download:{file_id}"
    manager.retain(file_id, digest, owner=owner, path=archive)
    manager._tasks[file_id] = task
    manager._on_task_done(task, archive)
    return weakref.ref(task), weakref.ref(profile), archive


def test_completed_download_history_releases_profiles_beyond_persisted_limit(
    qapp,
    temp_data_dir,
    tmp_path,
):
    """超过落盘历史上限的完成任务须释放完整详情，但不得删除已下载归档。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    removed = []
    manager.task_removed.connect(removed.append)
    references = []
    try:
        for file_id in range(100, 152):
            references.append(_complete_synthetic_download(manager, tmp_path / "downloads", file_id))

        gc.collect()

        assert [task.file.file_id for task in manager.active()] == list(range(102, 152))
        assert removed == [100, 101]
        assert all(reference() is None for pair in references[:2] for reference in pair[:2])
        assert all(path.is_file() for _task, _profile, path in references)
        assert "download:100" not in manager._consumers
        assert "download:101" not in manager._consumers
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


@pytest.mark.parametrize("count", [2, 52])
def test_completed_history_stays_bounded_when_queue_write_fails(
    qapp,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    count,
):
    """队列写盘失败时仍按本会话完成顺序保留最近五十条，且不删除归档。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")

    def fail_write_text(_path, _data, *args, **kwargs):
        """模拟 queue.json 写入失败，保留真实历史组装和异常处理路径。"""
        raise OSError("synthetic queue write failure")

    monkeypatch.setattr(Path, "write_text", fail_write_text)
    references = []
    try:
        for file_id in range(100, 100 + count):
            references.append(_complete_synthetic_download(manager, tmp_path / "downloads", file_id))

        retained_start = max(100, 100 + count - 50)
        assert [task.file.file_id for task in manager.active()] == list(range(retained_start, 100 + count))
        assert all(path.is_file() for _task, _profile, path in references)
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_restored_history_keeps_session_order_when_queue_write_fails(qapp, temp_data_dir, tmp_path, monkeypatch):
    """恢复的最新优先磁盘记录须转换为旧到新顺序，再接续本会话完成记录。"""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    items = []
    for file_id in reversed(range(100, 150)):
        archive = downloads / f"nomd5__{file_id}__restored-{file_id}.zip"
        archive.write_bytes(f"restored-{file_id}".encode())
        items.append(
            {
                "mod_id": 2000 + file_id,
                "file_id": file_id,
                "md5": "",
                "name": f"restored-{file_id}.zip",
                "path": str(archive),
                "download_url": f"https://example.invalid/restored-{file_id}.zip",
                "game_id": "sample",
                "ts": f"2026-01-01T00:00:{file_id - 100:02d}",
            }
        )
    (downloads / "queue.json").write_text(
        json.dumps({"schema_version": 1, "items": items}),
        encoding="utf-8",
    )
    manager = GbDownloadManager(base_dir=downloads)

    def fail_write_text(_path, _data, *args, **kwargs):
        """恢复完成后仅阻断后续 queue.json 写回。"""
        raise OSError("synthetic queue write failure")

    monkeypatch.setattr(Path, "write_text", fail_write_text)
    new_archives = []
    try:
        for file_id in (200, 201):
            _task, _profile, archive = _complete_synthetic_download(manager, downloads, file_id)
            new_archives.append(archive)

        assert list(manager._history_file_ids) == [*range(102, 150), 200, 201]
        assert {task.file.file_id for task in manager.active()} == {*range(102, 150), 200, 201}
        assert all(path.is_file() for path in downloads.glob("*__*.zip"))
        assert all(path.is_file() for path in new_archives)
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_explicit_consumer_postpones_completed_history_retirement(qapp, temp_data_dir, tmp_path):
    """共享消费者仍使用旧完成任务时延后淘汰，释放后再回收详情并保留归档。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    removed = []
    manager.task_removed.connect(removed.append)
    try:
        task_ref, profile_ref, archive = _complete_synthetic_download(
            manager,
            tmp_path / "downloads",
            100,
            consumer="update:sample",
        )
        for file_id in range(101, 151):
            _complete_synthetic_download(manager, tmp_path / "downloads", file_id)

        gc.collect()
        assert task_ref() is manager.get_task(100)
        assert profile_ref() is not None
        assert removed == []

        manager.release("update:sample")
        gc.collect()

        assert manager.get_task(100) is None
        assert task_ref() is None
        assert profile_ref() is None
        assert removed == [100]
        assert archive.is_file()
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_new_done_task_survives_release_until_history_registration(
    qapp,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    """完成状态与历史登记之间的其他释放操作不得淘汰当前新任务。"""
    from src.services.gamebanana import downloader as downloader_module

    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    monkeypatch.setattr(manager, "_schedule", lambda _task: None)
    monkeypatch.setattr(downloader_module, "_DOWNLOAD_HISTORY_LIMIT", 1)
    try:
        old_task_ref, old_profile_ref, old_archive = _complete_synthetic_download(
            manager,
            tmp_path / "downloads",
            100,
        )
        profile = GbModProfile.from_api(
            {
                "_idRow": 202,
                "_sName": "new completion",
                "_aFiles": [{"_idRow": 101, "_sFile": "new.zip"}],
            }
        )
        task = manager.request(profile, profile.files[0], game_id="sample")
        assert task is not None
        task.archive_path.write_bytes(b"new archive")

        task._set_state("done")
        manager.release("unrelated-owner")

        assert manager.get_task(101) is task
        manager._on_task_done(task, task.archive_path)
        gc.collect()

        assert manager.get_task(101) is task
        assert [item["file_id"] for item in manager._read_queue_items()] == [101]
        assert old_task_ref() is None
        assert old_profile_ref() is None
        assert old_archive.is_file()
        assert task.archive_path.is_file()
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_clear_completed_is_idempotent_when_task_removed_consumer_reenters(
    qapp,
    temp_data_dir,
    tmp_path,
):
    """任务移除通知重入批量清理时，每条记录只退场并通知一次。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    try:
        for file_id in (100, 101):
            _complete_synthetic_download(manager, tmp_path / "downloads", file_id)
        removed = []
        reentered = False

        def on_removed(file_id: int) -> None:
            """记录同步通知，并在首条通知中重入同一清理入口。"""
            nonlocal reentered
            removed.append(file_id)
            if not reentered:
                reentered = True
                manager.clear_completed()

        manager.task_removed.connect(on_removed)

        manager.clear_completed()

        assert manager.active() == []
        assert sorted(removed) == [100, 101]
        assert len(removed) == 2
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_clear_completed_preserves_replacement_for_snapshot_file_id(
    qapp,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    """移除通知为后续文件编号注册新任务时，外层快照不得删除新实例。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    monkeypatch.setattr(manager, "_schedule", lambda _task: None)
    replacement = None
    removed = []
    try:
        for file_id in (100, 101):
            _complete_synthetic_download(manager, tmp_path / "downloads", file_id)

        def on_removed(file_id: int) -> None:
            """首条同步通知中替换后续快照项，复现消费者重入注册。"""
            nonlocal replacement
            removed.append(file_id)
            if file_id != 100:
                return
            manager.delete_task(101)
            profile = GbModProfile.from_api({"_idRow": 301, "_aFiles": [{"_idRow": 101, "_sFile": "replacement.zip"}]})
            replacement = manager.request(profile, profile.files[0], game_id="sample")

        manager.task_removed.connect(on_removed)

        manager.clear_completed()

        assert replacement is not None
        assert manager.get_task(101) is replacement
        assert replacement.state == "queued"
        assert removed == [100, 101]
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


@pytest.mark.parametrize("state", ["failed", "canceled"])
def test_retryable_terminal_history_is_bounded_and_keeps_latest_retryable(
    qapp,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    state,
):
    """失败或取消任务只保留最近五十条完整详情，最近任务仍可重新调度。"""
    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    scheduled = []
    monkeypatch.setattr(manager, "_schedule", lambda task: scheduled.append(task.file.file_id))
    references = []
    try:
        for file_id in range(100, 152):
            profile = GbModProfile.from_api(
                {
                    "_idRow": 2000 + file_id,
                    "_sName": f"retryable-{file_id}",
                    "_sText": "retryable synthetic profile payload " * 20,
                    "_aFiles": [
                        {
                            "_idRow": file_id,
                            "_sFile": f"retryable-{file_id}.zip",
                            "_sDownloadUrl": f"https://example.invalid/retryable-{file_id}.zip",
                        }
                    ],
                }
            )
            task = manager.request(profile, profile.files[0], game_id="sample")
            assert task is not None
            references.append((weakref.ref(task), weakref.ref(profile)))
            if state == "failed":
                task._set_state("failed")
            else:
                task.cancel()

        del task, profile
        gc.collect()

        assert [task.file.file_id for task in manager.active()] == list(range(102, 152))
        assert all(reference() is None for pair in references[:2] for reference in pair)
        latest = manager.get_task(151)
        assert latest is not None and latest.state == state

        scheduled.clear()
        manager.retry(151)

        assert latest.state == "queued"
        assert scheduled == [151]
    finally:
        manager.shutdown()
        manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def test_explicit_consumer_postpones_retryable_history_retirement(
    qapp,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    """显式消费者继续使用旧失败任务时，释放前不得淘汰其详情。"""
    from src.services.gamebanana import downloader as downloader_module

    manager = GbDownloadManager(base_dir=tmp_path / "downloads")
    monkeypatch.setattr(manager, "_schedule", lambda _task: None)
    monkeypatch.setattr(downloader_module, "_RETRYABLE_HISTORY_LIMIT", 1)
    removed = []
    manager.task_removed.connect(removed.append)
    try:
        old_profile = GbModProfile.from_api({"_idRow": 201, "_aFiles": [{"_idRow": 101, "_sFile": "old.zip"}]})
        old_task = manager.request(
            old_profile,
            old_profile.files[0],
            game_id="sample",
            consumer="update:sample",
        )
        assert old_task is not None
        old_task._set_state("failed")
        task_ref, profile_ref = weakref.ref(old_task), weakref.ref(old_profile)

        new_profile = GbModProfile.from_api({"_idRow": 202, "_aFiles": [{"_idRow": 102, "_sFile": "new.zip"}]})
        new_task = manager.request(new_profile, new_profile.files[0], game_id="sample")
        assert new_task is not None
        new_task._set_state("failed")
        del old_task, old_profile
        gc.collect()

        assert task_ref() is manager.get_task(101)
        assert profile_ref() is not None
        assert removed == []

        manager.release("update:sample")
        gc.collect()

        assert task_ref() is None
        assert profile_ref() is None
        assert removed == [101]
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
