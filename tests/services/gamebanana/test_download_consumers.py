import asyncio
import hashlib

import httpx
import pytest

from src.services.gamebanana.downloader import GbDownloadManager
from src.services.gamebanana.models import GbAuthor, GbModel, GbModFile, GbModProfile


@pytest.fixture
def manager(qapp, tmp_path, monkeypatch):
    # 在测试事件循环中执行真实任务，避免第二个下载循环产生竞争。
    monkeypatch.setattr(GbDownloadManager, "_start_loop", lambda self: None)
    monkeypatch.setattr(GbDownloadManager, "_schedule", lambda self, task: None)
    result = GbDownloadManager(tmp_path / "downloads")
    yield result
    result.shutdown()


def _profile(md5="", *, file_id=10, size=11):
    return GbModProfile(
        mod_id=1,
        model=GbModel.MOD,
        name="shared",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
        files=[
            GbModFile(
                file_id=file_id,
                file_name="mod.zip",
                description="",
                download_url="https://example.test/mod.zip",
                size_bytes=size,
                date_added_ts=0,
                md5_checksum=md5,
                av_result="clean",
            )
        ],
    )


def _cached(manager, *, file_id=10, body=b"hello world", md5=None):
    checksum = hashlib.md5(body).hexdigest() if md5 is None else md5
    path = manager._base / f"{checksum or 'nomd5'}__{file_id}__mod.zip"
    path.write_bytes(body)
    manager.refresh_local_index()
    return path, checksum


async def test_releasing_update_keeps_shared_store_download_running(manager, respx_mock):
    first = b"a" * 65536
    second = b"b" * 65536
    waiting, finish = asyncio.Event(), asyncio.Event()

    class SharedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            waiting.set()
            await finish.wait()
            yield second

    profile = _profile(hashlib.md5(first + second).hexdigest(), size=len(first + second))
    file = profile.files[0]
    respx_mock.get(file.download_url).respond(
        200,
        headers={"Content-Length": str(file.size_bytes)},
        stream=SharedBody(),
    )
    store_task = manager.request(profile, file, game_id="game-a")
    store_task.bind_loop(asyncio.get_running_loop())
    running = asyncio.create_task(store_task._run_async())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert store_task.last_progress[0] == 65536
        update_task = manager.request(profile, file, game_id="game-a", consumer="mod-update:one")
        assert update_task is store_task
        assert manager.has_other_consumers(file.file_id, {"mod-update:one"})
        assert manager.has_other_consumers(file.file_id, {"download:10"})
        assert not manager.has_other_consumers(file.file_id, {"download:10", "mod-update:one"})

        manager.release("mod-update:one")
        assert store_task.state == "downloading"
        assert not running.done()
        finish.set()
        await asyncio.wait_for(running, 2)
        assert store_task.archive_path.read_bytes() == first + second
        assert store_task.state == "done"

        manager.delete_archive_when_unused(store_task.archive_path)
        assert store_task.archive_path.exists(), "A completed store download still owns its archive"
        manager.release("download:10")
        assert not store_task.archive_path.exists()
        assert manager.has_local_md5(file.md5_checksum) is None
    finally:
        if not running.done():
            store_task.cancel()
            await running


def test_explicit_request_retains_before_signals_without_default_owner(manager):
    profile = _profile()
    observed = []
    manager.task_added.connect(lambda task: observed.append(manager.has_other_consumers(10, set())))
    task = manager.request(profile, profile.files[0], game_id="game-a", consumer="mod-update:one")
    assert task is manager.get_task(10)
    assert observed == [True]
    assert not manager.has_other_consumers(10, {"mod-update:one"})
    manager.release("mod-update:one")
    assert not manager.has_other_consumers(10, set())


def test_cache_hit_retains_idempotent_owners_and_infers_path(manager):
    path, md5 = _cached(manager)
    profile = _profile(md5.upper())
    assert manager.request(profile, profile.files[0], game_id="game-a") is None
    assert manager.request(profile, profile.files[0], game_id="game-a", consumer="mod-update:one") is None
    manager.retain(10, md5, owner="mod-update:one")
    manager.retain(10, md5.upper(), owner="mod-update:one")
    assert manager.get_task(10) is None
    manager.delete_archive_when_unused(path)
    manager.release("download:10")
    assert path.exists()
    manager.release("mod-update:one")
    assert not path.exists()
    assert manager.has_local_md5(md5) is None


def test_different_file_ids_with_same_md5_protect_the_shared_archive(manager):
    path, md5 = _cached(manager)
    other_path, _ = _cached(manager, file_id=20)
    manager.retain(10, md5, owner="mod-update:one", path=path)
    manager.retain(20, md5.upper(), owner="mod-update:two", path=other_path)
    assert not manager.has_other_consumers(10, {"mod-update:one"}), "Network sharing is keyed by file ID"
    manager.delete_archive_when_unused(path)
    manager.release("mod-update:one")
    assert path.exists()
    manager.release("mod-update:two")
    assert not path.exists()
    assert other_path.exists(), "Only the archive explicitly requested for deletion should be removed"


def test_queued_md5_consumer_protects_archive_discovered_later(manager):
    body = b"queued cache consumer"
    md5 = hashlib.md5(body).hexdigest()
    manager.retain(20, md5, owner="mod-update:queued")
    path, _ = _cached(manager, body=body)
    manager.delete_archive_when_unused(path)
    assert path.exists()
    manager.release("mod-update:queued")
    assert not path.exists()


