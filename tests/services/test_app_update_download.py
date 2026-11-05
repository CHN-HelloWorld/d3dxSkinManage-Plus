"""验证更新包的真实落盘结果、跨源恢复和异常 HTTP 响应的处理。"""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from tests.services.app_update_download_helpers import InterruptedBody, make_session


async def test_interrupted_download_resumes_on_next_source(tmp_path, respx_mock):
    payload = b"first part--second part"
    first, second = "https://first.test/app.zip", "https://second.test/app.zip"
    respx_mock.get(first).respond(
        200, headers={"Content-Length": str(len(payload))}, stream=InterruptedBody(payload[:7])
    )
    requests = []

    def resume(request):
        """新线路只交付剩余区间，错误的偏移无法生成完整文件。"""
        requests.append(request)
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes 7-{len(payload) - 1}/{len(payload)}"},
            content=payload[7:],
        )

    respx_mock.get(second).mock(side_effect=resume)
    progress = []
    session = make_session(
        tmp_path, payload, [first, second], on_progress=lambda done, total: progress.append((done, total))
    )
    result = await session.run()
    assert result.read_bytes() == payload
    assert requests[0].headers["Range"] == "bytes=7-"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert progress[-1] == (len(payload), len(payload))
    assert not session.cache.part_path.exists()


async def test_failed_session_discards_partial_and_restarts_from_zero(tmp_path, respx_mock):
    payload = b"a retained prefix and remaining data"
    url = "https://source.test/app.zip"
    route = respx_mock.get(url).respond(200, stream=InterruptedBody(payload[:9]))
    first = make_session(tmp_path, payload, [url])
    with pytest.raises(RuntimeError):
        await first.run()
    assert not first.cache.part_path.exists()
    assert not (first.cache.directory / "resume.json").exists()
    route.respond(200, content=payload)
    second = make_session(tmp_path, payload, [url])
    result = await second.run()
    assert "Range" not in route.calls[-1].request.headers
    assert result.read_bytes() == payload


async def test_ignored_range_restarts_without_appending_full_response(tmp_path, respx_mock):
    payload = b"the complete archive"
    first, second = "https://first.test/app.zip", "https://second.test/app.zip"
    respx_mock.get(first).respond(200, stream=InterruptedBody(payload[:5]))
    respx_mock.get(second).respond(200, content=payload)
    sources = []
    session = make_session(tmp_path, payload, [first, second], on_source=lambda route, offset: sources.append(offset))
    result = await session.run()
    assert result.read_bytes() == payload
    assert sources[-1] == 0


@pytest.mark.parametrize(
    "status,headers",
    [
        (206, {}),
        (206, {"Content-Range": "bytes 4-11/12"}),
        (206, {"Content-Range": "bytes 5-11/13"}),
        (206, {"Content-Range": "bytes 5-4/12"}),
        (206, {"Content-Range": "bytes 5-12/12"}),
        (206, {"Content-Range": "bytes 5-11/*"}),
        (206, {"Content-Range": "bytes 5-11/12 trailing"}),
        (206, {"Content-Range": "bytes 5-11/12", "Content-Length": "6"}),
        (416, {"Content-Range": "bytes */12"}),
    ],
)
async def test_invalid_resume_response_is_closed_before_full_retry(tmp_path, respx_mock, status, headers):
    payload = b"abcdefghijkl"
    first, second = "https://first.test/app.zip", "https://second.test/app.zip"
    respx_mock.get(first).respond(200, stream=InterruptedBody(payload[:5]))
    requests = []

    def respond(request):
        """无效续传体不是完整包；只有新的全量请求才返回正确内容。"""
        requests.append(request)
        if "Range" in request.headers:
            return httpx.Response(status, headers=headers, content=b"invalid")
        return httpx.Response(200, content=payload)

    respx_mock.get(second).mock(side_effect=respond)
    result = await make_session(tmp_path, payload, [first, second]).run()
    assert result.read_bytes() == payload
    assert len(requests) == 2
    assert requests[0].headers["Range"] == "bytes=5-"
    assert "Range" not in requests[1].headers


@pytest.mark.parametrize("content", [b"abcdefghijk", b"abcdefghijklm", b"ABCDEFGHIJKL"])
async def test_wrong_size_or_digest_never_publishes_archive(tmp_path, respx_mock, content):
    payload = b"abcdefghijkl"
    url = "https://source.test/app.zip"
    respx_mock.get(url).respond(200, content=content)
    session = make_session(tmp_path, payload, [url])
    with pytest.raises(RuntimeError):
        await session.run()
    assert not session.cache.final_path.exists()


