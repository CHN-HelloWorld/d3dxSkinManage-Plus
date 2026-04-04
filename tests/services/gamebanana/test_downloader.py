import hashlib
import json
from pathlib import Path

import httpx
import pytest

from src.services.gamebanana.downloader import QUEUE_SCHEMA_VERSION, GbDownloadManager
from src.services.gamebanana.models import (
    GbAuthor,
    GbModel,
    GbModFile,
    GbModProfile,
)


@pytest.fixture
def manager(tmp_path):
    m = GbDownloadManager(base_dir=tmp_path)
    yield m
    m.shutdown()


def _make_profile(file_md5: str = "abc") -> GbModProfile:
    return GbModProfile(
        mod_id=1,
        model=GbModel.MOD,
        name="x",
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
                file_id=10,
                file_name="mod.zip",
                description="",
                download_url="https://x/d.zip",
                size_bytes=11,
                date_added_ts=0,
                md5_checksum=file_md5,
                av_result="clean",
            )
        ],
    )


def test_has_local_md5_returns_path_when_cached(manager, tmp_path):
    fake = tmp_path / "aabbcc__10__mod.zip"
    fake.write_bytes(b"hello world")
    manager._scan_cache()
    assert manager.has_local_md5("aabbcc") == fake


def test_request_returns_none_when_md5_hits(manager, tmp_path):
    profile = _make_profile("xyzhit")
    fake = tmp_path / "xyzhit__10__mod.zip"
    fake.write_bytes(b"hello world")
    manager._scan_cache()
    task = manager.request(profile, profile.files[0], game_id="game_a")
    assert task is None


def test_part_file_not_treated_as_downloaded(manager, tmp_path):
    """未完成的 .part 文件不计入已下载资源，并在启动扫描时清理。"""
    part = tmp_path / "deadbeef__310004__elegant_tau.rar.part"
    part.write_bytes(b"partial bytes")
    manager._scan_cache(clean_partials=True)
    assert manager.has_local_md5("deadbeef") is None
    assert not part.exists()


async def test_download_writes_file_and_verifies_md5(manager, respx_mock):
    body = b"hello world"
    md5 = hashlib.md5(body).hexdigest()
    profile = _make_profile(md5)
    respx_mock.get("https://x/d.zip").mock(return_value=httpx.Response(200, content=body))
    task = manager.request(profile, profile.files[0], game_id="game_a")
    assert task is not None
    await task._run_async()
    assert task.state == "done"
    assert task.archive_path.exists()


async def test_md5_mismatch_marks_failed(manager, respx_mock):
    profile = _make_profile("expected_md5_no_match")
    respx_mock.get("https://x/d.zip").mock(return_value=httpx.Response(200, content=b"bytes"))
    task = manager.request(profile, profile.files[0], game_id="game_a")
    await task._run_async()
    assert task.state == "failed"


def test_queue_persisted_to_json(manager, tmp_path):
    profile = _make_profile("p1")
    fake = tmp_path / "p1__10__mod.zip"
    fake.write_bytes(b"hello world")
    manager._scan_cache()
    manager._persist_history(profile.files[0], fake, profile, "game_a")
    queue_file = tmp_path / "queue.json"
    assert queue_file.exists()
    data = json.loads(queue_file.read_text(encoding="utf-8"))
    # 队列文档包含版本信息及完整必填字段。
    assert data["schema_version"] == QUEUE_SCHEMA_VERSION
    assert [e["md5"] for e in data["items"]] == ["p1"]
    assert set(data["items"][0]) == {
        "mod_id",
        "file_id",
        "md5",
        "name",
        "path",
        "download_url",
        "game_id",
        "ts",
    }
    assert data["items"][0]["game_id"] == "game_a"


# 严格校验队列结构和历史记录。


def _write_queue(tmp_path, payload) -> Path:
    p = tmp_path / "queue.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _backups(tmp_path) -> list[Path]:
    return sorted(tmp_path.glob("queue.json.invalid-*"))


def _entry(**over) -> dict:
    e = {
        "mod_id": 1,
        "file_id": 10,
        "md5": "abc",
        "name": "mod.zip",
        "path": "D:/downloads/abc__10__mod.zip",
        "download_url": "https://x/d.zip",
        "game_id": "game_a",
        "ts": "2026-08-26T00:00:00",
    }
    e.update(over)
    return e


