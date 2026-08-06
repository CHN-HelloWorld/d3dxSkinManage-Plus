from threading import Event

from tests.core.test_library_reconcile import mm as mm


def test_invalidation_interrupts_obsolete_io_and_retries_without_error_backoff(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, released = Event(), Event()
    requests = []
    original = module.probe_library

    def probe(request, *, should_cancel):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert released.wait(5)
        return original(request, should_cancel=should_cancel)

    monkeypatch.setattr(module, "probe_library", probe)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        controller.invalidate()
        assert controller.worker.isInterruptionRequested()
        released.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2, timeout=700)
        assert controller.metrics["discarded"] == 1
        assert controller.metrics["max_active"] == 1
    finally:
        released.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_discovery_capture_does_not_normalize_all_sources_on_owner_thread(mm, monkeypatch):
    from src.models.mod import Mod
    from src.ui import library_discovery as module

    root = mm._index.paths.roots[mm.current_game_id]
    for index in range(100):
        name = f"Mod{index}"
        assert mm._index.add_mod(
            Mod(game_id=mm.current_game_id, name=name, folder_name=name, path=str(root / name / "body.7z"))
        )

    def forbidden(path):
        raise AssertionError("source normalization belongs to the worker")

    monkeypatch.setattr(module, "path_key", forbidden)
    request = module.capture_discovery(mm, None)
    assert len(request.existing_paths) == 100


def test_discovery_capture_does_not_load_deployment_records_or_category_counts(mm, monkeypatch):
    from src.ui.library_discovery import capture_discovery

    def unrelated_inventory(*args):
        raise AssertionError("discovery only needs source paths and category topology")

    monkeypatch.setattr(mm._index, "get_deployments", unrelated_inventory)
    monkeypatch.setattr(mm, "get_characters", unrelated_inventory)
    capture_discovery(mm, set())
