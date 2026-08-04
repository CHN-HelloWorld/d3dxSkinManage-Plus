"""验证资源发现快照、后台规划及中断时的生命周期。"""

from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_discovery_intake_preserves_shared_name_in_index_and_sidecar(qtbot, mm):
    """异步扫描所得文件名只是默认值，导入提交后仍须保留分享包的正式名称。"""
    from pathlib import Path

    from src.core.mod_intake import ModIntakeService
    from src.core.mod_package import read_sidecar
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController
    from tests.core.test_intake_source_name import _archive

    node = mm.add_character(name="Discovery", node_type="character")
    _archive(mm._get_character_library(node.id) / "Filesystem.zip", shared_name="Formal package")
    requests = []

    def consume(report, _options):
        """收集真实异步发现生成的请求，随后通过纳管事务提交。"""
        requests.extend(report.intake_requests)

    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=consume)
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        qtbot.waitUntil(lambda: bool(requests))
        assert len(requests) == 1
        service = ModIntakeService(mm)
        mod = service.commit(service.prepare(requests[0]))
        assert mod.name == "Formal package"
        assert mm.get_mod(mod.mod_id).name == "Formal package"
        assert read_sidecar(mod.path)["name"] == "Formal package"
        assert Path(mod.path).parent.name == "Filesystem"
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


@pytest.mark.parametrize("ending", ["complete", "mutation", "scope", "stop", "cancel"])
def test_discovery_capture_yields_and_discards_interrupted_snapshot(qtbot, mm, monkeypatch, ending):
    from PySide6.QtCore import QObject, Signal

    from src.ui import library_discovery as module

    class Probe(QObject):
        settled = Signal()
        invalidated = Signal()
        generation = 0
        source_inventory = None

        def schedule(self, **kwargs):
            pass

    probe = Probe()
    controller = module.LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=lambda *args: None)
    elapsed, steps, closed, requests = [0.0], [], [], []
    original = mm._index.iter_library_rows

    def rows(*args, **kwargs):
        try:
            for step in range(4):
                steps.append((step, get_ident()))
                elapsed[0] += 0.009
                yield None
            return (yield from original(*args, **kwargs))
        finally:
            closed.append(True)

    monkeypatch.setattr(mm._index, "iter_library_rows", rows)
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: elapsed[0]))
    monkeypatch.setattr(controller, "_start_worker", requests.append)
    try:
        controller.schedule(mm.current_game_id, trigger="manual")
        controller._prepare_timer.stop()
        controller._prepare()
        assert len(steps) == 1 and not requests, "capture must yield before draining the library"
        if ending == "mutation":
            mm.add_character(name="Changed during capture", node_type="character")
        elif ending == "scope":
            controller.reset_scope()
        elif ending == "stop":
            controller.stop()
        elif ending == "cancel":
            controller.cancel(cancel_probe=False)
        controller._capture_timer.stop()
        if ending == "complete":
            for _ in range(4):
                controller._capture_slice()
            assert len(requests) == 1
            assert requests[0].existing_paths == frozenset()
            assert all(tid == get_ident() for _, tid in steps)
        elif ending != "stop":
            controller._capture_slice()
        if ending != "complete":
            assert not requests and len(steps) == 1
        assert closed == [True], "the snapshot cursor must close on completion or cancellation"
    finally:
        controller.stop()


