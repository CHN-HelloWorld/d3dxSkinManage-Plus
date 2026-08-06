from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.core.library_events import LibraryEventService
from src.core.library_inventory import SourceInventory
from src.core.library_snapshot import ProbeResult, ProbeToken
from src.core.watcher import FileChange
from src.ui.library_reconcile_worker import LibraryReconcileController
from src.ui.main_window import MainWindow
from tests.core.test_library_reconcile import mm as mm


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "new_health_event",
        "other_request",
        "source_only",
        "cached_audit",
        "worker_error",
        "entry_error",
        "new_service",
    ],
)
def test_only_matching_completed_full_audit_clears_native_health(mm, qtbot, invalid):
    root = mm._index.paths.roots["game_a"]
    root.mkdir(parents=True, exist_ok=True)
    info = root.stat()
    event = FileChange(str(root), 3, "overflow", str(root), root_identity=(info.st_dev, info.st_ino))
    service = mm._local_source_events = LibraryEventService(mm._index.paths, generation=3)
    service.handle(event)
    assert service._tainted
    controller = LibraryReconcileController(mm, busy=lambda: False)
    controller._valid = lambda result: True
    request = replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full", force_source_audit=True)
    controller._source_recovery_claim = controller._capture_source_recovery(request)
    assert controller._source_recovery_claim is not None
    inventory = SourceInventory(str(root), (), (), 0)
    result = ProbeResult(request, source_inventory=inventory)
    if invalid == "new_health_event":
        service.handle(event)
    elif invalid == "other_request":
        result = replace(result, request=replace(request, token=ProbeToken("game_a", 1, 2)))
    elif invalid == "source_only":
        result = replace(result, request=replace(request, mode="source"))
    elif invalid == "cached_audit":
        result = replace(result, request=replace(request, force_source_audit=False))
    elif invalid == "worker_error":
        result = replace(result, error="unreadable directory")
    elif invalid == "entry_error":
        result = replace(
            result, source_inventory=replace(inventory, entries=(SimpleNamespace(error="permission denied"),))
        )
    elif invalid == "new_service":
        mm._local_source_events = LibraryEventService(mm._index.paths, generation=4)
    assert controller._accept_source_recovery(result) is (not invalid)
    assert service._tainted is bool(invalid)


def test_watcher_restart_retries_failed_start_but_never_revives_stopped_generation(monkeypatch):
    callbacks, starts, audits = [], [], []

    def schedule(delay, context, callback):
        assert context is window
        callbacks.append(callback)

    monkeypatch.setattr("src.ui.main_window.call_later", schedule)
    monkeypatch.setattr("src.ui.main_window.fs.is_dir", lambda path: True)

    # 真实 Qt 监控器可作为映射键，替身也须可哈希。
    class Watcher:
        generation = 2

        def start(self, root):
            self.generation += 2
            starts.append(root)
            if len(starts) == 1:
                raise OSError("temporarily unavailable")
            return True

    watcher = Watcher()
    window = SimpleNamespace(
        _file_watcher=watcher,
        _mod_manager=SimpleNamespace(_local_source_events=object()),
        _library_probe=SimpleNamespace(schedule=lambda **kw: audits.append(kw)),
    )
    MainWindow._schedule_watcher_restart(window, watcher, 2, "root")
    callbacks.pop(0)()
    callbacks.pop(0)()
    assert starts == ["root", "root"]
    assert audits == [{"force_source_audit": True}]
    assert window._mod_manager._local_source_events is None
    MainWindow._schedule_watcher_restart(window, watcher, watcher.generation, "root")
    watcher.generation += 1  # 关闭或游戏切换使已排队重启失效。
    callbacks.pop(0)()
    assert len(starts) == 2
