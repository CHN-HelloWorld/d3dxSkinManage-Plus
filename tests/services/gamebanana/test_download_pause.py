import asyncio
from types import SimpleNamespace

import httpx

from src.services.gamebanana.downloader import GbDownloadManager, GbDownloadTask
from src.services.gamebanana.models import GbModFile


async def test_pause_interrupts_stalled_body_read_and_preserves_received_bytes(tmp_path, respx_mock):
    waiting = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 65536
            waiting.set()
            await asyncio.Event().wait()

    file = GbModFile(
        file_id=42,
        file_name="slow.zip",
        description="",
        download_url="https://example.test/slow.zip",
        size_bytes=131072,
        date_added_ts=0,
        md5_checksum="",
        av_result="clean",
    )
    respx_mock.get(file.download_url).respond(200, headers={"Content-Length": "131072"}, stream=StalledBody())
    task = GbDownloadTask(SimpleNamespace(mod_id=1, name="slow"), file, tmp_path / "slow.zip", game_id="game-a")
    task.bind_loop(asyncio.get_running_loop())
    running = asyncio.create_task(task._run_async())
    await asyncio.wait_for(waiting.wait(), 1)
    assert task.last_progress[0] == 65536
    task.pause()
    done, _ = await asyncio.wait({running}, timeout=0.4)
    if not done:
        task.cancel()
        await running
    assert done, "暂停应中断正在等待的数据读取，而不是等下一块数据到达"
    assert task.state == "paused"
    assert task._part_path().read_bytes() == b"a" * 65536


def test_runtime_cache_refresh_preserves_partial_downloads(qapp, tmp_path):
    abandoned = tmp_path / "oldhash__old.zip.part"
    abandoned.write_bytes(b"abandoned before startup")
    manager = GbDownloadManager(tmp_path)
    try:
        assert not abandoned.exists(), "启动时仍应清理上次运行遗留的分片"
        paused = tmp_path / "newhash__new.zip.part"
        paused.write_bytes(b"received in this session")
        completed = tmp_path / "donehash__done.zip"
        completed.write_bytes(b"completed")
        manager.refresh_local_index()
        assert paused.read_bytes() == b"received in this session", "安装后刷新缓存不能删除其他任务的续传文件"
        assert manager.has_local_md5("newhash") is None
        assert manager.has_local_md5("donehash") == completed
    finally:
        manager.shutdown()