def test_empty_md5_only_protects_its_canonical_path(manager):
    owned, _ = _cached(manager, file_id=10, md5="")
    unrelated, _ = _cached(manager, file_id=20, md5="")
    alternate = owned.parent / "nested" / ".." / owned.name
    manager.retain(10, "", owner="install:one", path=alternate)
    manager.retain(30, "", owner="unrelated:queued")
    manager.delete_archive_when_unused(unrelated)
    assert not unrelated.exists(), "Empty MD5s must not equate unrelated consumers"
    manager.delete_archive_when_unused(owned)
    assert owned.exists()
    manager.release("install:one")
    assert not owned.exists()


def test_matching_path_protects_archive_even_with_different_metadata(manager):
    path, _ = _cached(manager)
    manager.retain(999, "different-md5", owner="install:one", path=path)
    manager.delete_archive_when_unused(path)
    assert path.exists()
    manager.release("install:one")
    assert not path.exists()


def test_retain_updates_existing_owner_and_releases_previous_archive(manager):
    previous, _ = _cached(manager, file_id=10, md5="")
    current, _ = _cached(manager, file_id=20, md5="")
    manager.retain(10, "", owner="install:one", path=previous)
    manager.delete_archive_when_unused(previous)
    manager.retain(20, "", owner="install:one", path=current)
    assert not previous.exists()
    assert not manager.has_other_consumers(10, set())
    assert manager.has_other_consumers(20, set())
    manager.delete_archive_when_unused(current)
    manager.retain(20, "", owner="install:one")
    assert current.exists(), "An idempotent retain must preserve its known path"
    manager.release("install:one")
    assert not current.exists()


def test_retain_infers_task_path_without_md5(manager):
    profile = _profile()
    task = manager.request(profile, profile.files[0], game_id="game-a")
    manager.retain(10, "", owner="mod-update:queued")
    manager.cancel(10)
    task.archive_path.write_bytes(b"local archive")
    manager.delete_archive_when_unused(task.archive_path)
    assert task.archive_path.exists()
    manager.release("mod-update:queued")
    assert not task.archive_path.exists()


@pytest.mark.parametrize("terminal", ["failed", "canceled"])
async def test_failed_or_canceled_task_releases_only_default_owner(manager, respx_mock, terminal):
    profile = _profile()
    task = manager.request(profile, profile.files[0], game_id="game-a")
    manager.request(profile, profile.files[0], game_id="game-a", consumer="mod-update:one")
    if terminal == "failed":
        respx_mock.get(profile.files[0].download_url).respond(503)
        await task._run_async()
    else:
        manager.cancel(10)
    assert task.state == terminal
    assert not manager.has_other_consumers(10, {"mod-update:one"})
    assert manager.has_other_consumers(10, set())
    manager.release("mod-update:one")
    assert not manager.has_other_consumers(10, set())


@pytest.mark.parametrize("remove", ["delete_record", "delete_file", "clear_completed"])
async def test_removing_done_task_releases_default_but_preserves_explicit_owner(manager, respx_mock, remove):
    body = b"hello world"
    profile = _profile(hashlib.md5(body).hexdigest())
    respx_mock.get(profile.files[0].download_url).respond(200, content=body)
    task = manager.request(profile, profile.files[0], game_id="game-a")
    await task._run_async()
    manager.retain(10, profile.files[0].md5_checksum, owner="install:one", path=task.archive_path)
    if remove == "clear_completed":
        manager.clear_completed()
    else:
        manager.delete_task(10, delete_file=remove == "delete_file")
    assert manager.get_task(10) is None
    assert not manager.has_other_consumers(10, {"install:one"})
    assert task.archive_path.read_bytes() == body
    manager.release("install:one")
    assert task.archive_path.exists() is (remove != "delete_file")


async def test_late_cancellation_of_removed_task_does_not_release_replacement_owner(manager, respx_mock):
    waiting = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 65536
            waiting.set()
            await asyncio.Event().wait()

    profile = _profile(size=131072)
    respx_mock.get(profile.files[0].download_url).respond(200, stream=StalledBody())
    old = manager.request(profile, profile.files[0], game_id="game-a")
    old.bind_loop(asyncio.get_running_loop())
    running = asyncio.create_task(old._run_async())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        manager.delete_task(10, delete_file=True)
        replacement = manager.request(profile, profile.files[0], game_id="game-a")
        await asyncio.wait_for(running, 2)
        assert old.state == "canceled"
        assert replacement is not old
        assert manager.has_other_consumers(10, set())
        assert not old._part_path().exists()
    finally:
        if not running.done():
            old.cancel()
            await running


def test_delete_archive_keeps_files_outside_download_directory(manager, tmp_path):
    outside = tmp_path / "personal.zip"
    outside.write_bytes(b"personal archive")
    manager.delete_archive_when_unused(outside)
    manager.release("unknown")
    assert outside.read_bytes() == b"personal archive"


def test_empty_archive_deletion_also_refreshes_local_index(manager):
    path, md5 = _cached(manager, body=b"")
    changes = []
    manager.queue_changed.connect(lambda: changes.append(True))
    manager.delete_archive_when_unused(path)
    assert not path.exists()
    assert changes == [True]
    assert manager.has_local_md5(md5) is None


async def test_pause_pending_distinguishes_interrupt_from_normal_shared_download(manager, respx_mock):
    waiting = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 65536
            waiting.set()
            await asyncio.Event().wait()

    profile = _profile(size=131072)
    respx_mock.get(profile.files[0].download_url).respond(200, stream=StalledBody())
    task = manager.request(profile, profile.files[0], game_id="game-a")
    task.bind_loop(asyncio.get_running_loop())
    running = asyncio.create_task(task._run_async())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not task.pause_pending
        manager.pause(10)
        assert task.state == "downloading"
        assert task.pause_pending
        await asyncio.wait_for(running, 2)
        assert task.state == "paused"
        task.resume()
        assert not task.pause_pending
        assert task.last_progress[0] == 65536
    finally:
        if not running.done():
            task.cancel()
            await running