async def test_corrupt_cross_source_prefix_is_discarded_before_clean_retry(tmp_path, respx_mock):
    payload = b"abcdefghijkl"
    first, second = "https://first.test/app.zip", "https://second.test/app.zip"
    respx_mock.get(first).respond(200, stream=InterruptedBody(b"XXXXX"))
    requests = []

    def respond(request):
        """完整重下返回正确包，分段返回无法修复上一个源写入的错误前缀。"""
        requests.append(request)
        if "Range" in request.headers:
            return httpx.Response(206, headers={"Content-Range": "bytes 5-11/12"}, content=payload[5:])
        return httpx.Response(200, content=payload)

    respx_mock.get(second).mock(side_effect=respond)
    result = await make_session(tmp_path, payload, [first, second]).run()
    assert result.read_bytes() == payload
    assert len(requests) == 2 and "Range" not in requests[-1].headers


@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_cancellation_interrupts_pending_io_and_removes_part(tmp_path, respx_mock, stage):
    waiting = asyncio.Event()
    closed = asyncio.Event()
    payload = b"abcdefghij"
    url = "https://source.test/app.zip"

    class StalledBody(httpx.AsyncByteStream):
        """持续等待下一块数据，并记录上下文关闭。"""

        async def __aiter__(self):
            yield payload[:5]
            waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def respond(request):
        """分别模拟等待响应头与等待正文的取消边界。"""
        if stage == "headers":
            waiting.set()
            await asyncio.Event().wait()
        return httpx.Response(200, stream=StalledBody())

    route = respx_mock.get(url).mock(side_effect=respond)
    session = make_session(tmp_path, payload, [url])
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    if stage == "body":
        assert closed.is_set()
    assert not session.cache.part_path.exists()
    assert not (session.cache.directory / "resume.json").exists()
    route.respond(200, content=payload)
    assert (await make_session(tmp_path, payload, [url]).run()).read_bytes() == payload
    assert "Range" not in route.calls[-1].request.headers


async def test_changed_artifact_does_not_reuse_previous_prefix(tmp_path, respx_mock):
    url = "https://source.test/app.zip"
    route = respx_mock.get(url).respond(200, stream=InterruptedBody(b"old"))
    with pytest.raises(RuntimeError):
        await make_session(tmp_path, b"old archive", [url]).run()
    route.respond(200, content=b"new archive")
    result = await make_session(tmp_path, b"new archive", [url]).run()
    assert "Range" not in route.calls[-1].request.headers
    assert result.read_bytes() == b"new archive"


async def test_complete_cached_archive_is_verified_before_reuse(tmp_path, respx_mock):
    payload, url = b"complete archive", "https://source.test/app.zip"
    route = respx_mock.get(url).respond(200, content=payload)
    result = await make_session(tmp_path, payload, [url]).run()
    assert (await make_session(tmp_path, payload, [url]).run()) == result
    assert route.call_count == 1
    result.write_bytes(b"corrupt archive!")
    assert (await make_session(tmp_path, payload, [url]).run()).read_bytes() == payload
    assert route.call_count == 2


async def test_encoded_response_is_rejected_before_writing(tmp_path, respx_mock):
    payload, url = b"complete archive", "https://source.test/app.zip"
    respx_mock.get(url).respond(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(gzip.compress(payload))
    )
    session = make_session(tmp_path, payload, [url])
    with pytest.raises(RuntimeError):
        await session.run()
    assert not session.cache.final_path.exists()


@pytest.mark.parametrize("size,digest", [(0, "a" * 64), (12, ""), (12, "not a digest")])
def test_artifact_requires_size_and_sha256(size, digest):
    from src.services.app_update.download_state import UpdateArtifact

    with pytest.raises(ValueError):
        UpdateArtifact("https://source.test/app.zip", size, digest)


async def test_concurrent_sessions_cannot_write_same_artifact(tmp_path, respx_mock):
    waiting = asyncio.Event()
    payload, url = b"complete archive", "https://source.test/app.zip"
    requests = []

    async def respond(request):
        """保持首个会话持有缓存锁，第二个会话不得发起下载。"""
        requests.append(request)
        waiting.set()
        await asyncio.Event().wait()

    respx_mock.get(url).mock(side_effect=respond)
    first = asyncio.create_task(make_session(tmp_path, payload, [url]).run())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        with pytest.raises(RuntimeError):
            await make_session(tmp_path, payload, [url]).run()
        assert len(requests) == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