def test_missing_deployment_root_finishes_discovery_failure_without_intake(qtbot, mm, tmp_path):
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController

    root = tmp_path / "deployment"
    root.mkdir()
    mm.set_deploy_dir(str(root))
    root.rename(root.with_name("moved"))
    probe = LibraryReconcileController(mm, busy=lambda: False)
    consumed, errors = [], []
    controller = LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=lambda *args: consumed.append(args))
    controller.failed.connect(lambda error, options: errors.append((error, options)))
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        qtbot.waitUntil(lambda: bool(errors) and controller.worker is None)
        assert len(errors) == 1 and errors[0][1]["manual"]
        assert "FileNotFoundError" in errors[0][0]
        qtbot.wait(1100)
        assert not consumed and controller.active is None and controller.pending is None
        assert not controller._error_retry.isActive() and probe.metrics["started"] == 1
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_cancel_inside_planning_does_not_continue_or_report_spurious_error(qtbot, mm, monkeypatch, caplog):
    from src.core.mod_intake import ModIntakeService
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController
    from tests.core.test_library_reconcile import _drop_bare_mod

    node = mm.add_character(name="Plan cancel", node_type="character")
    _drop_bare_mod(mm, node.id, "first")
    _drop_bare_mod(mm, node.id, "second")
    probe = LibraryReconcileController(mm, busy=lambda: False)
    consumed, planned = [], []
    controller = LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=lambda *args: consumed.append(args))
    original = ModIntakeService.plan_request

    def plan(service, **kwargs):
        request = original(service, **kwargs)
        planned.append(request)
        controller.cancel()
        return request

    monkeypatch.setattr(ModIntakeService, "plan_request", plan)
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        qtbot.waitUntil(lambda: bool(planned))
        qtbot.wait(30)
        assert len(planned) == 1 and not consumed
        assert not any(record.levelname == "ERROR" for record in caplog.records)
        assert controller.active is None and controller.pending is None
        assert not controller._timer.isActive() and not probe.pending
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_probe_failure_reports_manual_request_queued_behind_background_discovery(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as probes
    from src.ui.library_discovery import LibraryDiscoveryController

    release = Event()
    original = probes.probe_library

    def blocked(request, **kwargs):
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(probes, "probe_library", blocked)
    probe = probes.LibraryReconcileController(mm, busy=lambda: False)
    controller = LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=lambda *args: None)
    failures = []
    controller.failed.connect(lambda error, options: failures.append(options))
    try:
        controller.schedule(mm.current_game_id, trigger="manual")
        qtbot.waitUntil(lambda: controller.active is not None)
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        probe.pause("deployment root unavailable")
        assert len(failures) == 1 and failures[0]["manual"]
        assert controller.active is None and controller.pending is None
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


@pytest.mark.parametrize("phase", ["queued", "worker", "waiting", "retry"])
def test_cancel_stops_entire_refresh_chain_and_allows_next_f5(qtbot, mm, monkeypatch, phase):
    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probes

    entered, release = Event(), Event()
    original_discover, original_probe = module.discover, probes.probe_library
    consumed, first = [], [True]

    def discover(*args):
        if first[0] and phase in {"worker", "retry"}:
            first[0] = False
            entered.set()
            if phase == "retry":
                raise PermissionError("temporary source error")
            assert release.wait(5)
        return original_discover(*args)

    def probe_library(request, **kwargs):
        if first[0] and phase == "waiting":
            first[0] = False
            entered.set()
            assert release.wait(5)
        return original_probe(request, **kwargs)

    monkeypatch.setattr(module, "discover", discover)
    monkeypatch.setattr(probes, "probe_library", probe_library)
    probe = probes.LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda *args: consumed.append(args)
    )
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        if phase != "queued":
            qtbot.waitUntil(entered.is_set)
        if phase == "waiting":
            qtbot.waitUntil(lambda: controller._prepared is not None)
        elif phase == "retry":
            qtbot.waitUntil(lambda: controller._error_retry.isActive())
        controller.cancel()
        assert not controller.closing and not probe.closing
        release.set()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)
        qtbot.wait(1100)
        assert not consumed and controller.active is None and controller.pending is None
        assert not probe.pending and not probe.applying
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        qtbot.waitUntil(lambda: len(consumed) == 1)
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


