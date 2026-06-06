from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QMainWindow

from src.ui.main_window import MainWindow
from src.ui.pages.game import GamePage
from src.ui.pages.overview import OverviewPage
from tests.ui.test_mod_variant_controller import VariantService

pytestmark = pytest.mark.usefixtures("temp_data_dir", "native_qt_teardown")


class _Service(VariantService):
    def __init__(self):
        super().__init__()
        self.captures = []
        self.collect_failure = None

    def set_change_notify(self, callback):
        self.change_notify = callback

    def collect(self, **kwargs):
        self.captures.append((kwargs, QThread.currentThread()))
        if self.collect_failure is not None:
            raise self.collect_failure
        return None


class _VariantHost(MainWindow):
    """使用实际信号连接和处理方法，隔离无关服务。"""

    def __init__(self, manager=None):
        QMainWindow.__init__(self)
        self.service = manager.variants if manager is not None else _Service()
        self._mod_manager = manager or SimpleNamespace(variants=self.service, current_game_id="game_a")
        self._overview_page = OverviewPage()
        self.setCentralWidget(self._overview_page)
        self._preset_apply_worker = None
        self._plugin_operations = SimpleNamespace(busy=False)
        self._deployment_activation = SimpleNamespace(busy=False, closing=False)
        self._deploy_environment_dirty = False
        self._pending_gb_installs = []
        self._watcher_changed_paths = set()
        self._game_page = GamePage(self)
        self.messages = []
        self.enabled = []
        self._connect_variant_controller()

    def _on_overview_mod_toggle(self, mod_id):
        self.enabled.append(mod_id)

    def _ensure_toast(self):
        return SimpleNamespace(done=lambda message, **kwargs: self.messages.append((message, kwargs)))

    def closeEvent(self, event):
        QMainWindow.closeEvent(self, event)


def host(qtbot):
    window = _VariantHost()
    qtbot.addWidget(window)
    window.resize(1100, 700)
    window.show()
    return window


def event(sequence):
    return dict(
        game_id="game_a",
        deployment_generation="deployment-session",
        capture_generation=sequence,
        mods=({"mod_id": "mod-one", "deployment_generation": "same-mod", "capture_generation": sequence},),
    )


def test_background_file_event_reaches_collect_on_owner_thread_unchanged(qtbot):
    window = host(qtbot)
    frozen = event(1)
    worker = Thread(target=lambda: window.service.change_notify(frozen))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: len(window.service.captures) == 1)
    kwargs, thread = window.service.captures[0]
    assert kwargs == {"trigger": "user_ini_watch", "only_if_changed": True, "event": frozen}
    assert kwargs["event"] is frozen
    assert thread is window.thread()


@pytest.mark.parametrize("busy_kind", ["plugin", "deployment"])
def test_busy_retries_keep_latest_event_and_collect_it_only_once(qtbot, busy_kind):
    window = host(qtbot)
    owner = window._plugin_operations if busy_kind == "plugin" else window._deployment_activation
    owner.busy = True
    window.service.change_notify(event(1))
    latest = event(2)
    window.service.change_notify(latest)
    assert not window.service.captures
    owner.busy = False
    # 两个完成路径可能在同一轮事件循环结束。
    window._on_plugin_activity_changed(False)
    window._resume_deferred_library_environment()
    qtbot.waitUntil(lambda: bool(window.service.captures))
    qtbot.wait(15)
    assert len(window.service.captures) == 1
    assert window.service.captures[0][0]["event"] is latest


def test_variant_completion_resumes_deferred_environment_and_installs(qtbot, monkeypatch):
    window = host(qtbot)
    page = window._overview_page
    page._selected_mod = window.service.mod
    page._show_mod_panel(window.service.mod)
    controller = window._variant_controller
    qtbot.waitUntil(lambda: not controller.running)
    released = Event()
    apply = window.service.apply_variant

    def held_apply(*args):
        released.wait(2)
        return apply(*args)

    resumed = []
    monkeypatch.setattr(window.service, "apply_variant", held_apply)
    monkeypatch.setattr(window, "_apply_migoto_for_current_game", lambda: resumed.append("environment"))

    def install():
        window._pending_gb_installs.clear()
        resumed.append("install")

    monkeypatch.setattr(window, "_drain_gb_installs", install)
    controller.apply("mod-one", "other")
    try:
        assert not window._game_page.isEnabled()
        window._deploy_environment_dirty = True
        window._pending_gb_installs.append("queued-install")
        window.service.change_notify(event(1))
        assert not resumed and not window.service.captures
    finally:
        released.set()
    qtbot.waitUntil(lambda: not controller.running)
    qtbot.waitUntil(lambda: len(resumed) == 2)
    assert window._game_page.isEnabled()
    assert not window._pending_gb_installs
    assert not window._deploy_environment_dirty
    assert len(window.service.captures) == 1


def test_game_environment_refresh_is_deferred_during_variant_write(qtbot):
    window = host(qtbot)
    window._variant_controller = SimpleNamespace(busy=True)
    # 最小宿主不提供存储或加载界面，受保护调用须在访问这些组件或偏好锁前延后执行。
    window._preset_apply_worker = None
    window._deploy_workers = {}
    window._deploy_active = set()
    MainWindow._apply_migoto_for_current_game(window)
    assert window._deploy_environment_dirty


def test_core_event_refreshes_selected_variant_without_preview_reload(qtbot, monkeypatch):
    window = host(qtbot)
    page = window._overview_page
    page._selected_mod = window.service.mod
    page._show_mod_panel(window.service.mod)
    page._detail_tabs.setCurrentIndex(2)

    def unrelated(*args, **kwargs):
        pytest.fail("A variant event reloaded an unrelated preview surface")

    monkeypatch.setattr(page, "_load_mod_panel_preview", unrelated)
    monkeypatch.setattr(page, "_build_keys_panel", unrelated)
    window.service.snapshot["selected_variant_id"] = "other"
    worker = Thread(target=lambda: window._on_core_event("variant_state_changed", game_id="game_a", mod_id="mod-one"))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: "夜色" in page._panel_variant_label._full_text)
    assert page._detail_tabs.currentIndex() == 2


def test_missing_references_have_one_combined_notice_for_current_game(qtbot):
    window = host(qtbot)
    window._on_core_event("variant_references_missing", game_id="foreign", names=["外部组合"])
    window._on_core_event("variant_references_missing", game_id="game_a", names=["日常", "摄影", "日常"])
    qtbot.waitUntil(lambda: bool(window.messages))
    assert len(window.messages) == 1
    message = window.messages[0][0]
    assert "日常" in message and "摄影" in message
    assert message.count("日常") == 1
    assert "Mod 默认" in message


def test_restore_notice_only_confirms_write_and_requests_next_refresh(qtbot):
    window = host(qtbot)
    window._on_core_event("variant_restored", game_id="game_a", mod_id="mod-one", applied=1)
    qtbot.waitUntil(lambda: bool(window.messages))
    message = window.messages[0][0]
    assert "用户偏好已写入" in message
    assert "请退出后重新应用，再启动" in message
    assert "F10 生效" not in message
