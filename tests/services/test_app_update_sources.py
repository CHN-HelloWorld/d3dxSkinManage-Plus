"""检验实际正文测速、持续低速判定和自动换源的停止边界。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.services.app_update_download_helpers import make_session


def test_manual_source_never_adds_other_mirrors():
    from src.services.app_update.download_sources import build_download_routes

    routes = build_download_routes("https://github.com/example/app/v2.zip", "official", "http://127.0.0.1:9")
    assert [(route.url, route.proxy) for route in routes] == [
        ("https://github.com/example/app/v2.zip", "http://127.0.0.1:9"),
        ("https://github.com/example/app/v2.zip", None),
    ]


def test_auto_source_separates_proxy_and_direct_routes():
    from src.services.app_update.download_sources import build_download_routes

    url, proxy = "https://github.com/example/app/v2.zip", "http://127.0.0.1:9"
    direct = build_download_routes(url, "auto", None)
    proxied = build_download_routes(url, "auto", proxy)
    assert all(route.proxy is None for route in direct)
    assert {(route.url, route.proxy) for route in proxied} == {
        (route.url, choice) for route in direct for choice in (None, proxy)
    }
    assert (proxied[0].url, proxied[0].proxy) == (url, proxy)


def test_low_speed_requires_a_full_window_and_sustained_duration():
    from src.services.app_update.download_sources import DownloadPolicy, SlowDownloadMonitor

    clock = [0.0]
    monitor = SlowDownloadMonitor(
        DownloadPolicy(warmup=1, speed_window=2, slow_duration=3, slow_speed=100, finish_grace=0),
        clock=lambda: clock[0],
    )
    for now in [0.5, 1.0, 2.0, 3.0, 4.0, 5.9]:
        clock[0] = now
        assert not monitor.should_switch(remaining=10000)
    clock[0] = 6.0
    assert monitor.should_switch(remaining=10000)


def test_recovery_breaks_the_continuous_low_speed_interval():
    from src.services.app_update.download_sources import DownloadPolicy, SlowDownloadMonitor

    clock = [0.0]
    monitor = SlowDownloadMonitor(
        DownloadPolicy(warmup=0, speed_window=2, slow_duration=3, slow_speed=100, finish_grace=0),
        clock=lambda: clock[0],
    )
    clock[0] = 2
    assert not monitor.should_switch(remaining=10000)
    clock[0] = 4
    monitor.record(1000)
    assert not monitor.should_switch(remaining=10000)
    clock[0] = 6
    assert not monitor.should_switch(remaining=10000)
    clock[0] = 8
    assert not monitor.should_switch(remaining=10000)
    clock[0] = 9
    assert monitor.should_switch(remaining=10000)


def test_nearly_complete_slow_transfer_is_allowed_to_finish():
    from src.services.app_update.download_sources import DownloadPolicy, SlowDownloadMonitor

    clock = [0.0]
    monitor = SlowDownloadMonitor(
        DownloadPolicy(warmup=0, speed_window=1, slow_duration=1, slow_speed=100, finish_grace=15),
        clock=lambda: clock[0],
    )
    for now in range(1, 5):
        clock[0] = now
        monitor.record(now * 10)
        assert not monitor.should_switch(remaining=20)
    assert monitor.should_switch(remaining=10000)


async def test_probe_compares_body_speed_and_closes_bounded_ranges(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy, probe_routes

    closed = []
    payload = b"a" * 1024
    slow, fast = "https://slow.test/a.zip", "https://fast.test/a.zip"

    class Body(httpx.AsyncByteStream):
        """首包即到但第二块延迟，区分响应头速度和有效传输速度。"""

        def __init__(self, name, delay):
            self.name, self.delay = name, delay

        async def __aiter__(self):
            yield b"a"
            await asyncio.sleep(self.delay)
            yield b"a" * 31
            raise AssertionError("Probe read exceeded its byte budget")

        async def aclose(self):
            closed.append(self.name)

    respx_mock.get(slow).respond(206, headers={"Content-Range": "bytes 0-31/1024"}, stream=Body("slow", 0.05))
    fast_route = respx_mock.get(fast).respond(206, headers={"Content-Range": "bytes 0-31/1024"}, stream=Body("fast", 0))
    session = make_session(tmp_path, payload, [slow, fast])
    results = await probe_routes(session.routes, session.artifact, DownloadPolicy(probe_bytes=32))
    assert results[0].route.url == fast and results[0].speed > results[1].speed
    assert all(result.resumable for result in results)
    assert sorted(closed) == ["fast", "slow"]
    assert fast_route.calls[0].request.headers["Range"] == "bytes=0-31"
    assert not session.cache.part_path.exists()


async def test_probe_deadline_closes_stalled_bodies(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy, probe_routes

    closed = asyncio.Event()
    url = "https://stalled.test/a.zip"

    class Body(httpx.AsyncByteStream):
        """返回少量数据后永久等待，必须由探测总时限取消。"""

        async def __aiter__(self):
            yield b"a"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    respx_mock.get(url).respond(200, stream=Body())
    session = make_session(tmp_path, b"a" * 1024, [url])
    results = await asyncio.wait_for(
        probe_routes(session.routes, session.artifact, DownloadPolicy(probe_timeout=1, probe_budget=0.05)), 0.5
    )
    assert closed.is_set()
    assert len(results) == 1


async def test_slow_switch_closes_old_stream_before_new_writer(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy

    payload = b"a" * 10000
    first, second = "https://first.test/a.zip", "https://second.test/a.zip"
    closed = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        """连接未断开但停止发包，由独立监控触发换源。"""

        async def __aiter__(self):
            yield payload[:10]
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def respond(request):
        """探测保持相同结果，真正下载时首源停顿、备用源按偏移恢复。"""
        if request.headers.get("Range") == "bytes=0-7":
            return httpx.Response(206, headers={"Content-Range": "bytes 0-7/10000"}, content=payload[:8])
        if request.url.host == "first.test":
            return httpx.Response(200, stream=StalledBody())
        assert closed.is_set(), "下一条线路打开前旧正文必须关闭"
        assert request.headers["Range"] == "bytes=10-"
        return httpx.Response(206, headers={"Content-Range": "bytes 10-9999/10000"}, content=payload[10:])

    respx_mock.get(first).mock(side_effect=respond)
    respx_mock.get(second).mock(side_effect=respond)
    # 首源探测有效，备用源探测失败但仍可用于真正的恢复请求。
    first_probe = True

    def second_response(request):
        """使首源稳定排在前面，避免依赖运行器的微秒计时顺序。"""
        nonlocal first_probe
        if first_probe:
            first_probe = False
            return httpx.Response(503)
        return respond(request)

    respx_mock.get(second).mock(side_effect=second_response)
    policy = DownloadPolicy(
        network_retries=0,
        probe_bytes=8,
        warmup=0,
        speed_window=0.02,
        slow_duration=0.02,
        poll_interval=0.01,
        finish_grace=0,
    )
    session = make_session(tmp_path, payload, [first, second], automatic=True, policy=policy)
    result = await asyncio.wait_for(session.run(), 1)
    assert result.read_bytes() == payload


async def test_all_slow_sources_eventually_continue_without_switching_forever(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy

    payload = b"a" * 1000
    attempts = []

    class Body(httpx.AsyncByteStream):
        """每个源先停顿足够触发换源，最后锁定源时仍能完成。"""

        async def __aiter__(self):
            await asyncio.sleep(0.08)
            yield payload

    def respond(request):
        """探测全部失败，实际请求都能慢速完成。"""
        if "Range" in request.headers:
            return httpx.Response(503)
        attempts.append(request.url.host)
        return httpx.Response(200, stream=Body())

    urls = ["https://first.test/a.zip", "https://second.test/a.zip"]
    for url in urls:
        respx_mock.get(url).mock(side_effect=respond)
    policy = DownloadPolicy(
        network_retries=0,
        warmup=0,
        speed_window=0.01,
        slow_duration=0.01,
        poll_interval=0.005,
        finish_grace=0,
    )
    result = await asyncio.wait_for(make_session(tmp_path, payload, urls, automatic=True, policy=policy).run(), 1)
    assert result.read_bytes() == payload
    assert len(attempts) == 3


async def test_manual_source_does_not_switch_on_low_speed(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy

    payload, url = b"a" * 1000, "https://source.test/a.zip"

    class Body(httpx.AsyncByteStream):
        """固定来源在低速观察期限后返回数据。"""

        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield payload

    route = respx_mock.get(url).respond(200, stream=Body())
    policy = DownloadPolicy(warmup=0, speed_window=0.01, slow_duration=0.01, poll_interval=0.005)
    assert (await make_session(tmp_path, payload, [url], policy=policy).run()).read_bytes() == payload
    assert route.call_count == 1


@pytest.mark.parametrize("automatic", [True, False])
async def test_invalid_proxy_does_not_prevent_direct_download(tmp_path, respx_mock, automatic):
    from src.services.app_update.download_sources import DownloadRoute

    payload, url = b"complete archive", "https://source.test/app.zip"
    respx_mock.get(url).respond(200, content=payload)
    session = make_session(tmp_path, payload, [url], automatic=automatic)
    session.routes = [DownloadRoute("invalid proxy", url, "http://127.0.0.1:invalid"), DownloadRoute("direct", url)]
    assert (await session.run()).read_bytes() == payload