@pytest.fixture
def host(qtbot, mm):
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController
    from src.ui.main_window import MainWindow

    events, published = [], []

    class Host:
        _reconcile_library = MainWindow._reconcile_library
        _consume_library_discovery = MainWindow._consume_library_discovery
        _refresh_current = MainWindow._refresh_current
        _intake_worker = None
        _intake_settling = False
        _import_active = False
        _preset_apply_worker = None
        _watcher_suppress_until = 0

        def _schedule_source_cleanup_retry(self):
            pass

        def _refresh_after_source_reconcile(self, gid, removed):
            published.append((gid, removed))

        def _start_intake_batch(self, report, *, manual):
            events.append(("intake", len(report.intake_requests)))

    value = Host()
    value._game_service = SimpleNamespace(current_game_id=mm.current_game_id, has_active_game=lambda: True)
    value._intake_progress = SimpleNamespace(
        start_batch=lambda *a: events.append(("start",)),
        update_progress=lambda *a: events.append(("progress",)),
        finish=lambda *a: events.append(("finish",)),
    )
    value._mod_manager = mm

    def busy():
        return value._intake_worker is not None

    probe = value._library_probe = LibraryReconcileController(mm, busy=busy)
    probe.changed.connect(value._refresh_after_source_reconcile)
    controller = value._library_discovery = LibraryDiscoveryController(
        mm, probe, busy=busy, consume=value._consume_library_discovery
    )
    value.events, value.published = events, published

    def settle():
        qtbot.waitUntil(
            lambda: (
                controller.worker is None
                and controller.active is None
                and controller.pending is None
                and not probe.applying
                and probe.worker is None
            )
        )

    value.settle = settle
    try:
        yield value
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_manual_intake_scan_shows_progress_before_discovery(host):
    host._reconcile_library("game_a", trigger="manual", manual=True)
    assert host.events == [("start",), ("progress",)]
    host.settle()
    assert host.events == [("start",), ("progress",), ("finish",)]


def test_startup_discovery_waits_for_offline_relocation_audit(qtbot, mm, tmp_path, monkeypatch):
    from pathlib import Path

    from src.ui import library_reconcile_worker as probe_module
    from src.ui.library_discovery import LibraryDiscoveryController

    char = mm.add_character(name="Offline", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "existing")
    container = Path(mod.path).parent
    moved = container.with_name("MovedOffline")
    container.rename(moved)
    release = Event()
    original = probe_module.probe_library
    consumed = []

    def held(request, **kwargs):
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(probe_module, "probe_library", held)
    from src.ui import library_discovery as discovery_module

    original_discover = discovery_module.discover
    requests = []

    def recorded(request, *args):
        requests.append(request)
        return original_discover(request, *args)

    monkeypatch.setattr(discovery_module, "discover", recorded)
    probe = probe_module.LibraryReconcileController(mm, busy=lambda: False)
    controller = LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule("game_a", trigger="game_switch")
        qtbot.waitUntil(lambda: probe.worker is not None)
        assert controller.worker is None, "startup must not scan stale paths before recovery"
        assert not consumed, "known offline identities must be recovered before intake planning"
        release.set()
        qtbot.waitUntil(lambda: bool(consumed))
        assert not consumed[0].intake_requests
        recovered = mm.get_mod(mod.mod_id)
        assert Path(recovered.path) == moved / Path(mod.path).name
        assert recovered.imported_at == mod.imported_at
        assert (moved / ".d3dxmod").is_file()
        assert len(requests) == 1, "accepted recovery should require only one discovery pass"
        assert requests[0].audit_owns_known_checks
        assert str(moved / Path(mod.path).name) in requests[0].existing_paths
        assert probe.metrics["started"] == 1
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


