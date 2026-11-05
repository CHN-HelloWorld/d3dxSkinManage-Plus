"""验证取消清理和旧会话残留回收不会删除活动任务或缓存目录外的数据。"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from PySide6.QtCore import QLockFile

from tests.services.app_update_download_helpers import make_session


def link_directory(target, link):
    """创建真实目录链接，Windows 使用不依赖符号链接权限的目录联接。"""
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不支持创建目录链接")


async def test_new_download_removes_old_version_partials(tmp_path, respx_mock):
    old = make_session(tmp_path, b"old version", ["https://old.test/app.zip"])
    old.cache.directory.mkdir(parents=True)
    old.cache.part_path.write_bytes(b"old prefix")
    (old.cache.directory / "resume.json").write_text("{}", encoding="utf-8")
    (old.cache.directory / "resume.tmp").write_text("{}", encoding="utf-8")
    payload, url = b"new version", "https://new.test/app.zip"
    respx_mock.get(url).respond(200, content=payload)
    result = await make_session(tmp_path, payload, [url]).run()
    assert result.read_bytes() == payload
    assert not old.cache.part_path.exists()
    assert not (old.cache.directory / "resume.json").exists()
    assert not (old.cache.directory / "resume.tmp").exists()


async def test_cleanup_keeps_other_active_download_and_unrelated_files(tmp_path, respx_mock):
    old = make_session(tmp_path, b"other version", ["https://other.test/app.zip"])
    old.cache.directory.mkdir(parents=True)
    old.cache.part_path.write_bytes(b"active prefix")
    lock = QLockFile(str(old.cache.directory / "download.lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    unrelated = tmp_path / "user-files"
    unrelated.mkdir()
    (unrelated / "update.zip.part").write_bytes(b"unrelated file")
    payload, url = b"new version", "https://new.test/app.zip"
    respx_mock.get(url).respond(200, content=payload)
    try:
        result = await make_session(tmp_path, payload, [url]).run()
        assert result.read_bytes() == payload
        assert old.cache.part_path.read_bytes() == b"active prefix"
        assert (unrelated / "update.zip.part").read_bytes() == b"unrelated file"
    finally:
        lock.unlock()


async def test_cleanup_does_not_follow_directory_links(tmp_path, respx_mock):
    cache_root, outside = tmp_path / "download", tmp_path / "outside"
    cache_root.mkdir()
    outside.mkdir()
    (outside / "update.zip.part").write_bytes(b"outside data")
    link_directory(outside, cache_root / ("a" * 64))
    payload, url = b"new version", "https://new.test/app.zip"
    respx_mock.get(url).respond(200, content=payload)
    assert (await make_session(cache_root, payload, [url]).run()).read_bytes() == payload
    assert (outside / "update.zip.part").read_bytes() == b"outside data"


async def test_current_artifact_directory_link_is_rejected_before_cleanup(tmp_path, respx_mock):
    cache_root, outside = tmp_path / "download", tmp_path / "outside"
    cache_root.mkdir()
    outside.mkdir()
    files = {"update.zip.part": b"outside data", "resume.json": b"{}", "resume.tmp": b"{}"}
    for name, content in files.items():
        (outside / name).write_bytes(content)
    payload, url = b"new version", "https://new.test/app.zip"
    session = make_session(cache_root, payload, [url])
    link_directory(outside, session.cache.directory)
    route = respx_mock.get(url).respond(503)
    with pytest.raises(RuntimeError):
        await session.run()
    assert {name: (outside / name).read_bytes() for name in files} == files
    assert not (outside / "download.lock").exists()
    assert route.call_count == 0


async def test_cancellation_closes_stream_before_deleting_partial(tmp_path, respx_mock):
    waiting, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    payload, url = b"complete version", "https://source.test/app.zip"

    class Body(httpx.AsyncByteStream):
        """把清理停在响应关闭处，确保删除只由结束后的文件所有者执行。"""

        async def __aiter__(self):
            yield payload[:5]
            waiting.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()

    respx_mock.get(url).respond(200, stream=Body())
    session = make_session(tmp_path, payload, [url])
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    try:
        await asyncio.wait_for(closing.wait(), 1)
        assert session.cache.part_path.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not session.cache.part_path.exists()
