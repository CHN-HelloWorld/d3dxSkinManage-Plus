from types import SimpleNamespace

from src.core.mod_manager import ModManager


class _Signal:
    def __init__(self) -> None:
        self._callback = None

    def connect(self, callback) -> None:
        self._callback = callback

    def emit(self, value) -> None:
        assert self._callback is not None
        self._callback(value)


class _ImportProgressDialog:
    def __init__(self, items, character_id, mod_manager, parent=None, *, panel=None) -> None:
        self.import_finished = _Signal()

    def start_import(self) -> None:
        pass


def test_overview_import_schedules_one_level_reload(monkeypatch):
    from src.ui import main_window as main_window_module
    from src.ui.dialogs import import_progress
    from src.ui.main_window import MainWindow

    scheduled = []
    reloaded = []
    suppressed = []
    watcher_stops = []
    reconcile_deferrals = []

    def schedule(delay, context, callback) -> None:
        scheduled.append((delay, context, callback))

    class _Harness:
        _do_import = MainWindow._do_import
        _do_import_overview = MainWindow._do_import_overview

        def __init__(self) -> None:
            self._mod_manager = object()
            self._game_service = SimpleNamespace(current_game_id="game_a")
            self._import_active = False
            self._intake_progress = object()
            self._pending_gb_installs = []
            self._watcher_debounce = SimpleNamespace(stop=lambda: watcher_stops.append(True))

        def _suppress_watcher(self) -> None:
            suppressed.append(True)

        def _reload_overview_level(self, game_id: str) -> None:
            reloaded.append(game_id)

        def _defer_source_reconcile(self) -> None:
            reconcile_deferrals.append(True)

    monkeypatch.setattr(import_progress, "ImportProgressDialog", _ImportProgressDialog)
    monkeypatch.setattr(main_window_module, "call_later", schedule)

    harness = _Harness()
    harness._do_import_overview([("source", "name")], "character-id")
    assert harness._import_active and scheduled == []
    harness._import_controller.import_finished.emit([(0, object())])

    assert not harness._import_active and harness._import_controller is None
    assert suppressed == [True]
    assert watcher_stops == [True]
    assert reconcile_deferrals == [True]
    assert len(scheduled) == 1
    assert scheduled[0][0] == 0
    assert scheduled[0][1] is harness
    scheduled[0][2]()
    assert reloaded == ["game_a"]


def test_local_events_are_published_during_automatic_refresh_suppression():
    from src.ui.main_window import MainWindow

    event = object()
    delta = SimpleNamespace(retry_after_ms=None)
    batches, published = [], []

    def apply_events(events):
        batches.append(events)
        return delta

    window = SimpleNamespace(
        _watcher_suppress_until=float("inf"),
        _watcher_changed_paths=set(),
        _watcher_events=[event],
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_source_events=apply_events, _deferred_source_events=()),
        _publish_local_library_delta=published.append,
    )
    MainWindow._refresh_from_watcher(window)
    assert batches == [[event]]
    assert published == [delta]
    assert window._watcher_events == []
    assert not window._plugin_watcher_pending


def test_main_window_does_not_commit_prepare_exception():
    from src.core.mod_intake import DuplicateIntakeError
    from src.ui.main_window import MainWindow

    calls = []

    class _Worker:
        is_cancel_requested = False

        def acknowledge(self, *, committed, error="") -> None:
            calls.append(("ack", committed, error))

    class _Service:
        def commit(self, *_args, **_kwargs):
            calls.append(("commit",))
            raise AssertionError("prepare exception must not reach commit")

    class _Harness:
        _on_intake_item_prepared = MainWindow._on_intake_item_prepared

        def __init__(self) -> None:
            self._intake_worker = _Worker()
            self._intake_service = _Service()

        def _mark_intake_item(self, index, status, text) -> None:
            # 面板明细行更新与本测试的核心不变量（异常不得进 commit）无关，
            # 但生产代码会调用它，故替身须提供，否则测的是 AttributeError 而非行为。
            calls.append(("mark", status))

    _Harness()._on_intake_item_prepared(0, DuplicateIntakeError("same hash"))

    # 核心不变量：prepare 抛异常时只能 ack(False)，绝不能触达 commit。
    assert ("commit",) not in calls
    assert calls[0] == ("ack", False, "same hash")


def test_refresh_current_uses_unified_reconcile_instead_of_sync_scan():
    from src.ui.main_window import MainWindow

    calls = []

    class _Manager:
        # 触发器是否发现新增由生产规则单点判定，替身直接借真实实现。
        trigger_discovers = staticmethod(ModManager.trigger_discovers)

        def sync_with_filesystem(self, _gid):
            raise AssertionError("F5 must go through reconcile_library")

        def scan_and_sync(self):
            raise AssertionError("F5 must not perform synchronous intake IO")

    class _GameService:
        current_game_id = "game_a"

        @staticmethod
        def has_active_game():
            return True

    class _Harness:
        _refresh_current = MainWindow._refresh_current

        def __init__(self) -> None:
            self._import_active = False
            self._preset_apply_worker = None
            self._watcher_suppress_until = 0.0
            self._game_service = _GameService()
            self._mod_manager = _Manager()

        def _reconcile_library(self, game_id, *, trigger, changed_paths=None, manual=False, publish="on-change"):
            calls.append((game_id, trigger, changed_paths, manual, publish))

    harness = _Harness()
    harness._refresh_current(manual=True)
    # F5 = 当前游戏全量浅扫（无 changed_paths）+ 无条件重绘，且只走统一对账这一条路
    assert calls == [("game_a", "manual", None, True, "always")]

    calls.clear()
    harness._refresh_current(changed_paths={"changed"})
    # watcher = 只处理本轮合并出的变化路径，且无变化不重绘
    assert calls == [("game_a", "watcher", {"changed"}, False, "on-change")]

    calls.clear()
    harness._refresh_current(after_preset_commit=True)
    # 预设提交 = 与 F5 同档的全量收敛 + 无条件重绘（启用态由预设引擎写，对账计数为 0）
    assert calls == [("game_a", "manual", None, False, "always")]