@pytest.mark.parametrize("change", ["unchanged", "repoint", "corrupt", "permission", "escaping_link", "internal_link"])
def test_manual_preparation_reuses_cache_only_after_matching_fresh_audit(qtbot, mm, tmp_path, monkeypatch, change):
    import json
    import shutil
    from pathlib import Path

    from src.core import library_inventory
    from src.core.library_snapshot import ProbeToken
    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probe_module
    from tests.core.test_library_discovery_reads import _replace_archive_with_folder
    from tests.core.test_mod_package import _link_directory

    char = mm.add_character(name="Verified", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "known")
    body = Path(mod.path)
    if change.endswith("_link"):
        _replace_archive_with_folder(mod)
    inventory = library_inventory.scan_source_inventory(mm.create_library_probe_request(ProbeToken("game_a", 0, 0)))
    container = Path(mod.path).parent
    marker_path = container / ".d3dxmod"
    version = library_inventory.marker_version(container)
    if change in {"repoint", "corrupt"}:
        data = json.loads(marker_path.read_text(encoding="utf8"))
        replacement = container / "replacement.zip"
        shutil.copy2(mod.path, replacement)
        data["body"] = replacement.name
        marker_path.write_text("{broken" if change == "corrupt" else json.dumps(data), encoding="utf8")
    elif change.endswith("_link"):
        target = tmp_path / "outside" if change == "escaping_link" else container / "internal"
        body.rename(target)
        _link_directory(body, target)
    original_version = library_inventory.marker_version

    def marker_version(path):
        if Path(path) == container:
            if change == "permission":
                raise PermissionError("unreadable known marker")
            return version
        return original_version(path)

    monkeypatch.setattr(library_inventory, "marker_version", marker_version)
    if change == "permission":
        from src.core import mod_package

        original_read = mod_package.read_container_marker

        def unreadable(path):
            if Path(path) == container:
                raise PermissionError("unreadable known marker")
            return original_read(path)

        monkeypatch.setattr(mod_package, "read_container_marker", unreadable)
    release = Event()
    original_probe = probe_module.probe_library
    requests, consumed = [], []
    original_discover = module.discover

    def held(*args, **kwargs):
        assert release.wait(5)
        return original_probe(*args, **kwargs)

    def discover(request, cancel, observed=None):
        requests.append(request)
        return original_discover(request, cancel, observed)

    monkeypatch.setattr(probe_module, "probe_library", held)
    monkeypatch.setattr(module, "discover", discover)
    probe = probe_module.LibraryReconcileController(mm, busy=lambda: False)
    probe.source_inventory = inventory
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule("game_a", trigger="manual", manual=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1, timeout=2000)
        assert requests[0].source_inventory is inventory
        assert not requests[0].verify_markers, "strong audit owns full raw marker validation"
        assert requests[0].audit_owns_known_checks
        assert not consumed
        release.set()
        if change == "permission":
            qtbot.waitUntil(lambda: probe.metrics["discarded"] == 1, timeout=3000)
            assert not consumed
            assert probe.source_inventory is inventory
            assert probe.paused and not probe.pending and mm.get_mod(mod.mod_id) is not None
            assert controller.active is None and controller.pending is None
            return
        qtbot.waitUntil(lambda: bool(consumed), timeout=3000)
        assert probe.metrics["started"] == 1, "changed inventory retries only discovery"
        if change in {"unchanged", "internal_link"}:
            assert len(requests) == 1 and not consumed[0].intake_requests
        elif change == "repoint":
            assert len(requests) == 2
            assert requests[1].audit_owns_known_checks
            assert requests[1].source_inventory is probe.source_inventory
            assert consumed[0].intake_requests == []
            assert mm.get_mod(mod.mod_id).path == str(replacement)
        else:
            assert len(requests) == 2 and consumed[0].invalid_containers == [str(container)]
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)
        if change.endswith("_link"):
            if body.is_symlink():
                body.unlink()
            else:
                body.rmdir()


def test_merged_manual_discovery_keeps_full_marker_verification(host, monkeypatch):
    from src.ui import library_discovery as module

    requests = []
    original = module.discover

    def discover(request, cancel, observed=None):
        requests.append(request)
        return original(request, cancel, observed)

    monkeypatch.setattr(module, "discover", discover)
    host._intake_worker = object()
    host._reconcile_library("game_a", trigger="manual", manual=True)
    host._reconcile_library("game_a", trigger="watcher", changed_paths={"new"})
    host._intake_worker = None
    host.settle()
    assert len(requests) == 1
    assert requests[0].changed_paths is None
    assert host._library_probe.last_mode == "full"
    assert host._library_probe.last_force_source_audit


def test_watcher_without_new_input_does_not_show_intake_progress(host):
    host._reconcile_library("game_a", trigger="watcher", changed_paths=set())
    host.settle()
    assert host.events == [] and host.published == []


def test_preset_commit_and_manual_refresh_always_republish_views(host):
    for options in ({"after_preset_commit": True}, {"manual": True}):
        host.published.clear()
        host._refresh_current(**options)
        host.settle()
        assert host.published == [("game_a", 0)]
    host.published.clear()
    host._reconcile_library("game_a", trigger="poll")
    host.settle()
    assert host.published == []


