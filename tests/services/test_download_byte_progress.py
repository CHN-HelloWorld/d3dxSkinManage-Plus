from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Slot
from PySide6.QtTest import QSignalSpy

from src.services.gamebanana.downloader import GbDownloadTask
from src.services.network import DownloadWorker


@pytest.mark.parametrize(
    "downloaded,total",
    [
        (65536, 3 * 1024**3),
        (2**31 + 65536, 5 * 1024**3),
    ],
)
def test_gamebanana_queued_progress_retains_large_counts(qtbot, downloaded, total):
    task = GbDownloadTask(None, SimpleNamespace(), Path("unused"), game_id="test")
    received = []

    class ProgressView(QObject):
        @Slot("qint64", "qint64")
        def update(self, done, size):
            received.append((done, size, QThread.currentThread() == self.thread()))

    view = ProgressView()
    task.progress.connect(view.update, Qt.ConnectionType.QueuedConnection)
    thread = Thread(target=task._emit_progress, args=(downloaded, total))
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    qtbot.waitUntil(lambda: bool(received))
    assert received == [(downloaded, total, True)]
    assert task.last_progress == (downloaded, total)


def test_network_worker_reports_large_content_length_without_progress_errors(qtbot, tmp_path, monkeypatch):
    from src.services import network

    total = 5 * 1024**3
    chunks = [b"first", b"second"]

    class Response:
        headers = {"content-length": str(total)}

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            assert chunk_size == 8192
            yield from chunks

    @contextmanager
    def response(method, url, **kwargs):
        assert method == "GET" and url == "https://example.invalid/large.zip"
        yield Response()

    monkeypatch.setattr(network.httpx, "stream", response)
    target = tmp_path / "download.zip"
    worker = DownloadWorker("https://example.invalid/large.zip", str(target))
    progress, completed, failed = QSignalSpy(worker.progress), QSignalSpy(worker.finished), QSignalSpy(worker.error)
    worker.start()
    try:
        qtbot.waitUntil(lambda: not worker.isRunning())
        assert worker.wait(2000)
        assert failed.count() == 0
        assert [progress.at(i) for i in range(progress.count())] == [[5, total], [11, total]]
        assert completed.count() == 1 and completed.at(0) == [str(target)]
        assert target.read_bytes() == b"firstsecond"
    finally:
        worker.cancel()
        worker.wait(2000)
        worker.deleteLater()
