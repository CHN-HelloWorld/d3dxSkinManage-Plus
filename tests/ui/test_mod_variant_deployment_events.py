"""验证部署启用状态通过核心事件及时更新当前外观变体面板。"""

from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.models.mod import ModStatus
from tests.ui.test_mod_variant_controller import harness
from tests.ui.test_mod_variant_panel import row
from tests.ui.test_mod_variant_wiring import host

pytestmark = pytest.mark.usefixtures("temp_data_dir", "native_qt_teardown")


def _set_enabled(service, enabled: bool) -> None:
    """同步测试服务的 Mod 与变体状态，模拟核心完成部署后的可读结果。"""
    service.mod.status = ModStatus.ENABLED if enabled else ModStatus.DISABLED
    service.snapshot.update(enabled=enabled, can_save=enabled)


@pytest.fixture
def variant_context(qtbot):
    """创建真实变体面板及可选主窗口信号宿主，结束时清理工作线程和控件。"""
    contexts = []

    def create(*, enabled: bool, through_host: bool = False):
        """建立指定初始启用状态的当前选择，并等待首轮后台读取完成。"""
        if through_host:
            window = host(qtbot)
            service = window.service
            _set_enabled(service, enabled)
            page, controller = window._overview_page, window._variant_controller
            page._selected_mod = service.mod
            page._selected_ids = {service.mod.mod_id}
            page._show_mod_panel(service.mod)
            page._detail_tabs.setCurrentIndex(2)
            dispatch = window._on_core_event
        else:
            page, controller, service, _ = harness(qtbot, enabled=enabled)
            window, dispatch = page, controller.handle_core_event
        context = SimpleNamespace(page=page, controller=controller, service=service, window=window, dispatch=dispatch)
        contexts.append(context)
        qtbot.waitUntil(lambda: not controller.running)
        return context

    yield create
    for context in reversed(contexts):
        context.controller.close()
        qtbot.waitUntil(lambda: not context.controller.running)
        context.page.close()
        context.window.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("through_host", [False, True])
def test_deployment_event_refreshes_selected_variant_without_reselection(
    qtbot, monkeypatch, variant_context, enabled, through_host
):
    """单个部署事件经过控制器或主窗口线程桥后更新保存能力与使用标记，不重载其他详情。"""
    context = variant_context(enabled=not enabled, through_host=through_host)
    page, controller, service = context.page, context.controller, context.service
    panel = page._variant_panel

    def unrelated_refresh(*args, **kwargs):
        """将意外的按键或图片重载直接报告为测试失败。"""
        pytest.fail("部署状态事件不应重载当前图片或按键面板")

    monkeypatch.setattr(page, "_load_mod_panel_preview", unrelated_refresh)
    monkeypatch.setattr(page, "_build_keys_panel", unrelated_refresh)
    _set_enabled(service, enabled)
    event = "mod_enabled" if enabled else "mod_disabled"

    def dispatch() -> None:
        """从核心事件入口发布已提交的部署状态变化。"""
        context.dispatch(event, mod_id=service.mod.mod_id, game_id="game_a")

    if through_host:
        worker = Thread(target=dispatch)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
    else:
        dispatch()
    qtbot.waitUntil(lambda: not controller.running and panel._state.get("enabled") is enabled, timeout=1000)

    assert panel.save_button.isEnabled() is enabled
    assert row(panel, "first").using_label.text() == ("正在使用" if enabled else "下次使用")
    assert page._selected_mod is service.mod
    assert page._detail_tabs.currentIndex() == 2


@pytest.mark.parametrize("event", ["mod_enabled", "mod_disabled"])
@pytest.mark.parametrize("mod_id, game_id", [("other-mod", "game_a"), ("mod-one", "other-game")])
def test_foreign_deployment_event_does_not_refresh_selected_variant(
    qtbot, monkeypatch, variant_context, event, mod_id, game_id
):
    """其他 Mod 或其他游戏的部署事件不触发当前变体读取，也不改动当前保存能力。"""
    context = variant_context(enabled=True)
    service, controller = context.service, context.controller
    prepared = []
    prepare_task = service.prepare_task

    def record_prepare(*args, **kwargs):
        """记录真实任务准备，保留后台读取与状态发布行为。"""
        prepared.append(args[0])
        return prepare_task(*args, **kwargs)

    monkeypatch.setattr(service, "prepare_task", record_prepare)
    _set_enabled(service, False)
    controller.handle_core_event(event, mod_id=mod_id, game_id=game_id)
    qtbot.waitUntil(lambda: not controller.running)

    panel = context.page._variant_panel
    assert prepared == []
    assert panel.save_button.isEnabled()
    assert row(panel, "first").using_label.text() == "正在使用"


@pytest.mark.parametrize("enabled", [False, True])
def test_deployment_events_during_read_coalesce_into_latest_state(qtbot, monkeypatch, variant_context, enabled):
    """部署变化发生于旧状态读取期间时，合并后续读取并以最新启用状态更新面板。"""
    context = variant_context(enabled=not enabled)
    service, controller = context.service, context.controller
    started, release = Event(), Event()
    reads = []
    get_state = service.get_state

    def delayed_state(mod_id):
        """冻结首轮读取结果直到部署事件到达，后续读取返回实时状态。"""
        result = get_state(mod_id)
        reads.append(result["enabled"])
        if len(reads) == 1:
            started.set()
            if not release.wait(3):
                raise TimeoutError("测试未释放变体状态读取")
        return result

    monkeypatch.setattr(service, "get_state", delayed_state)
    controller.refresh(service.mod.mod_id)
    try:
        qtbot.waitUntil(started.is_set)
        _set_enabled(service, enabled)
        event = "mod_enabled" if enabled else "mod_disabled"
        for _ in range(3):
            controller.handle_core_event(event, mod_id=service.mod.mod_id, game_id="game_a")
    finally:
        release.set()
        qtbot.waitUntil(lambda: not controller.running)

    panel = context.page._variant_panel
    assert panel.save_button.isEnabled() is enabled
    assert row(panel, "first").using_label.text() == ("正在使用" if enabled else "下次使用")
    assert reads == [not enabled, enabled]
