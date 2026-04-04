import asyncio
import threading
from concurrent.futures import Future

import httpx
import pytest

from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbModel
from src.services.gamebanana.update_checker import ModUpdateChecker
from tests.services.gamebanana.test_update_checker_async import _meta
from tests.services.gamebanana.test_update_files_batching import _file, _observe


@pytest.mark.parametrize("store_first", [False, True])
async def test_cancel_does_not_cancel_the_stores_shared_changelog(tmp_path, respx_mock, qapp, store_first):
    client = GameBananaClient(request_interval_ms=0, cache=GbApiCache(persist_dir=tmp_path / "cache"))
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(200, json=[{"_idRow": 1, "_aFiles": [_file()]}])
        entered.set()
        await release.wait()
        return httpx.Response(200, json=[])

    def submit(factory):
        task = asyncio.create_task(factory())
        tasks.append(task)
        return task

    route = respx_mock.get(url__regex=r"https://gamebanana.com/apiv11/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client, submit=submit)
    events = _observe(checker)
    store_task = None
    try:
        if store_first:
            store_task = asyncio.create_task(client.get_updates(GbModel.MOD, 1))
            await asyncio.wait_for(entered.wait(), 1)
        assert checker.start([("local", _meta(1))], {}, force=True)
        await asyncio.wait_for(entered.wait(), 1)
        if not store_first:
            store_task = asyncio.create_task(client.get_updates(GbModel.MOD, 1))
        # 等待两个使用者加入缓存的共享请求。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert checker.cancel()
        assert not store_task.done()
        release.set()
        assert await asyncio.wait_for(store_task, 1) == []
        await asyncio.gather(*tasks, return_exceptions=True)
        assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True}]
        assert events["found"] == {} and events["up_to_date"] == []
        assert route.call_count == 2, "one batch and one shared changelog are sufficient"
    finally:
        release.set()
        if store_task is not None:
            await asyncio.gather(store_task, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()


async def test_cancel_before_submitted_factory_starts_never_sends_a_request(qapp):
    factories = []
    future = Future()

    def submit(factory):
        factories.append(factory)
        return future

    class Client:
        async def bulk_update_files(self, *args, **kwargs):
            pytest.fail("a cancelled queued check must not reach the network")

    checker = ModUpdateChecker(Client(), submit=submit)
    events = _observe(checker)
    checker.start([("local", _meta(1))], {}, force=True)
    assert checker.cancel()
    await asyncio.gather(factories[0](), return_exceptions=True)
    assert future.cancelled()
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True}]
    assert events["progress"] == [] and events["up_to_date"] == []


def test_queued_old_events_cannot_publish_results_or_unlock_a_new_round(qtbot):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    second_entered = threading.Event()
    release = None
    submitted = []

    class Client:
        async def bulk_update_files(self, ids, *, force):
            nonlocal release
            if ids == [2]:
                release = asyncio.Event()
                second_entered.set()
                await release.wait()
            return {mid: [] for mid in ids}

    def submit(factory):
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        submitted.append(future)
        return future

    checker = ModUpdateChecker(Client(), submit=submit)
    events = _observe(checker)
    try:
        checker.start([("old", _meta(1))], {}, force=True)
        # 等待后台任务而不分发 Qt 事件，使其通知保持排队。
        submitted[0].result(timeout=2)
        assert checker.cancel()
        assert checker.start([("new", _meta(2))], {}, force=True)
        qtbot.waitUntil(second_entered.is_set, timeout=2000)
        assert not checker.start([("duplicate", _meta(3))], {}, force=True)
        assert events["up_to_date"] == [] and events["progress"] == []
        assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True}]
        with qtbot.waitSignal(checker.finished, timeout=2000):
            loop.call_soon_threadsafe(release.set)
        assert events["up_to_date"] == ["new"]
        assert events["progress"] == [(0, 1), (1, 1)]
        assert events["finished"][-1] == {"found": 0, "up_to_date": 1, "failed": 0}
        assert len(events["finished"]) == 2
    finally:
        if release is not None:
            loop.call_soon_threadsafe(release.set)
        for future in submitted:
            if not future.done():
                future.cancel()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_submission_failure_reports_retryable_local_failures_once(qapp):
    def submit(factory):
        raise RuntimeError("loop unavailable")

    checker = ModUpdateChecker(object(), submit=submit)
    events = _observe(checker)
    assert checker.start([("one", _meta(1)), ("two", _meta(2))], {}, force=True)
    assert events["failed"] == {"one": "loop unavailable", "two": "loop unavailable"}
    assert events["progress"][-1] == (2, 2)
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 2}]
    assert not checker.cancel()


def test_fallback_thread_cancels_awaited_work_and_allows_immediate_restart(qtbot):
    entered, cancelled = threading.Event(), threading.Event()
    calls = 0

    class Client:
        async def bulk_update_files(self, ids, *, force):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return {mid: [] for mid in ids}

    checker = ModUpdateChecker(Client())
    events = _observe(checker)
    checker.start([("old", _meta(1))], {}, force=True)
    first_thread = checker._thread
    qtbot.waitUntil(entered.is_set, timeout=2000)
    assert checker.cancel()
    with qtbot.waitSignal(checker.finished, timeout=2000):
        assert checker.start([("new", _meta(2))], {}, force=True)
    qtbot.waitUntil(cancelled.is_set, timeout=2000)
    first_thread.join(timeout=2)
    checker._thread.join(timeout=2)
    assert events["up_to_date"] == ["new"]
    assert events["finished"] == [
        {"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True},
        {"found": 0, "up_to_date": 1, "failed": 0},
    ]


async def test_finished_receiver_can_restart_without_old_callback_clearing_busy(qapp):
    entered = asyncio.Event()
    tasks = []

    class Client:
        async def bulk_update_files(self, ids, *, force):
            if ids == [2]:
                entered.set()
                await asyncio.Future()
            return {mid: [] for mid in ids}

    def submit(factory):
        task = asyncio.create_task(factory())
        tasks.append(task)
        return task

    checker = ModUpdateChecker(Client(), submit=submit)
    events = _observe(checker)

    def restart(counts):
        if len(events["finished"]) == 1:
            assert checker.start([("second", _meta(2))], {}, force=True)

    checker.finished.connect(restart)
    checker.start([("first", _meta(1))], {}, force=True)
    try:
        await tasks[0]
        await asyncio.wait_for(entered.wait(), 1)
        assert not checker.start([("duplicate", _meta(3))], {}, force=True)
        assert checker.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert events["up_to_date"] == ["first"]
        assert len(events["finished"]) == 2
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_fallback_thread_start_failure_finishes_local_failures_once(qapp, monkeypatch):
    def cannot_start(thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    checker = ModUpdateChecker(object())
    events = _observe(checker)
    assert checker.start([("one", _meta(1)), ("two", _meta(2))], {}, force=True)
    assert events["failed"] == {"one": "thread unavailable", "two": "thread unavailable"}
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 2}]
    assert not checker.cancel()
