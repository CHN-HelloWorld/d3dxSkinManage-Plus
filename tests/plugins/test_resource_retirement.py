"""验证长期启用的插件不会无限保留已释放的注册句柄。"""

import gc
import weakref

from src.plugin_sdk.disposable import Disposable, DisposableStore
from src.plugin_sdk.events import MOD_ENABLED, EventSubscription
from src.plugins.runtime.eventbus import EventBus


def test_repeated_subscribe_and_dispose_keeps_only_bounded_retired_handles():
    store = DisposableStore()
    bus = EventBus()
    events = EventSubscription("test.retirement", bus, store)
    references = []
    for _ in range(1024):
        handle = events.on(MOD_ENABLED, lambda **_payload: None)
        references.append(weakref.ref(handle))
        handle.dispose()
    del handle
    gc.collect()
    assert not bus._subs
    assert sum(reference() is not None for reference in references) <= 64
    store.dispose()
    gc.collect()
    assert all(reference() is None for reference in references)


def test_pruning_preserves_live_handle_identity_and_reverse_release_order():
    store = DisposableStore()
    releases = []
    first = Disposable(lambda: releases.append("first"))
    assert store.add(first) is first
    for _ in range(256):
        store.add(lambda: None).dispose()
    last = Disposable(lambda: releases.append("last"))
    assert store.add(last) is last
    store.dispose()
    assert releases == ["last", "first"]


def test_closed_nested_stores_do_not_accumulate_in_live_parent():
    root = DisposableStore()
    references = []
    for _ in range(256):
        child = root.add(DisposableStore())
        references.append(weakref.ref(child))
        child.dispose()
    del child
    gc.collect()
    assert sum(reference() is not None for reference in references) <= 64
    root.dispose()