def test_source_reconcile_reloads_only_for_real_change(host, mm, tmp_path):
    import shutil
    from pathlib import Path

    char = mm.add_character(name="Changed", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "changed")
    host._reconcile_library("game_a", trigger="poll")
    host.settle()
    assert host.published == []
    container = Path(mod.path).parent
    renamed = container.with_name("Renamed")
    container.rename(renamed)
    host._reconcile_library("game_a", trigger="poll")
    host.settle()
    assert host.published == [("game_a", 0)]
    host.published.clear()
    shutil.rmtree(renamed)
    host._reconcile_library("game_a", trigger="poll")
    host.settle()
    assert host.published == [("game_a", 1)]


def test_intake_refresh_triggers_coalesce_to_one_followup_scan(host):
    host._intake_worker = object()
    for trigger, paths, manual in [("watcher", {"a"}, False), ("manual", None, True), ("watcher", {"b"}, False)]:
        host._reconcile_library("game_a", trigger=trigger, changed_paths=paths, manual=manual)
    controller = host._library_discovery
    assert controller.worker is None
    assert controller.pending["manual"] and controller.pending["changed_paths"] is None
    assert host.events == []
    host._intake_worker = None
    host.settle()
    assert host._library_probe.metrics["started"] == 1
    assert host.events == [("finish",)]


def test_discovery_reads_files_off_thread_and_plans_on_owner(qtbot, mm, tmp_path, monkeypatch):
    from src.core.mod_intake import ModIntakeService
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="Discovery", node_type="character")
    _import_mod(mm, char.id, tmp_path, "existing")
    incoming = mm._get_character_library(char.id, mm.current_game_id) / "Incoming"
    incoming.mkdir()
    (incoming / "mod.ini").write_text("[Constants]\nglobal $new = 1")
    owner = get_ident()
    scans, plans, consumed = [], [], []
    original_scan, original_plan = module.discover, ModIntakeService.plan_request

    def scan(*args):
        scans.append(get_ident())
        return original_scan(*args)

    def plan(self, **kwargs):
        plans.append(get_ident())
        return original_plan(self, **kwargs)

    monkeypatch.setattr(module, "discover", scan)
    monkeypatch.setattr(ModIntakeService, "plan_request", plan)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule(mm.current_game_id, trigger="manual")
        qtbot.waitUntil(lambda: bool(consumed))
        assert scans and all(tid != owner for tid in scans)
        assert plans == [owner]
        assert [r.source_path for r in consumed[0].intake_requests] == [incoming]
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_discovery_invalidated_during_io_retries_and_keeps_heartbeat(qtbot, mm, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    entered, release = Event(), Event()
    consumed, scans, beats = [], [], []
    original = module.discover

    def blocked(*args):
        result = original(*args)
        scans.append(result)
        if len(scans) == 1:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(module, "discover", blocked)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append((report, options))
    )
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(1)
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        controller.schedule(mm.current_game_id, trigger="watcher", changed_paths={"changed"})
        qtbot.waitUntil(entered.is_set)
        assert controller.active["trigger"] == "watcher"
        assert controller.active["changed_paths"] is None
        probe.invalidate()
        qtbot.waitUntil(lambda: len(beats) >= 3)
        assert not consumed
        release.set()
        qtbot.waitUntil(lambda: bool(consumed))
        assert len(scans) == 2
        assert consumed[0][1]["manual"]
    finally:
        release.set()
        controller.stop()
        probe.stop()
        timer.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_old_game_discovery_never_publishes(qtbot, mm, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    entered, release = Event(), Event()
    consumed = []
    original = module.discover

    def blocked(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, "discover", blocked)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule(mm.current_game_id, trigger="game_switch")
        qtbot.waitUntil(entered.is_set)
        mm.set_game("game_b")
        release.set()
        qtbot.waitUntil(lambda: controller.worker is None)
        assert not consumed and controller.active is None
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_new_game_request_queued_behind_old_worker_is_not_lost(qtbot, mm, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    entered, release = Event(), Event()
    consumed, calls = [], []
    original = module.discover

    def blocked(*args):
        result = original(*args)
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(module, "discover", blocked)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report.game_id)
    )
    try:
        controller.schedule("game_a", trigger="game_switch")
        qtbot.waitUntil(entered.is_set)
        mm.set_game("game_b")
        controller.schedule("game_b", trigger="game_switch")
        qtbot.waitUntil(lambda: probe.worker is None and not probe.applying)
        release.set()
        qtbot.waitUntil(lambda: bool(consumed))
        assert consumed == ["game_b"]
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_unrelated_import_does_not_suppress_local_watcher_facts(monkeypatch):
    from src.ui import main_window
    from src.ui.main_window import MainWindow

    scheduled, applied, published = [], [], []
    host = SimpleNamespace(
        _import_active=True,
        _watcher_suppress_until=2,
        _watcher_changed_paths={"external-new-mod"},
        _watcher_events=["bounded-event"],
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_source_events=lambda events: applied.extend(events) or "delta"),
        _publish_local_library_delta=published.append,
        _watcher_debounce=SimpleNamespace(start=scheduled.append),
    )
    monkeypatch.setattr(main_window.time, "monotonic", lambda: 1)
    MainWindow._refresh_from_watcher(host)
    assert scheduled == [] and host._watcher_events == []
    assert applied == ["bounded-event"] and published == ["delta"]


