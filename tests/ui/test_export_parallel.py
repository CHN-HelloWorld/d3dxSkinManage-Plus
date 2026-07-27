from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import Qt

from src.core.mod_exporter import ExportItem, ExportResult
from src.ui.dialogs.export_progress import ExportProgressDialog, ExportWorkerThread


def _worker(tmp_path, count=5, workers=2):
    items = []
    for index in range(count):
        source = tmp_path / f"source{index}.zip"
        source.write_bytes(b"source")
        items.append(ExportItem(str(index), str(source)))
    return ExportWorkerThread(
        items,
        str(tmp_path / "out"),
        "zip",
        False,
        str(tmp_path),
        max_workers=workers,
    )


def _connect(signal, slot):
    signal.connect(slot, Qt.ConnectionType.DirectConnection)


@pytest.mark.parametrize("workers", [1, 2, 99])
def test_export_overlaps_up_to_the_requested_limit(qtbot, tmp_path, workers):
    worker = _worker(tmp_path, workers=workers)
    expected = min(workers, 5)
    ready = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = peak = 0
    started, done = [], []

    class Exporter:
        def export_one(self, item, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                started.append(item.name)
                if active == expected:
                    ready.set()
            assert release.wait(5)
            with lock:
                active -= 1
            return ExportResult(True, item.name)

    worker._exporter = Exporter()
    _connect(worker.item_done, lambda index, result: done.append(index))
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        assert ready.wait(3), "The configured number of exports must overlap"
        assert len(started) == expected
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert peak == expected
    assert sorted(done) == [0, 1, 2, 3, 4]


def test_parallel_progress_uses_all_items_and_never_moves_backwards(qtbot, tmp_path):
    worker = _worker(tmp_path, count=2)
    progress = []
    _connect(worker.stage_progress, lambda fraction, text: progress.append(fraction))
    worker._emit_stage(1, 2, 0.8, "second item")
    worker._emit_stage(0, 2, 0.6, "first item")
    worker._emit_stage(0, 2, 0.2, "an earlier phase callback")
    assert progress == pytest.approx([0.4, 0.7, 0.7])


def test_finished_waits_for_running_io_and_discards_all_unconfirmed_products(qtbot, tmp_path):
    worker = _worker(tmp_path, count=3)
    ready = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    returned = threading.Event()
    finished = threading.Event()
    acknowledged = threading.Event()
    done, started = [], []
    output = tmp_path / "out"
    output.mkdir()

    class Exporter:
        def export_one(self, item, *args, **kwargs):
            index = int(item.name)
            started.append(index)
            ready[index].set()
            assert release[index].wait(5)
            path = output / f"{item.name}.zip"
            path.write_bytes(b"exported")
            if index == 0:
                returned.set()
            return ExportResult(True, item.name, out_path=str(path))

    worker._exporter = Exporter()
    _connect(worker.item_done, lambda index, result: done.append(index))
    _connect(worker.item_done, lambda index, result: acknowledged.set())
    _connect(worker.finished_all, finished.set)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        assert all(event.wait(3) for event in ready)
        worker.pause()
        release[0].set()
        assert returned.wait(3)
        assert not acknowledged.wait(0.1), "Pause must hold completed products at the commit gate"
        worker.terminate_export()
        assert not finished.is_set(), "An active IO operation still owns an output"
        release[1].set()
        assert finished.wait(3)
    finally:
        worker.terminate_export()
        for event in release:
            event.set()
        thread.join(5)
    assert not thread.is_alive()
    assert sorted(started) == [0, 1], "Pending exports must not start after stop"
    assert done == []
    assert list(output.iterdir()) == []


def test_parallel_pause_holds_each_commit_until_resume(qtbot, tmp_path):
    worker = _worker(tmp_path, count=2)
    ready = threading.Barrier(3)
    release = threading.Event()
    finished = threading.Event()
    done = []

    class Exporter:
        def export_one(self, item, *args, **kwargs):
            ready.wait(timeout=3)
            assert release.wait(5)
            return ExportResult(True, item.name)

    worker._exporter = Exporter()
    _connect(worker.item_done, lambda index, result: done.append(index))
    _connect(worker.finished_all, finished.set)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        ready.wait(timeout=3)
        worker.pause()
        release.set()
        assert not finished.wait(0.1), "All completed items must remain paused before acknowledgement"
        assert done == []
        assert not finished.is_set()
        worker.resume()
        assert finished.wait(3)
    finally:
        worker.terminate_export()
        release.set()
        thread.join(5)
    assert sorted(done) == [0, 1]


def test_dialog_does_not_replace_stage_progress_with_item_index(qtbot, tmp_path, temp_data_dir):
    class Manager:
        def _get_library_root(self):
            return tmp_path

    class Panel:
        def __init__(self):
            self.progress = []

        def update_item(self, *args):
            pass

        def update_progress(self, percent, *args):
            self.progress.append(percent)

    dialog = ExportProgressDialog(
        [],
        str(tmp_path / "out"),
        Manager(),
        export_items=[ExportItem(str(i), str(tmp_path / str(i))) for i in range(4)],
    )
    qtbot.addWidget(dialog)
    panel = Panel()
    dialog._panel = panel
    dialog._on_stage_progress(0.65, "working")
    dialog._on_progress(3, 4, "last item starts")
    dialog._on_item_done(1, ExportResult(True, "1"))
    dialog._on_stage_progress(0.4, "late callback")
    assert panel.progress == [65.0, 65.0, 65.0, 65.0]


def test_later_items_complete_while_first_is_busy_and_errors_are_isolated(qtbot, tmp_path):
    worker = _worker(tmp_path, count=4)
    first_started = threading.Event()
    release_first = threading.Event()
    later_finished = threading.Event()
    done, progress, started = [], [], []
    missing_source = tmp_path / "source3.zip"
    missing_source.unlink()

    class Exporter:
        def export_one(self, item, *args, **kwargs):
            if item.name == "0":
                first_started.set()
                assert release_first.wait(5)
                kwargs["stage_cb"](0.6, "first item")
            elif item.name == "1":
                assert first_started.wait(3)
                raise OSError("cannot read source")
            return ExportResult(True, item.name)

    def record_done(index, result):
        done.append((index, result))
        if index == 3:
            later_finished.set()

    worker._exporter = Exporter()
    _connect(worker.item_done, record_done)
    _connect(worker.stage_progress, lambda fraction, text: progress.append(fraction))
    _connect(worker.progress_updated, lambda index, total, text: started.append(index))
    worker.start()
    try:
        assert later_finished.wait(3), "An unfinished first item must not hold later results"
        assert [index for index, result in done] == [1, 2, 3]
        assert done[0][1].error == "cannot read source"
        assert done[1][1].ok
        assert done[2][1].skipped
    finally:
        release_first.set()
        assert worker.wait(5000)
    assert [index for index, result in done] == [1, 2, 3, 0]
    assert sorted(started) == [0, 1, 2, 3, 4]
    assert progress == sorted(progress)
    assert progress[-1] == 1.0
