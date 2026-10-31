"""验证更新源竞速的返回时机、响应校验和失败处理。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue
from threading import Barrier, Event

import httpx
import pytest

from src.services.app_update import checker as checker_module
from src.services.app_update.checker import AppUpdateChecker

_MIRRORS = ("", "https://slow-one.example/", "https://slow-two.example/", "https://fast.example/")
_API_PATH = "https://api.github.com/repos/example/app/releases"


@pytest.fixture
def serve(monkeypatch, temp_data_dir):
    """仅替换 HTTP 传输，并记录真实客户端关闭，便于清理后台请求。"""
    real_client = httpx.Client
    monkeypatch.setattr(checker_module, "APP_UPDATE_API_MIRRORS", _MIRRORS)

    def configure(handler):
        """为每个候选请求创建独立客户端，关闭后通过队列通知调用方。"""
        closed = Queue()

        @contextmanager
        def client(**kwargs):
            """保留真实客户端的上下文清理及异常传播。"""
            try:
                with real_client(transport=httpx.MockTransport(handler), **kwargs) as instance:
                    yield instance
            finally:
                closed.put(None)

        monkeypatch.setattr(checker_module.httpx, "Client", client)
        return closed

    return configure


@pytest.mark.parametrize(
    "suffix,payload",
    [("/latest", {"tag_name": "v2.0.0"}), ("?per_page=30", [{"tag_name": "v1.0.0"}])],
    ids=["latest", "history"],
)
def test_race_returns_before_slow_sources_finish(serve, suffix, payload):
    """用同步屏障确保四源均已发起，并在调用返回前持续阻塞慢源。"""
    started = Barrier(len(_MIRRORS) + 1)
    release_slow = Event()

    def handle(request):
        """快速源返回合法数据；慢源由测试释放，超时仅用于避免失败时挂起。"""
        started.wait(timeout=5)
        if request.url.host == "fast.example":
            return httpx.Response(200, json=payload)
        if not release_slow.wait(timeout=5):
            raise httpx.ReadTimeout("Slow source timed out", request=request)
        return httpx.Response(503)

    closed = serve(handle)
    checker = AppUpdateChecker("1.0.0", "example/app")
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(checker._race_fetch, _API_PATH + suffix, 1, None)
        try:
            started.wait(timeout=5)
            assert result.result(timeout=1) == payload
        finally:
            release_slow.set()
            for _ in _MIRRORS:
                closed.get(timeout=5)


@pytest.mark.parametrize("status,payload", [(503, {}), (200, {"message": "Invalid release"})])
def test_race_ignores_invalid_first_responses(serve, status, payload):
    def handle(request):
        """有效源等待其他源完成失败响应，确保先返回的错误不能赢得竞速。"""
        if request.url.host != "fast.example":
            return httpx.Response(status, json=payload)
        for _ in range(len(_MIRRORS) - 1):
            closed.get(timeout=5)
        return httpx.Response(200, json={"tag_name": "v2.0.0"})

    closed = serve(handle)
    result = AppUpdateChecker("1.0.0", "example/app")._race_fetch(_API_PATH + "/latest", 1, None)
    assert result == {"tag_name": "v2.0.0"}


def test_race_raises_when_all_sources_fail(serve):
    def handle(request):
        """所有候选源均返回服务不可用错误。"""
        return httpx.Response(503)

    serve(handle)
    with pytest.raises(httpx.HTTPStatusError):
        AppUpdateChecker("1.0.0", "example/app")._race_fetch(_API_PATH + "/latest", 1, None)
