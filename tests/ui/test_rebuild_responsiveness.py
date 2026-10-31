import threading

from PySide6.QtCore import QTimer

from src.core.rebuild_session import RebuildSession
from src.ui.dialogs.rebuild_progress import RebuildProgressDialog
from tests.core.test_rebuild_index import _make_deployable_mod
from tests.core.test_rebuild_index import mm as rebuild_mm

mm = rebuild_mm


def test_cancel_waits_for_worker_before_releasing_cache(mm, qtbot, monkeypatch):
    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    ready, release = threading.Event(), threading.Event()
    prepare = RebuildSession.prepare

    def blocked(session, progress):
        prepare(session, progress)
        ready.set()
        release.wait(5)
        session.check_cancel()

    monkeypatch.setattr(RebuildSession, "prepare", blocked)
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog._start()
    try:
        qtbot.waitUntil(ready.is_set)
        assert mm._rebuild_inflight_ids
        dialog.reject()
        assert not dialog._finished
        assert dialog._worker.isRunning()
        assert mm._rebuild_inflight_ids
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._finished)
    assert not dialog._worker.isRunning()
    assert not mm._rebuild_inflight_ids
    assert not mm.get_mods()
    assert "取消" in dialog.error


def test_staging_yields_without_live_transaction(mm, qtbot, monkeypatch):
    for i in range(4):
        _make_deployable_mod(mm._get_library_root("game_a"), str(i))
    original = RebuildSession.stage_batch
    observations = []

    def one(session, size=32):
        result = original(session, 1)
        observations.append((session.position, mm._index._db().in_transaction, len(mm.get_mods())))
        return result

    monkeypatch.setattr(RebuildSession, "stage_batch", one)
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog._start()
    qtbot.waitUntil(lambda: dialog._finished)
    assert not dialog.error
    assert observations == [(i, False, 0) for i in range(1, 5)]
    assert dialog.report.registered == 4


def test_cancel_before_queued_start_never_creates_worker(mm, qtbot):
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.reject()
    delivered = []
    QTimer.singleShot(0, lambda: delivered.append(True))
    qtbot.waitUntil(lambda: bool(delivered))
    assert dialog._finished
    assert dialog._session is None
    assert dialog._worker is None
    assert "取消" in dialog.error


def test_close_between_staging_batches_discards_private_rows(mm, qtbot, monkeypatch):
    for i in range(3):
        _make_deployable_mod(mm._get_library_root("game_a"), str(i))
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    original = RebuildSession.stage_batch

    def one(session, size=32):
        complete = original(session, 1)
        QTimer.singleShot(0, dialog.close)
        return complete

    monkeypatch.setattr(RebuildSession, "stage_batch", one)
    dialog._start()
    qtbot.waitUntil(lambda: dialog._finished)
    assert "取消" in dialog.error
    assert not mm.get_mods()
    assert not mm._rebuild_inflight_ids
    assert dialog._session.stage is None
    # 清理后过期定时器或完成信号不得产生副作用。
    dialog._stage_next()
    dialog._commit()
    dialog._prepared()
    assert not mm.get_mods()


def test_postcommit_sync_failure_keeps_success_and_ignores_cancel(mm, qtbot, monkeypatch):
    from src.core.rebuild_session import _SyncRestore

    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    ready, release = threading.Event(), threading.Event()

    def broken(work, progress):
        ready.set()
        release.wait(5)
        raise OSError("injected sync error after publication")

    monkeypatch.setattr(_SyncRestore, "run", broken)
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)
    dialog._start()
    try:
        qtbot.waitUntil(ready.is_set)
        assert len(mm.get_mods()) == 1
        assert dialog._session._published
        dialog.reject()
        assert not dialog._finished
        assert not dialog._session.cancel.is_set()
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._finished)
    assert not dialog.error
    assert dialog.report.registered == 1
    assert len(mm.get_mods()) == 1
    assert not mm.library_probe_busy()
    assert not mm._rebuild_inflight_ids
    assert dialog._session.stage is None
