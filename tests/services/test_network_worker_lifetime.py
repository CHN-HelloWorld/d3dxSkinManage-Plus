"""验证普通下载在终态后释放原生线程对象与其持有的回调资源。"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent

from src.services.network import DownloadWorker, NetworkService


def _flush_deletes() -> None:
    """投递跨线程回调并完成延迟删除，避免把待清理对象误判为长期保留。"""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()


class _Payload:
    """由下载完成回调持有的业务数据，用弱引用观察其释放。"""


@pytest.fixture
def network_service(qapp, temp_data_dir):
    """保留长寿命服务供多次下载复用，退出前停止残余线程并销毁 Qt 对象。"""
    service = NetworkService()
    yield service
    for worker in service.findChildren(DownloadWorker):
        worker.cancel()
        assert worker.wait(3000)
    if shiboken6.isValid(service):
        service.deleteLater()
    _flush_deletes()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_terminal_downloads_release_qt_children_and_callback_payloads(network_service, tmp_path, monkeypatch, outcome):
    """成功、失败和取消均应解除长期服务对已结束任务及其业务闭包的所有权。"""
    from src.services import network

    entered = threading.Event()
    release = threading.Event()
    payload_refs = []

    class Response:
        """将请求停在可控制的边界，取消场景无需访问真实网络。"""

        headers = {"content-length": "4"}

        def raise_for_status(self):
            """响应状态在三个场景中均成功，失败由读取阶段产生。"""

        def iter_bytes(self, chunk_size):
            """在主线程决定取消状态之后继续读取或产生受控异常。"""
            entered.set()
            assert release.wait(3)
            if outcome == "failure":
                raise httpx.ReadError("synthetic read failure")
            yield b"data"

    @contextmanager
    def stream(method, url, **kwargs):
        """替换最外层网络输入，保留真实 DownloadWorker 线程及文件写入路径。"""
        yield Response()

    monkeypatch.setattr(network.httpx, "stream", stream)
    for index in range(4):
        entered.clear()
        release.clear()
        payload = _Payload()
        payload_refs.append(weakref.ref(payload))
        worker = network_service.download_file(
            f"https://example.invalid/archive-{index}.zip",
            tmp_path / f"archive-{index}.zip",
            on_finished=lambda _path, item=payload: item,
        )
        del payload
        try:
            assert entered.wait(3)
            if outcome == "cancelled":
                worker.cancel()
        finally:
            release.set()
            assert worker.wait(3000)
        del worker
        _flush_deletes()

    retained = (
        len(network_service._active_downloads),
        len(network_service.findChildren(DownloadWorker)),
        sum(reference() is not None for reference in payload_refs),
    )
    assert retained == (0, 0, 0), "活动记录、已结束 Qt 线程及回调数据均应释放"


def test_business_completion_keeps_worker_alive_until_native_thread_exits(network_service, tmp_path, monkeypatch):
    """业务结果先于 run 返回时，不提前销毁仍在执行的原生线程。"""
    from src.services import network

    entered = threading.Event()
    release = threading.Event()
    received = []

    class Worker(DownloadWorker):
        """在业务信号发出后暂停，明确区分业务完成与原生线程结束。"""

        def run(self):
            """完成通知后继续占用线程，直到测试允许退出。"""
            self.finished.emit(self._save_path)
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(network, "DownloadWorker", Worker)
    url = "https://example.invalid/result.zip"
    worker = network_service.download_file(url, tmp_path / "result.zip", on_finished=received.append)
    try:
        assert entered.wait(3)
        _flush_deletes()
        assert received == [str(tmp_path / "result.zip")]
        assert shiboken6.isValid(worker) and worker.isRunning()
        assert network_service._active_downloads.get(url) is worker
    finally:
        release.set()
        assert worker.wait(3000)
        _flush_deletes()


def test_finishing_old_download_keeps_new_task_for_same_url(network_service, tmp_path, monkeypatch):
    """同地址重试已经开始时，旧线程退出不能移除新任务的活动登记。"""
    from src.services import network

    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    class Worker(DownloadWorker):
        """按目标文件区分同时运行的同 URL 下载。"""

        def run(self):
            """两个请求分别在各自的事件边界等待完成。"""
            index = int(Path(self._save_path).stem)
            entered[index].set()
            assert release[index].wait(3)
            self.finished.emit(self._save_path)

    monkeypatch.setattr(network, "DownloadWorker", Worker)
    url = "https://example.invalid/retry.zip"
    workers = []
    try:
        for index in range(2):
            workers.append(network_service.download_file(url, tmp_path / f"{index}.zip"))
            assert entered[index].wait(3)
        release[0].set()
        assert workers[0].wait(3000)
        _flush_deletes()
        assert network_service._active_downloads.get(url) is workers[1]
    finally:
        for gate in release:
            gate.set()
        for worker in workers:
            if shiboken6.isValid(worker):
                worker.cancel()
                assert worker.wait(3000)
        _flush_deletes()


def test_service_destruction_cancels_and_releases_running_download(tmp_path):
    """在子进程检验销毁服务的原生安全，防止回归造成整个 Qt 测试进程退出。"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    probe = Path(__file__).with_name("_network_worker_lifetime_probe.py")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(probe), str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
