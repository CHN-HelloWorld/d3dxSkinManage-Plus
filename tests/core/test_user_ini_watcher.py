"""验证用户 INI 监控的路径筛选、防抖和线程边界。

通过可控定时器及 notify_path 注入事件，避免操作系统监控调度影响断言。
"""

from __future__ import annotations

from src.core.user_ini_watcher import UserIniWatcher


class _FakeTimer:
    """threading.Timer 的确定性替身：不起线程，由测试显式 fire。"""

    created: list[_FakeTimer] = []

    def __init__(self, interval, fn) -> None:
        self.interval = interval
        self.fn = fn
        self.started = False
        self.cancelled = False
        self.daemon = False
        _FakeTimer.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


def _watcher(tmp_path, calls, *, debounce=0.0):
    _FakeTimer.created = []
    w = UserIniWatcher(
        lambda: calls.append(1),
        debounce_seconds=debounce,
        enable_watch=False,  # 不碰真 watchdog
        timer_factory=_FakeTimer,
    )
    w.watch(tmp_path / "d3dx_user.ini")  # 目录存在、文件可以还不存在
    return w


def test_watch_accepts_missing_target_file(tmp_path):
    w = _watcher(tmp_path, [])
    assert w.target == tmp_path / "d3dx_user.ini"
    assert not (tmp_path / "d3dx_user.ini").exists()


def test_target_file_event_triggers_single_callback(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    w.notify_path(tmp_path / "d3dx_user.ini")
    assert len(_FakeTimer.created) == 1
    _FakeTimer.created[0].fire()
    assert calls == [1]


def test_filename_match_is_case_insensitive(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    w.notify_path(tmp_path / "D3DX_USER.INI")
    assert len(_FakeTimer.created) == 1
    _FakeTimer.created[0].fire()
    assert calls == [1]


def test_other_files_in_migoto_root_are_ignored(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    for name in ("d3dx_log.txt", "d3dx.ini", "ShaderFixes.txt"):
        w.notify_path(tmp_path / name)
    assert _FakeTimer.created == [] and calls == []


def test_burst_of_events_is_debounced_into_one_callback(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    for _ in range(5):
        w.notify_path(tmp_path / "d3dx_user.ini")
    assert len(_FakeTimer.created) == 1  # 始终只有一个活跃 Timer
    _FakeTimer.created[0].fire()
    assert calls == [1]


def test_callback_exception_does_not_kill_the_timer_thread(tmp_path):
    def boom():
        raise RuntimeError("boom")

    _FakeTimer.created = []
    w = UserIniWatcher(boom, debounce_seconds=0.0, enable_watch=False, timer_factory=_FakeTimer)
    w.watch(tmp_path / "d3dx_user.ini")
    w.notify_path(tmp_path / "d3dx_user.ini")
    _FakeTimer.created[0].fire()  # 不抛
    w.notify_path(tmp_path / "d3dx_user.ini")
    assert len(_FakeTimer.created) == 2  # 仍能继续调度


def test_watch_none_clears_target_and_stops_reacting(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    assert w.watch(None) is False
    assert w.target is None
    w.notify_path(tmp_path / "d3dx_user.ini")
    assert _FakeTimer.created == [] and calls == []


def test_missing_directory_keeps_target_for_later_retry(tmp_path):
    calls: list[int] = []
    _FakeTimer.created = []
    w = UserIniWatcher(lambda: calls.append(1), debounce_seconds=0.0, enable_watch=False, timer_factory=_FakeTimer)
    missing = tmp_path / "nope" / "d3dx_user.ini"
    assert w.watch(missing) is False
    assert w.target == missing


def test_stop_is_idempotent_and_silences_events(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    w.notify_path(tmp_path / "d3dx_user.ini")
    pending = _FakeTimer.created[0]
    w.stop()
    w.stop()
    assert pending.cancelled
    w.notify_path(tmp_path / "d3dx_user.ini")
    assert len(_FakeTimer.created) == 1 and calls == []


def test_watch_after_stop_is_refused(tmp_path):
    calls: list[int] = []
    w = _watcher(tmp_path, calls)
    w.stop()
    assert w.watch(tmp_path / "d3dx_user.ini") is False