async def test_valid_short_range_continues_until_full_size(tmp_path, respx_mock):
    payload = b"abcdefghijkl"
    first, second = "https://first.test/app.zip", "https://second.test/app.zip"
    respx_mock.get(first).respond(200, stream=InterruptedBody(payload[:5]))

    def respond(request):
        """服务器限制单次范围长度，后续请求必须从实际区间末尾继续。"""
        if request.headers["Range"] == "bytes=5-":
            return httpx.Response(206, headers={"Content-Range": "bytes 5-7/12"}, content=payload[5:8])
        assert request.headers["Range"] == "bytes=8-"
        return httpx.Response(206, headers={"Content-Range": "bytes 8-11/12"}, content=payload[8:])

    respx_mock.get(second).mock(side_effect=respond)
    assert (await make_session(tmp_path, payload, [first, second]).run()).read_bytes() == payload


async def test_cancel_discards_complete_but_unverified_part(tmp_path, respx_mock):
    payload, url = b"complete archive", "https://source.test/app.zip"
    waiting = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        """完整内容已经落盘，用户在请求结束前取消仍应清理暂存文件。"""

        async def __aiter__(self):
            yield payload
            waiting.set()
            await asyncio.Event().wait()

    route = respx_mock.get(url).respond(200, stream=Body())
    session = make_session(tmp_path, payload, [url])
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.cache.part_path.exists()
    route.respond(200, content=payload)
    result = await make_session(tmp_path, payload, [url]).run()
    assert result.read_bytes() == payload
    assert route.call_count == 2
    assert "Range" not in route.calls[-1].request.headers


async def test_connection_error_after_complete_body_verifies_before_retry(tmp_path, respx_mock):
    from src.services.app_update.download_sources import DownloadPolicy

    payload, url = b"complete archive", "https://source.test/app.zip"
    requests = []

    def respond(request):
        """末尾连接错误不应导致对完整分片发送越界 Range 并删除正确内容。"""
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=InterruptedBody(payload))
        if "Range" in request.headers:
            return httpx.Response(416, headers={"Content-Range": f"bytes */{len(payload)}"})
        return httpx.Response(503)

    respx_mock.get(url).mock(side_effect=respond)
    result = await make_session(tmp_path, payload, [url], policy=DownloadPolicy(network_retries=1)).run()
    assert result.read_bytes() == payload
    assert len(requests) == 1


async def test_small_network_chunks_do_not_flood_progress_callbacks(tmp_path, respx_mock, monkeypatch):
    from src.services.app_update import download

    payload, url = b"x" * 1000, "https://source.test/app.zip"
    progress = []

    class Body(httpx.AsyncByteStream):
        """同一采样时间内产生大量小块，模拟高频网络回调。"""

        async def __aiter__(self):
            for _ in range(1000):
                yield b"x"

    monkeypatch.setattr(download, "monotonic", lambda: 10.0, raising=False)
    respx_mock.get(url).respond(200, stream=Body())
    result = await make_session(
        tmp_path, payload, [url], on_progress=lambda done, total: progress.append((done, total))
    ).run()
    assert result.read_bytes() == payload
    assert progress[-1] == (1000, 1000)
    assert len(progress) < 20, "进度发布应按时间合并，不能让每个小块都进入界面队列"


@pytest.mark.parametrize("part_size", [5, 100])
async def test_previous_session_checkpoint_is_removed_before_download(tmp_path, respx_mock, part_size):
    payload, url = b"complete update", "https://source.test/app.zip"
    route = respx_mock.get(url).respond(200, content=payload)
    first = make_session(tmp_path, payload, [url])
    first.cache.directory.mkdir(parents=True)
    first.cache.part_path.write_bytes(b"x" * part_size)
    (first.cache.directory / "resume.json").write_text("{old metadata}", encoding="utf-8")
    (first.cache.directory / "resume.tmp").write_text("interrupted write", encoding="utf-8")
    result = await make_session(tmp_path, payload, [url]).run()
    assert "Range" not in route.calls[-1].request.headers
    assert result.read_bytes() == payload
    assert not (first.cache.directory / "resume.json").exists()
    assert not (first.cache.directory / "resume.tmp").exists()
