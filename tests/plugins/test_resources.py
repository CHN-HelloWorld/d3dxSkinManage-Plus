"""验证插件资源的独立注销、逆序清理和线程边界。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.errors import PluginError


def test_disposable_releases_callback_exactly_once():
    from src.plugin_sdk.disposable import Disposable

    calls = []
    handle = Disposable(lambda: calls.append("released"))
    handle.dispose()
    handle.dispose()
    assert calls == ["released"]
    assert handle.disposed


def test_store_cleans_up_in_reverse_order_despite_failure():
    from src.plugin_sdk.disposable import Disposable, DisposableStore

    calls = []
    resources = DisposableStore()
    resources.add(lambda: calls.append("first"))

    def fail():
        """模拟资源释放异常，后续资源仍必须清理。"""
        calls.append("failed")
        raise RuntimeError("cleanup failed")

    resources.add(Disposable(fail))
    resources.add(lambda: calls.append("last"))
    resources.dispose()
    resources.dispose()
    late = resources.add(lambda: calls.append("late"))
    assert calls == ["last", "failed", "first", "late"]
    assert late.disposed


def test_nested_resource_scope_can_be_closed_independently():
    from src.plugin_sdk.disposable import DisposableStore

    calls = []
    root = DisposableStore()
    child = root.add(DisposableStore())
    child.add(lambda: calls.append("child"))
    root.add(lambda: calls.append("root"))
    child.dispose()
    assert calls == ["child"]
    root.dispose()
    assert calls == ["child", "root"]


def test_context_resource_rejects_use_on_a_worker_thread():
    resource = ContextResource()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resource._require_open)
        with pytest.raises(PluginError):
            future.result(timeout=3)
    resource._require_open()
    resource._close()
    with pytest.raises(PluginError):
        resource._require_open()


def test_old_view_handle_cannot_remove_a_reregistered_factory():
    from src.plugins.runtime.views import ViewRegistry

    registry = ViewRegistry()

    def factory():
        """返回独立的视图内容用于验证注册身份。"""
        return object()

    first = registry.register("test.view", "panel", factory)
    second = registry.register("test.view", "panel", factory)
    first.dispose()
    assert registry.get("test.view", "panel") is factory
    second.dispose()
    assert registry.get("test.view", "panel") is None
