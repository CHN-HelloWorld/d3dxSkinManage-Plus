from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from tests.core.test_variant_preset_switch import env as env


def test_enable_publication_keeps_heartbeat_and_publishes_only_after_configuration(env, qtbot, monkeypatch):
    from src.core import mod_variant_manager
    from src.ui.deployment_enable import EnablePublicationController

    mm, mod = env["mm"], env["mod"]
    staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    worker = SimpleNamespace(
        result=(True, str(staged)), _stage_only=True, source_receipt=None, created_deployment=True, error=None
    )
    entered, release = Event(), Event()
    owner = get_ident()
    original = mod_variant_manager.inspect_effective_inis

    def delayed(*args, **kwargs):
        assert get_ident() != owner
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", delayed)
    finished = []

    def publish(mid, deploy_worker, disabled):
        prepared = deploy_worker.enable_preparation
        result = mm.finalize_enable(
            mid, True, deploy_path=prepared.deploy_path, source_receipt=prepared.source_receipt, preparation=prepared
        )
        finished.append(result)

    controller = EnablePublicationController(mm, publish)
    timer = QTimer()
    beats = []
    timer.timeout.connect(lambda: beats.append(True))
    timer.start(10)
    try:
        controller.enqueue(mod.mod_id, worker, [])
        qtbot.waitUntil(entered.is_set, timeout=5000)
        qtbot.waitUntil(lambda: len(beats) >= 3, timeout=1000)
        assert not finished
        assert not mm.get_mod(mod.mod_id).is_enabled
        assert controller.busy
    finally:
        release.set()
        qtbot.waitUntil(lambda: not controller.busy, timeout=15000)
        timer.stop()
    assert len(finished) == 1 and finished[0].is_enabled


def test_queued_enable_checks_namespaces_against_previously_published_mod(env, qtbot):
    from src.ui.deployment_enable import EnablePublicationController
    from tests.core.test_named_variant_coordination import add_mod

    mm = env["mm"]
    ini = "namespace = shared\n[Constants]\nglobal persist $choice = 0\n"
    mods = [add_mod(env, name, ini) for name in ("First", "Second")]
    workers = []
    for mod in mods:
        staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
        workers.append(
            SimpleNamespace(
                result=(True, str(staged)), _stage_only=True, source_receipt=None, created_deployment=True, error=None
            )
        )
    completed = []

    def publish(mid, worker, disabled):
        completed.append(mid)
        if worker.configuration_error is None:
            preparation = worker.enable_preparation
            assert mm.finalize_enable(
                mid,
                True,
                deploy_path=preparation.deploy_path,
                source_receipt=preparation.source_receipt,
                preparation=preparation,
            ).is_enabled

    controller = EnablePublicationController(mm, publish)
    for mod, worker in zip(mods, workers):
        controller.enqueue(mod.mod_id, worker, [])
    qtbot.waitUntil(lambda: not controller.busy, timeout=15000)
    assert completed == [mod.mod_id for mod in mods]
    assert mm.get_mod(mods[0].mod_id).is_enabled
    assert not mm.get_mod(mods[1].mod_id).is_enabled
    assert isinstance(workers[1].configuration_error, ValueError)
    assert not Path(workers[1].result[1]).exists()


def test_failed_enable_restores_conflicting_mod_off_thread(env, qtbot):
    from src.ui.deployment_enable import EnablePublicationController
    from tests.core.test_named_variant_coordination import add_mod

    mm, previous = env["mm"], env["mod"]
    target = add_mod(
        env, "Broken", "namespace = shared\n[Constants]\nglobal persist $choice = 0\nglobal persist $choice = 1\n"
    )
    assert mm.toggle_mod(previous.mod_id).is_enabled
    staged = mm._storage.materialize_deployment(target.path, target.mod_id, target.folder_name, active=False)
    worker = SimpleNamespace(
        result=(True, str(staged)), _stage_only=True, source_receipt=None, created_deployment=True, error=None
    )
    completed = []
    controller = EnablePublicationController(mm, lambda *args: completed.append(args))
    controller.enqueue(target.mod_id, worker, [previous.mod_id])
    qtbot.waitUntil(lambda: not controller.busy, timeout=15000)
    assert len(completed) == 1
    assert worker.configuration_error is not None
    assert mm.get_mod(previous.mod_id).is_enabled
    assert previous.mod_id in mm.preset_manager.get_current("g1").enabled_mod_ids
    assert not mm.get_mod(target.mod_id).is_enabled
    assert not Path(worker.result[1]).exists()