def test_discovery_index_failure_preserves_pending_without_qt_exception(qtbot, mm, monkeypatch, tmp_path):
    import sqlite3

    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    entered, release = Event(), Event()
    consumed = []
    node = mm.add_character("Incoming")
    source = mm._index.paths.node_path(node.id) / "new.zip"
    source.write_bytes(b"payload")
    original = module.discover

    def blocked(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, "discover", blocked)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(mm, probe, busy=lambda: False, consume=lambda *a: consumed.append(1))
    original_busy = mm._index.paths.locate

    def unavailable(*args):
        raise sqlite3.OperationalError("index unavailable")

    try:
        controller.schedule("game_a", trigger="watcher", changed_paths={str(source)})
        qtbot.waitUntil(entered.is_set)
        monkeypatch.setattr(mm._index.paths, "locate", unavailable)
        release.set()
        qtbot.waitUntil(lambda: controller.worker is None)
        assert not consumed and controller.pending is not None
        monkeypatch.setattr(mm._index.paths, "locate", original_busy)
        controller.schedule("game_a", trigger="watcher", changed_paths={str(source)})
        qtbot.waitUntil(lambda: bool(consumed))
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_persistent_discovery_error_backs_off_but_new_event_retries(qtbot, mm, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    failing, consumed = [True], []
    original = module.discover

    def discover(*args):
        if failing[0]:
            raise PermissionError("unreadable new directory")
        return original(*args)

    monkeypatch.setattr(module, "discover", discover)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda *args: consumed.append(1)
    )
    try:
        controller.schedule("game_a", trigger="manual", manual=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] >= 1)
        qtbot.wait(200)
        assert controller.metrics["started"] == 1
        assert controller.pending["manual"] and not consumed
        failing[0] = False
        controller.schedule("game_a", trigger="watcher", changed_paths={"new-event"})
        qtbot.waitUntil(lambda: bool(consumed), timeout=700)
        assert controller.metrics["started"] == 2
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_discovery_reads_during_probe_but_waits_for_accepted_probe(qtbot, mm, tmp_path, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probe_module

    char = mm.add_character(name="Overlap", node_type="character")
    incoming = mm._get_character_library(char.id, "game_a") / "Incoming"
    incoming.mkdir()
    (incoming / "mod.ini").write_text("[Constants]\nglobal $new = 1")
    entered, release = Event(), Event()
    consumed = []
    original = probe_module.probe_library

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(probe_module, "probe_library", blocked_probe)
    probe = probe_module.LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule("game_a", trigger="manual")
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1, timeout=1000)
        assert not consumed and not controller.planning
        release.set()
        qtbot.waitUntil(lambda: bool(consumed))
        assert [item.source_path for item in consumed[0].intake_requests] == [incoming]
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_overlapped_discovery_reacquires_after_probe_relocates_source(qtbot, mm, tmp_path, monkeypatch):
    from pathlib import Path

    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probe_module

    char = mm.add_character(name="Relocated", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "known")
    container = Path(mod.path).parent
    moved = container.with_name("Renamed")
    container.rename(moved)
    release = Event()
    consumed = []
    original = probe_module.probe_library

    def blocked_probe(*args, **kwargs):
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(probe_module, "probe_library", blocked_probe)
    probe = probe_module.LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule("game_a", trigger="manual")
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1, timeout=1000)
        assert not consumed
        release.set()
        qtbot.waitUntil(lambda: bool(consumed))
        assert consumed[0].intake_requests == []
        assert controller.metrics["started"] == 2
        assert controller.metrics["discarded"] == 1
        assert mm.get_mod(mod.mod_id).path == str(moved / Path(mod.path).name)
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