def _restored(tmp_path):
    m = GbDownloadManager(base_dir=tmp_path)
    try:
        return m.active(), _backups(tmp_path)
    finally:
        m.shutdown()


@pytest.mark.parametrize(
    "payload",
    [
        [_entry()],  # 顶层列表不符合队列对象的结构约定。
        {"schema_version": 1, "items": {"a": 1}},  # items 不是 list
        "not-json-object",
    ],
)
def test_unreadable_queue_is_backed_up_and_reset(tmp_path, payload):
    _write_queue(tmp_path, payload)
    tasks, backups = _restored(tmp_path)
    assert tasks == []
    assert len(backups) == 1
    assert not (tmp_path / "queue.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [_entry()]},  # 缺版本
        {"schema_version": 99, "items": [_entry()]},  # 版本不匹配
        {"schema_version": "1", "items": [_entry()]},  # 版本不是整数
    ],
)
def test_other_schema_version_is_still_restored(tmp_path, payload):
    _write_queue(tmp_path, payload)
    tasks, backups = _restored(tmp_path)
    assert [t.file.file_id for t in tasks] == [10]
    assert backups == []
    assert (tmp_path / "queue.json").exists()


@pytest.mark.parametrize(
    "over",
    [
        {"file_id": 0},  # 无效 id
        {"file_id": "10"},  # 类型错
        {"name": ""},  # 拒绝空名称。
        {"path": ""},  # 拒绝空路径。
        {"game_id": ""},  # 拒绝缺少游戏归属的记录。
        {"ts": ""},
        {"download_url": 5},
    ],
)
def test_entry_missing_required_field_is_rejected(tmp_path, over):
    entry = _entry(**over)
    _write_queue(tmp_path, {"schema_version": QUEUE_SCHEMA_VERSION, "items": [entry]})
    tasks, backups = _restored(tmp_path)
    assert tasks == []  # 坏条目直接拒绝，不补 0/空串/磁盘名/空 Path
    assert backups == []  # 但整份文件是可解读的 → 不备份重置


def test_missing_key_is_rejected(tmp_path):
    entry = _entry()
    del entry["game_id"]
    _write_queue(tmp_path, {"schema_version": QUEUE_SCHEMA_VERSION, "items": [entry]})
    tasks, _ = _restored(tmp_path)
    assert tasks == []


def test_valid_history_is_restored(tmp_path):
    _write_queue(
        tmp_path,
        {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "items": [_entry(), _entry(file_id=11, md5="", name="b.rar", game_id="game_c")],
        },
    )
    m = GbDownloadManager(base_dir=tmp_path)
    try:
        tasks = {t.file.file_id: t for t in m.active()}
        assert set(tasks) == {10, 11}
        assert tasks[10].state == "done"
        assert tasks[10].game_id == "game_a"
        assert tasks[10].file.file_name == "mod.zip"
        # md5 可以为空串（nomd5 下载），不算缺字段
        assert tasks[11].file.md5_checksum == ""
    finally:
        m.shutdown()


def test_active_filters_by_exact_game_id(tmp_path):
    _write_queue(
        tmp_path,
        {
            "schema_version": QUEUE_SCHEMA_VERSION,
            "items": [_entry(file_id=10, game_id="game_a"), _entry(file_id=11, game_id="game_c")],
        },
    )
    m = GbDownloadManager(base_dir=tmp_path)
    try:
        assert [t.file.file_id for t in m.active(game_id="game_a")] == [10]
        assert [t.file.file_id for t in m.active(game_id="ghost")] == []
        assert len(m.active()) == 2  # 不传 = 不过滤（未激活游戏时的全量视图）
    finally:
        m.shutdown()


def test_download_dir_is_fixed_data_downloads(temp_data_dir):
    from src.utils import paths as paths_mod

    legacy = temp_data_dir / "data" / "cache" / "gb_downloads"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "old.zip").write_bytes(b"x")

    assert paths_mod.get_gb_download_dir() == temp_data_dir / "data" / "downloads"
    assert (legacy / "old.zip").exists()
