import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QWidget

from scripts.performance.perf_mod_library import close_gui_window


def test_benchmark_close_destroys_window_before_returning(qapp):
    window = QWidget()
    child = QWidget(window)
    window.show()
    close_gui_window(window, qapp)
    assert not shiboken6.isValid(window)
    assert not shiboken6.isValid(child)


@pytest.mark.parametrize("detached", [False, True])
def test_benchmark_close_keeps_window_alive_until_workers_stop(qapp, monkeypatch, detached):
    window = QWidget()
    worker = QThread(None if detached else window)
    if detached:
        window._overview_page = SimpleNamespace(_pixmap_worker=worker)
    running = [True]
    monkeypatch.setattr(worker, "isRunning", lambda: running[0])
    QTimer.singleShot(30, lambda: running.__setitem__(0, False))
    window.show()
    close_gui_window(window, qapp)
    assert not running[0]
    assert not shiboken6.isValid(window)


def test_benchmark_close_waits_for_deferred_close_before_destruction(qapp):
    class DeferredWindow(QWidget):
        ready = False

        def closeEvent(self, event):
            if not self.ready:
                event.ignore()
                QTimer.singleShot(20, self.finish)
                return
            super().closeEvent(event)

        def finish(self):
            self.ready = True
            self.close()

    window = DeferredWindow()
    window.show()
    close_gui_window(window, qapp)
    assert window.ready
    assert not shiboken6.isValid(window)


def test_scenarios_destroy_already_closed_window_before_releasing_fixture(qapp, tmp_path, monkeypatch):
    scripts = Path(__file__).parents[2] / "scripts" / "performance"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("scenario_teardown_test", scripts / "perf_mod_library_scenarios.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = module.ScenarioRunner(None, tmp_path, {}, lambda: None, offscreen_smoke=True)
    runner.window = QWidget()
    child = QWidget(runner.window)
    runner.window._library_probe = SimpleNamespace(worker=None, applying=False)
    runner.window.show()
    runner.window.close()
    runner.closed = True
    try:
        runner.close()
        assert not shiboken6.isValid(runner.window)
        assert not shiboken6.isValid(child)
    finally:
        if shiboken6.isValid(runner.window):
            shiboken6.delete(runner.window)


def test_benchmark_close_stops_its_download_loop_before_fixture_cleanup(qapp, tmp_path):
    from src.services.gamebanana.downloader import GbDownloadManager

    window = QWidget()
    downloader = GbDownloadManager(base_dir=tmp_path / "downloads")
    window._gb_store_page = SimpleNamespace(_download_manager=downloader)
    assert downloader._worker_thread.is_alive()
    try:
        close_gui_window(window, qapp)
        assert not downloader._worker_thread.is_alive()
    finally:
        downloader.shutdown()