@pytest.mark.parametrize(
    "trigger,partial,accepted",
    [
        ("manual", "source", "full"),
        ("manual", "deployment", "full"),
        ("manual", "full", "full"),
        ("watcher", "deployment", "source"),
    ],
)
@pytest.mark.parametrize("discover", [True, False])
def test_discovery_requires_probe_covering_requested_scope(qtbot, mm, trigger, partial, accepted, discover):
    from PySide6.QtCore import QObject, Signal

    from src.ui.library_discovery import LibraryDiscoveryController

    class Probe(QObject):
        settled = Signal()
        generation = 0
        applying = False
        worker = None
        last_mode = ""
        last_force_source_audit = False

        def schedule(self, *, force_source_audit=False):
            self.worker = object()

        def settle(self, mode, *, strong=True):
            self.worker = None
            self.last_mode = mode
            self.last_force_source_audit = strong
            self.settled.emit()

    consumed = []
    probe = Probe()
    controller = LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule(
            "game_a", trigger=trigger, changed_paths={"new"} if trigger == "watcher" else None, discover=discover
        )
        if discover:
            qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        if trigger == "watcher":
            qtbot.waitUntil(lambda: bool(consumed))
            assert len(consumed) == 1, "local watcher discovery is independent of audit completion"
            return
        probe.settle(partial, strong=False)
        assert consumed == []
        probe.settle(accepted)
        qtbot.waitUntil(lambda: bool(consumed))
        assert len(consumed) == 1
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_obsolete_discovery_cancels_promptly_without_error_backoff(qtbot, mm, monkeypatch):
    from src.ui import library_discovery as module
    from src.ui.library_reconcile_worker import LibraryReconcileController

    entered, cancelled, release = Event(), Event(), Event()
    original = module.discover
    calls, consumed = [], []

    def blocked(request, cancel, observed=None):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            while not release.wait(0.01):
                if cancel():
                    cancelled.set()
                    raise InterruptedError("superseded")
        return original(request, cancel, observed)

    monkeypatch.setattr(module, "discover", blocked)
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda *args: consumed.append(1)
    )
    try:
        controller.schedule("game_a", trigger="manual")
        qtbot.waitUntil(entered.is_set)
        probe.invalidate()
        qtbot.waitUntil(cancelled.is_set, timeout=500)
        qtbot.waitUntil(lambda: bool(consumed), timeout=700)
        assert len(calls) == 2
        assert not controller._error_retry.isActive()
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_accepted_discovery_finishes_sliced_planning_during_same_generation_poll(qtbot, mm, monkeypatch):
    import time

    from src.core.mod_intake import ModIntakeService
    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probe_module

    char = mm.add_character(name="Mixed", node_type="character")
    sources = []
    for name in ("First", "Second"):
        source = mm._get_character_library(char.id, "game_a") / name
        source.mkdir()
        (source / "mod.ini").write_text("[Constants]\nx=" + name, encoding="utf-8")
        sources.append(source)
    release = Event()
    calls, plans, consumed = [], [], []
    original_probe, original_plan = probe_module.probe_library, ModIntakeService.plan_request

    def blocked_probe(request, **kwargs):
        calls.append(request)
        if len(calls) > 1:
            assert release.wait(5)
        return original_probe(request, **kwargs)

    probe = probe_module.LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )

    def plan(service, **kwargs):
        result = original_plan(service, **kwargs)
        plans.append(result)
        if len(plans) == 1:
            probe.schedule()
            time.sleep(0.012)  # 强制进入所有者执行预算耗尽后的继续调度路径。
        return result

    monkeypatch.setattr(probe_module, "probe_library", blocked_probe)
    monkeypatch.setattr(ModIntakeService, "plan_request", plan)
    try:
        controller.schedule("game_a", trigger="manual", manual=True)
        qtbot.waitUntil(lambda: bool(consumed), timeout=1800)
        assert [item.source_path for item in consumed[0].intake_requests] == sources
        assert len(plans) == 2
        assert probe.worker is not None
    finally:
        release.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)