def test_close_finishes_active_configuration_and_cleans_queued_deployment(env, qtbot, monkeypatch):
    from src.core import mod_variant_manager
    from src.ui.deployment_enable import EnablePublicationController
    from tests.core.test_named_variant_coordination import add_mod

    mm = env["mm"]
    second = add_mod(env, "Second", "[Constants]\nglobal persist $other = 0\n")
    mods = [env["mod"], second]
    workers = []
    for mod in mods:
        staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
        workers.append(
            SimpleNamespace(
                result=(True, str(staged)), _stage_only=True, source_receipt=None, created_deployment=True, error=None
            )
        )
    entered, release = Event(), Event()
    original = mod_variant_manager.inspect_effective_inis

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", delayed)

    def publish(mid, worker, disabled):
        if worker.configuration_error is None:
            preparation = worker.enable_preparation
            assert mm.finalize_enable(
                mid,
                True,
                deploy_path=preparation.deploy_path,
                source_receipt=preparation.source_receipt,
                preparation=preparation,
            ).is_enabled

    controller = EnablePublicationController(mm, publish)
    stopped = []
    controller.stopped.connect(lambda: stopped.append(True))
    for mod, worker in zip(mods, workers):
        controller.enqueue(mod.mod_id, worker, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        controller.close()
        assert controller.busy and not stopped
    finally:
        release.set()
        qtbot.waitUntil(lambda: bool(stopped), timeout=15000)
    assert mm.get_mod(mods[0].mod_id).is_enabled
    assert not mm.get_mod(second.mod_id).is_enabled
    assert isinstance(workers[1].configuration_error, InterruptedError)
    assert not Path(workers[1].result[1]).exists()


def _real_deploy_window(env, qtbot):
    from src.ui.deployment_enable import EnablePublicationController
    from tests.ui.test_deploy_responsiveness import _DeployWindow

    mm = env["mm"]
    window = _DeployWindow(mm.get_mods(), mm._storage)
    window._mod_manager = mm
    window._game_service = mm._game_service
    window._enable_publication = EnablePublicationController(mm, window._on_async_deploy_finished, window)
    window._enable_publication_close_pending = False
    window._deployment_activation.closing = False
    window._watcher_changed_paths = set()
    window._pending_gb_installs = []
    window._enable_publication.idle.connect(window._on_enable_publication_idle)
    qtbot.addWidget(window)
    return window


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_independent_deployments_use_configured_slots_during_publication(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
    concurrency,
):
    from src.core import mod_variant_manager
    from tests.core.test_named_variant_coordination import add_mod

    mm, first = env["mm"], env["mod"]
    others = []
    for index in range(4):
        character = mm.add_character(name=f"Independent {index}", node_type="character")
        others.append(add_mod(dict(env, char=character), f"Next{index}", "[Constants]\nglobal $ordinary = 10\n"))
    window = _real_deploy_window(env, qtbot)
    window._config.set("mods.deploy_concurrency", concurrency)
    entered, release = Event(), Event()
    started = set()
    inspect = mod_variant_manager.inspect_effective_inis
    materialize = mm._storage.materialize_deployment

    def held_configuration(path):
        if str(path) == first.path:
            entered.set()
            assert release.wait(5)
        return inspect(path)

    def held_materialization(source, mod_id, *args, **kwargs):
        if mod_id != first.mod_id:
            started.add(mod_id)
            assert release.wait(5)
        return materialize(source, mod_id, *args, **kwargs)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", held_configuration)
    monkeypatch.setattr(mm._storage, "materialize_deployment", held_materialization)
    window._on_overview_mod_toggle(first.mod_id)
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        for mod in others:
            window._on_overview_mod_toggle(mod.mod_id)
            window._on_overview_mod_toggle(mod.mod_id)
        qtbot.waitUntil(lambda: len(started) == concurrency - 1, timeout=1500)
        assert len(window._deploy_active) == concurrency
        assert len(window._pending_deploy_ids) == 5 - concurrency
        assert set(window._deploy_workers) == {first.mod_id, *(mod.mod_id for mod in others)}
        assert not any(mm.get_mod(mod.mod_id).is_enabled for mod in [first, *others])
        assert all(window.feedback[mod.mod_id] == "pending" for mod in [first, *others])
    finally:
        release.set()
        qtbot.waitUntil(
            lambda: (
                not window._deploy_workers
                and not window._enable_publication.busy
                and not getattr(window, "_deferred_enable_ids", {})
            ),
            timeout=15000,
        )
    assert all(mm.get_mod(mod.mod_id).is_enabled for mod in [first, *others])
    assert all(window.feedback[mod.mod_id] == "success" for mod in [first, *others])
    assert not mm._library_protection.snapshot()[1]


def test_same_character_waits_for_publication_then_replaces_previous_mod(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    from src.core import mod_variant_manager
    from tests.core.test_named_variant_coordination import add_mod

    mm, first = env["mm"], env["mod"]
    second = add_mod(env, "Next", "[Constants]\nglobal $bench = 10\n")
    window = _real_deploy_window(env, qtbot)
    window._config.set(window._CONFLICT_SUPPRESS_KEY, True)
    entered, release = Event(), Event()
    inspect = mod_variant_manager.inspect_effective_inis

    def held(path):
        if str(path) == first.path:
            entered.set()
            assert release.wait(5)
        return inspect(path)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", held)
    window._start_async_deploy(first.mod_id, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window._on_overview_mod_toggle(second.mod_id)
        window._on_overview_mod_toggle(second.mod_id)
        assert window.feedback.get(second.mod_id) == "pending"
        assert not mm.get_mod(second.mod_id).is_enabled
    finally:
        release.set()
        qtbot.waitUntil(
            lambda: (
                not window._deploy_workers
                and not window._enable_publication.busy
                and not getattr(window, "_deferred_enable_ids", {})
            ),
            timeout=15000,
        )
    assert window.feedback[second.mod_id] == "success"
    assert not mm.get_mod(first.mod_id).is_enabled and mm.get_mod(second.mod_id).is_enabled
    assert set(mm.preset_manager.get_current("g1").enabled_mod_ids) == {second.mod_id}
    assert not mm._library_protection.snapshot()[1]


def test_close_cancels_enable_click_waiting_for_configuration(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    from src.core import mod_variant_manager
    from tests.core.test_named_variant_coordination import add_mod

    mm, first = env["mm"], env["mod"]
    second = add_mod(env, "Next", "[Constants]\nglobal $bench = 10\n")
    window = _real_deploy_window(env, qtbot)
    entered, release = Event(), Event()
    inspect = mod_variant_manager.inspect_effective_inis

    def held(path):
        entered.set()
        assert release.wait(5)
        return inspect(path)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", held)
    window._start_async_deploy(first.mod_id, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window._on_overview_mod_toggle(second.mod_id)
        assert window.feedback[second.mod_id] == "pending"
        assert not window._stop_async_deploys()
        assert window.feedback[second.mod_id] == "failed"
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert not mm.get_mod(second.mod_id).is_enabled
    assert not window._deferred_enable_ids


@pytest.mark.parametrize("operation", ["activation", "plugin"])
def test_deferred_enable_resumes_after_an_intervening_environment_operation(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
    operation,
):
    from src.core import mod_variant_manager
    from tests.core.test_named_variant_coordination import add_mod

    mm, first = env["mm"], env["mod"]
    second = add_mod(env, "Next", "[Constants]\nglobal $bench = 10\n")
    window = _real_deploy_window(env, qtbot)
    window._plugin_operations = SimpleNamespace(busy=False)
    blocking = window._deployment_activation if operation == "activation" else window._plugin_operations
    window._resolve_character_conflict = lambda mod, defer: (True, [])
    entered, release = Event(), Event()
    inspect = mod_variant_manager.inspect_effective_inis

    def held(path):
        if str(path) == first.path:
            entered.set()
            assert release.wait(5)
        return inspect(path)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", held)
    window._start_async_deploy(first.mod_id, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        window._on_overview_mod_toggle(second.mod_id)
        blocking.busy = True
        with qtbot.waitSignal(window._enable_publication.idle, timeout=15000):
            release.set()
        qtbot.wait(1)  # 其他操作仍占有执行门闩时，清空零延迟重试。
        window._drain_deferred_enables()
        assert not mm.get_mod(second.mod_id).is_enabled
        blocking.busy = False
        if operation == "plugin":
            window._on_plugin_activity_changed(False)
        else:
            window._resume_deferred_library_environment()
        qtbot.waitUntil(lambda: window.feedback.get(second.mod_id) == "success", timeout=5000)
        assert mm.get_mod(second.mod_id).is_enabled
    finally:
        release.set()
        blocking.busy = False
        window._deferred_enable_ids.clear()
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)


@pytest.mark.parametrize("entry", ["deferred", "direct"])
def test_deferred_enable_waits_for_prior_conflict_confirmation(env, qtbot, native_qt_teardown, entry):
    from tests.core.test_named_variant_coordination import add_mod

    mm, first = env["mm"], env["mod"]
    second = add_mod(env, "Next", "[Constants]\nglobal $bench = 10\n")
    window = _real_deploy_window(env, qtbot)
    queued = (first, second) if entry == "deferred" else (second,)
    window._deferred_enable_ids = {mod.mod_id: mm.current_game_id for mod in queued}
    depths, active = [], []

    def confirm(mod, defer):
        active.append(mod.mod_id)
        depths.append(len(active))
        # 模拟消息框的嵌套事件循环，排队的空闲通知不得重复打开确认框。
        loop = QEventLoop()
        QTimer.singleShot(0, window._drain_deferred_enables)
        QTimer.singleShot(0, loop.quit)
        loop.exec()
        active.pop()
        return True, []

    window._resolve_character_conflict = confirm
    if entry == "direct":
        window._on_overview_mod_toggle(first.mod_id)
    else:
        window._on_enable_publication_idle()
    qtbot.waitUntil(
        lambda: (
            len(depths) == 2
            and not window._deferred_enable_ids
            and not window._deploy_workers
            and not window._enable_publication.busy
        ),
        timeout=15000,
    )
    assert depths == [1, 1], "queued conflict decisions must not overlap"
    assert mm.get_mod(first.mod_id).is_enabled and mm.get_mod(second.mod_id).is_enabled


def test_main_window_publishes_real_deployment_without_owner_declaration_io(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    from src.core import mod_variant_manager

    window = _real_deploy_window(env, qtbot)
    mm, mod = env["mm"], env["mod"]
    entered, release = Event(), Event()
    owner = get_ident()
    original = mod_variant_manager.inspect_effective_inis

    def delayed(*args, **kwargs):
        assert get_ident() != owner
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", delayed)
    window._start_async_deploy(mod.mod_id, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        assert window.feedback[mod.mod_id] == "pending"
        assert mod.mod_id in mm._library_protection.snapshot()[1]
        assert not mm.get_mod(mod.mod_id).is_enabled
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert window.feedback[mod.mod_id] == "success"
    assert mm.get_mod(mod.mod_id).is_enabled
    assert not mm._library_protection.snapshot()[1]


def test_main_window_close_waits_when_finished_archive_has_not_entered_publication(env, qtbot, native_qt_teardown):
    window = _real_deploy_window(env, qtbot)
    mod = env["mod"]
    window._start_async_deploy(mod.mod_id, [])
    worker = window._deploy_workers[mod.mod_id]
    assert worker.wait(5000)
    assert worker.result[0]
    assert not window._enable_publication.busy
    try:
        closed = window._stop_async_deploys(timeout_ms=5000)
        assert not closed, "new configuration IO must finish before services and INI sync shut down"
        assert window._enable_publication_close_pending
    finally:
        qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert env["mm"].get_mod(mod.mod_id).is_enabled
    assert not env["mm"]._library_protection.snapshot()[1]


def test_main_window_shared_namespace_failure_cleans_deployment(env, qtbot, monkeypatch, native_qt_teardown):
    from PySide6.QtWidgets import QMessageBox

    from tests.core.test_named_variant_coordination import add_mod

    mm = env["mm"]
    ini = "namespace = shared\n[Constants]\nglobal persist $choice = 0\n"
    mods = [add_mod(env, name, ini) for name in ("First", "Second")]
    window = _real_deploy_window(env, qtbot)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    for mod in mods:
        window._start_async_deploy(mod.mod_id, [])
    qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert sorted(window.feedback.values()) == ["failed", "success"]
    assert sum(mm.get_mod(mod.mod_id).is_enabled for mod in mods) == 1
    failed = next(mod for mod in mods if not mm.get_mod(mod.mod_id).is_enabled)
    assert not (env["migoto"] / "Mods" / failed.folder_name).exists()
    assert not mm._library_protection.snapshot()[1]
    assert len(warnings) == 1


def test_main_window_keeps_failed_deployment_reserved_until_background_cleanup(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    from PySide6.QtWidgets import QMessageBox

    from tests.core.test_named_variant_coordination import add_mod

    mm = env["mm"]
    target = add_mod(
        env, "Broken", "namespace = shared\n[Constants]\nglobal persist $choice = 0\nglobal persist $choice = 1\n"
    )
    window = _real_deploy_window(env, qtbot)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    entered, release = Event(), Event()
    original = mm._storage.remove_deploy_path

    def delayed(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(mm._storage, "remove_deploy_path", delayed)
    window._start_async_deploy(target.mod_id, [])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        assert target.mod_id not in window._deploy_workers
        assert target.mod_id in mm._library_protection.snapshot()[1]
        from src.core.watcher import FileChange

        root = env["migoto"] / "Mods"
        active = root / target.folder_name
        publication = mm.apply_local_deployment_events(
            [FileChange(str(root), 1, "created", str(active), is_directory=True)]
        )
        assert not publication.changed_ids
        assert not mm.get_mod(target.mod_id).is_enabled
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._enable_publication.busy, timeout=15000)
    assert not mm._library_protection.snapshot()[1]


def test_main_window_close_preserves_finished_workers_conflict_ids(env, qtbot, native_qt_teardown):
    from tests.core.test_named_variant_coordination import add_mod

    mm, previous = env["mm"], env["mod"]
    target = add_mod(env, "Second", "[Constants]\nglobal persist $other = 0\n")
    assert mm.toggle_mod(previous.mod_id).is_enabled
    window = _real_deploy_window(env, qtbot)
    window._start_async_deploy(target.mod_id, [previous.mod_id])
    worker = window._deploy_workers[target.mod_id]
    assert worker.wait(5000)
    assert worker.result[0]
    assert not window._stop_async_deploys(timeout_ms=5000)
    qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert mm.get_mod(target.mod_id).is_enabled
    assert not mm.get_mod(previous.mod_id).is_enabled
    assert not mm._library_protection.snapshot()[1]


def test_main_window_blocks_game_switch_until_failed_enable_rollback_finishes(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    from PySide6.QtWidgets import QMessageBox

    from tests.core.test_named_variant_coordination import add_mod

    mm, previous = env["mm"], env["mod"]
    target = add_mod(
        env, "Broken", "namespace = shared\n[Constants]\nglobal persist $choice = 0\nglobal persist $choice = 1\n"
    )
    assert mm.toggle_mod(previous.mod_id).is_enabled
    store = mm._game_service._store
    games = store.get_games()
    games.append(
        dict(games[0], id="g2", name="Second game", name_en="g2", resource_dir="g2", mods_path="", migoto_path="")
    )
    store.set_games(games)
    mm._game_service.reload_games()
    window = _real_deploy_window(env, qtbot)
    window._game_store = store
    # 隔离导航页，保留真实游戏启用、管理器和配置任务。
    for name in (
        "_apply_migoto_for_current_game",
        "_start_file_watcher",
        "_schedule_activation_reconcile",
        "_load_characters",
        "_refresh_preset_combo",
        "_apply_nav_gating",
        "_resume_deferred_library_environment",
    ):
        monkeypatch.setattr(window, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    entered, release = Event(), Event()
    original = mm._storage.remove_deploy_path

    def delayed(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(mm._storage, "remove_deploy_path", delayed)
    window._start_async_deploy(target.mod_id, [previous.mod_id])
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        assert not window._deploy_workers and window._enable_publication.busy
        window._game_service.current_game_id = "g2"
        window._on_game_changed("g2")
        during = (mm.current_game_id, window._game_service.current_game_id, window._game_page.isEnabled())
    finally:
        # 断言失败时仍须完成原游戏的回滚。
        if mm.current_game_id != "g1":
            mm.set_game("g1")
        window._game_service.current_game_id = "g1"
        release.set()
        qtbot.waitUntil(lambda: not window._enable_publication.busy, timeout=15000)
    assert during == ("g1", "g1", False)
    qtbot.waitUntil(window._game_page.isEnabled, timeout=1000)
    assert mm.get_mod(previous.mod_id).is_enabled
    assert not mm.get_mod(target.mod_id).is_enabled
    assert not mm._library_protection.snapshot()[1]


def test_main_window_owner_database_error_rolls_back_on_production_worker(
    env,
    qtbot,
    monkeypatch,
    native_qt_teardown,
):
    import sqlite3

    from PySide6.QtWidgets import QMessageBox

    mm, mod = env["mm"], env["mod"]
    current = mm.preset_manager.get_current("g1")
    before_record = mm._index.get_deployment(mod.mod_id)
    before_members = dict(current.character_mods)
    refresh = mm.preset_manager.refresh_references

    def fail_after_refresh(*args, **kwargs):
        refresh(*args, **kwargs)
        raise sqlite3.OperationalError("injected owner SQL failure")

    monkeypatch.setattr(mm.preset_manager, "refresh_references", fail_after_refresh)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    window = _real_deploy_window(env, qtbot)
    owner = get_ident()
    removed_on = []
    remove = mm._storage.remove_deploy_path

    def remove_in_worker(path):
        removed_on.append(get_ident())
        assert get_ident() != owner
        return remove(path)

    monkeypatch.setattr(mm._storage, "remove_deploy_path", remove_in_worker)
    window._start_async_deploy(mod.mod_id, [])
    qtbot.waitUntil(lambda: not window._deploy_workers and not window._enable_publication.busy, timeout=15000)
    assert removed_on
    assert window.feedback[mod.mod_id] == "failed"
    assert mm._index.get_deployment(mod.mod_id) == before_record
    assert not mm.get_mod(mod.mod_id).is_enabled
    assert current.character_mods == before_members
    assert not (env["migoto"] / "Mods" / mod.folder_name).exists()
    assert not mm._library_protection.snapshot()[1]
