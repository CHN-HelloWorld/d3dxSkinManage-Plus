"""验证后台宿主事件在激活插件前先转交主线程。"""

from concurrent.futures import ThreadPoolExecutor
from threading import get_ident

from tests.plugins.test_activation import ENTRY, lazy_plugin, prepare
from tests.plugins.test_runtime import env as env


def test_worker_host_event_activates_and_dispatches_on_owner_thread(env, qtbot):
    from src.ui.plugin_tasks import PluginTaskDispatcher

    manager, directory, _ = env
    dispatcher = PluginTaskDispatcher()
    plugin = lazy_plugin(directory, activationEvents=["onEvent:game_changed"])
    source = "from threading import get_ident\n" + ENTRY.replace(
        'self.calls.append(payload["game_id"])', 'self.calls.append((get_ident(), payload["game_id"]))'
    )
    (plugin / "entry.py").write_text(source, encoding="utf-8")
    manager.set_event_dispatcher(dispatcher.post)
    prepare(manager, "test.lazy")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(manager.on_host_event, "game_changed", game_id="game-a").result(timeout=3)
        assert manager.get("test.lazy").instance is None
        qtbot.waitUntil(lambda: manager.get("test.lazy").instance is not None)
        assert manager.get("test.lazy").instance.calls == [(get_ident(), "game-a")]
    finally:
        manager.shutdown()
        dispatcher.close()
        dispatcher.deleteLater()