def test_full_discovery_yields_and_observes_owner_cancellation(qapp, monkeypatch):
    from src.ui import library_discovery as module

    worker = module.DiscoveryWorker(None, None)
    current, cancelled, results = [0.0], [False], []
    monkeypatch.setattr(module.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: cancelled[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: cancelled.__setitem__(0, True))

    def discover(request, check, observed):
        assert check() is False
        current[0] = 1.0
        results.append(check())
        return ([], [])

    monkeypatch.setattr(module, "discover", discover)
    worker.run()
    assert results == [True]


@pytest.mark.parametrize("mutation", ["none", "sql", "generation"])
@pytest.mark.parametrize("audit_state", ["running", "settled"])
def test_obsolete_discovery_joins_current_full_audit_instead_of_queuing_another(
    qtbot, mm, monkeypatch, mutation, audit_state
):
    from src.ui import library_discovery as module
    from src.ui import library_reconcile_worker as probes

    release_first, release_second, release_discovery = Event(), Event(), Event()
    second_entered, discovery_entered = Event(), Event()
    original_probe, original_discover = probes.probe_library, module.discover
    calls, consumed, discoveries = [], [], []

    def blocked_probe(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            assert release_first.wait(5)
        elif len(calls) == 2:
            second_entered.set()
            assert release_second.wait(5)
        return original_probe(request, **kwargs)

    def blocked_discovery(*args):
        discoveries.append(1)
        if len(discoveries) == 1:
            discovery_entered.set()
            assert release_discovery.wait(5)
        return original_discover(*args)

    monkeypatch.setattr(probes, "probe_library", blocked_probe)
    monkeypatch.setattr(module, "discover", blocked_discovery)
    probe = probes.LibraryReconcileController(mm, busy=lambda: False)
    controller = module.LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: consumed.append(report)
    )
    try:
        controller.schedule(mm.current_game_id, trigger="manual", manual=True)
        qtbot.waitUntil(lambda: len(calls) == 1 and discovery_entered.is_set())
        mm.add_character(name="Committed intake", node_type="character")
        probe.invalidate()
        probe.schedule(force_source_audit=True)
        release_first.set()
        qtbot.waitUntil(second_entered.is_set)
        if audit_state == "settled":
            release_second.set()
            qtbot.waitUntil(lambda: probe.worker is None and not probe.pending)
        if mutation == "sql":
            mm.add_character(name="Changed again", node_type="character")
        elif mutation == "generation":
            probe.invalidate()
        release_discovery.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] >= 1)
        release_second.set()
        qtbot.waitUntil(lambda: bool(consumed) and probe.worker is None and not probe.pending)
        assert len(calls) == (2 if mutation == "none" else 3), "only a current audit may cover the retry"
        assert len(consumed) == 1 and not consumed[0].intake_requests
        if mutation == "none" and audit_state == "settled":
            controller.schedule(mm.current_game_id, trigger="manual", manual=True)
            qtbot.waitUntil(lambda: len(consumed) == 2 and probe.worker is None and not probe.pending)
            assert len(calls) == 3, "a new explicit F5 must perform a new audit"
    finally:
        release_first.set()
        release_second.set()
        release_discovery.set()
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)
