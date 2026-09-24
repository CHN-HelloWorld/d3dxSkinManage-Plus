from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.services.gamebanana.downloader import GbDownloadManager
from src.services.gamebanana.models import GbModProfile
from tests.core.test_mod_update_controls import several_updates as several_updates
from tests.core.test_mod_update_controls import update_service as update_service
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.helpers import registered_games as registered_games


class ControlledBody(httpx.AsyncByteStream):
    """控制真实响应字节，模拟不被重复取消中断的 HTTP 清理。"""

    def __init__(self, payload):
        self.payload = payload
        self.first_received = threading.Event()
        self.second_received = threading.Event()
        self.allow_second = threading.Event()
        self.allow_finish = threading.Event()
        self.closing = threading.Event()
        self.allow_close = threading.Event()
        self.allow_close.set()

    async def __aiter__(self):
        yield self.payload[:65536]
        self.first_received.set()
        while not self.allow_second.is_set():
            await asyncio.sleep(0.01)
        yield self.payload[65536:131072]
        self.second_received.set()
        while not self.allow_finish.is_set():
            await asyncio.sleep(0.01)
        yield self.payload[131072:]

    async def aclose(self):
        self.closing.set()
        while not self.allow_close.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                # HTTP 连接清理可能受到取消屏蔽保护。
                pass

    def release(self):
        self.allow_second.set()
        self.allow_finish.set()
        self.allow_close.set()


@pytest.fixture
def network_update(update_service, monkeypatch, qtbot):
    manager, mod, service = update_service
    contents = b"new body" * 32768
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Skin/mod.ini", "[Constants]\nx=1\n")
        archive.writestr("Skin/body.buf", contents)
    payload = buffer.getvalue()
    info = service._status[mod.mod_id]
    file = replace(info.latest_file, size_bytes=len(payload), md5_checksum=hashlib.md5(payload).hexdigest())
    info.latest_file = file
    info.candidate_files = [file]
    body = ControlledBody(payload)
    requests = []
    real_client = httpx.AsyncClient

    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, headers={"Content-Length": str(len(payload))}, stream=body)
        return httpx.Response(200, content=payload)

    def client(**kwargs):
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("src.services.gamebanana.downloader.httpx.AsyncClient", client)
    monkeypatch.setattr("src.utils.proxy.get_active_proxy_url", lambda: None)
    downloads = GbDownloadManager()
    service.set_download_manager(downloads)
    profile = GbModProfile.from_api({"_idRow": 123, "_sName": "Store download"})
    yield SimpleNamespace(
        manager=manager,
        mod=mod,
        service=service,
        file=file,
        downloads=downloads,
        profile=profile,
        body=body,
        payload=payload,
        contents=contents,
        requests=requests,
    )
    body.release()
    try:
        service.shutdown_updates()
    finally:
        tasks = downloads.active()
        for task in tasks:
            if task.is_running or task.state not in {"done", "failed", "canceled"}:
                task.cancel()
        try:
            qtbot.waitUntil(lambda: all(not task.is_running for task in tasks), timeout=5000)
        finally:
            downloads.shutdown()


def test_cancel_waits_for_http_teardown_before_allowing_a_successful_retry(network_update, qtbot):
    case = network_update
    service, mid = case.service, case.mod.mod_id
    before = Path(case.mod.path).read_bytes()
    case.body.allow_close.clear()
    service.apply_update(mid)
    task = case.downloads.get_task(case.file.file_id)
    assert task is not None
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)
    assert task.last_progress[0] == 65536

    token = service.begin_stop_confirmation([mid])
    qtbot.waitUntil(case.body.closing.is_set, timeout=1500)
    assert not case.body.second_received.is_set(), "取消必须打断等待下一块数据的传输"
    service.finish_stop_confirmation(token, confirmed=True)
    assert task.is_running, "关闭响应流尚未结束，下载协程应仍存活"
    assert service.update_task_snapshots()[mid]["phase"] == "stopping"
    assert mid in service.active_update_ids()
    assert Path(case.mod.path).read_bytes() == before

    service.apply_update(mid)
    assert service.update_task_snapshots()[mid]["phase"] == "stopping"
    assert len(case.requests) == 1, "旧传输收尾期间不能启动重试"
    case.body.allow_close.set()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mid]["phase"] == "canceled", timeout=5000)
    assert not task.is_running
    assert mid not in service.active_update_ids()

    service.apply_update(mid)
    qtbot.waitUntil(
        lambda: service.update_task_snapshots()[mid]["phase"] in {"done", "failed", "canceled"}, timeout=15000
    )
    snapshot = service.update_task_snapshots()[mid]
    assert snapshot["phase"] == "done", snapshot
    assert len(case.requests) == 2, "重试应发送新请求，不能复用旧协程的取消结果"
    updated = case.manager.get_mod(mid)
    assert updated.info.version == "v2"
    with zipfile.ZipFile(updated.path) as archive:
        assert archive.read("Skin/body.buf") == case.contents


@pytest.mark.parametrize("control", ["pause", "stop"])
def test_update_control_keeps_store_owned_transfer_running(network_update, qtbot, control):
    case = network_update
    service, mid = case.service, case.mod.mod_id
    before = Path(case.mod.path).read_bytes()
    task = case.downloads.request(case.profile, case.file, game_id="game-a")
    assert task is not None
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)
    service.apply_update(mid)

    if control == "pause":
        assert service.pause_update(mid)
        expected_phase = "paused"
    else:
        token = service.begin_stop_confirmation([mid])
        service.finish_stop_confirmation(token, confirmed=True)
        expected_phase = "canceled"
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mid]["phase"] == expected_phase, timeout=3000)

    case.body.allow_second.set()
    qtbot.waitUntil(
        lambda: task.last_progress[0] == 131072 or task.state in {"paused", "failed", "canceled"}, timeout=3000
    )
    assert task.last_progress[0] == 131072, "更新的独立控制不应中断商店下载的数据流"
    assert task.state == "downloading"
    assert service.update_task_snapshots()[mid]["phase"] == expected_phase

    case.body.allow_finish.set()
    qtbot.waitUntil(lambda: task.state in {"done", "failed", "canceled"}, timeout=5000)
    assert task.state == "done"
    assert task.archive_path.read_bytes() == case.payload
    assert len(case.requests) == 1
    assert service.update_task_snapshots()[mid]["phase"] == expected_phase
    assert case.manager.get_mod(mid).info.version == "v1"
    assert Path(case.mod.path).read_bytes() == before


def test_joined_transfer_snapshot_immediately_includes_existing_bytes_speed_and_eta(network_update, qtbot):
    case = network_update
    task = case.downloads.request(case.profile, case.file, game_id="game-a")
    assert task is not None
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)
    qtbot.wait(450)  # 等待真实下载器的速度采样窗口结束。
    case.body.allow_second.set()
    qtbot.waitUntil(case.body.second_received.is_set, timeout=3000)
    assert task.last_progress == (131072, len(case.payload))
    speed, eta = task.speed_bps, task.eta_seconds
    assert speed > 0
    assert eta > 0

    case.service.apply_update(case.mod.mod_id)
    # 响应体保持暂停，后续数据块无法补回空快照。
    snapshot = case.service.update_task_snapshots()[case.mod.mod_id]
    assert snapshot["phase"] == "downloading"
    assert snapshot["downloaded_bytes"] == 131072
    assert snapshot["total_bytes"] == len(case.payload)
    assert snapshot["speed_bps"] == pytest.approx(speed)
    assert snapshot["eta_seconds"] == pytest.approx(eta)


def test_update_download_limit_does_not_throttle_store_downloads(network_update, qtbot):
    case = network_update
    case.service._config.set("gamebanana.update_check.concurrency", 1)
    case.service.apply_update(case.mod.mod_id)
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)

    store_tasks = [
        case.downloads.request(
            case.profile,
            replace(case.file, file_id=case.file.file_id + index, md5_checksum=""),
            game_id=case.manager.current_game_id,
        )
        for index in (1, 2, 3)
    ]
    qtbot.waitUntil(lambda: all(task.state == "done" for task in store_tasks), timeout=5000)
    assert all(task.archive_path.read_bytes() == case.payload for task in store_tasks)
    assert case.service.update_task_snapshots()[case.mod.mod_id]["phase"] == "downloading"
    assert not case.body.second_received.is_set()


@pytest.mark.parametrize("cache_source", ["checksum", "completed_task"])
def test_store_completion_wakes_queued_update_without_a_network_slot(
    network_update,
    several_updates,
    qtbot,
    cache_source,
):
    case = network_update
    _, mods, service = several_updates
    service._config.set("gamebanana.update_check.concurrency", 1)
    service._config.set("import.parallel_workers", 1)
    service.apply_update(mods[0].mod_id)
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)

    file = replace(
        case.file,
        file_id=case.file.file_id + 1,
        md5_checksum=case.file.md5_checksum if cache_source == "checksum" else "",
    )
    service.apply_update(mods[1].mod_id, chosen_file=file)
    assert service.update_task_snapshots()[mods[1].mod_id]["phase"] == "queued"
    store_task = case.downloads.request(case.profile, file, game_id=case.manager.current_game_id)
    qtbot.waitUntil(lambda: store_task.state == "done", timeout=5000)

    qtbot.waitUntil(lambda: service.update_task_snapshots()[mods[1].mod_id]["phase"] != "queued", timeout=3000)
    qtbot.waitUntil(
        lambda: service.update_task_snapshots()[mods[1].mod_id]["phase"] in {"done", "failed"}, timeout=15000
    )
    snapshot = service.update_task_snapshots()[mods[1].mod_id]
    assert snapshot["phase"] == "done", snapshot
    updated = case.manager.get_mod(mods[1].mod_id)
    assert updated.info.version == "v2"
    with zipfile.ZipFile(updated.path) as archive:
        assert archive.read("Skin/body.buf") == case.contents
    assert service.update_task_snapshots()[mods[0].mod_id]["phase"] == "downloading"
    assert not case.body.second_received.is_set()
    assert len(case.requests) == 2


@pytest.fixture
def shared_network_updates(network_update, several_updates, qtbot):
    case = network_update
    _, mods, service = several_updates
    for mod in mods:
        info = service._status[mod.mod_id]
        info.latest_file = case.file
        info.candidate_files = [case.file]
        service.apply_update(mod.mod_id)
    qtbot.waitUntil(case.body.first_received.is_set, timeout=3000)
    assert service.update_task_snapshots()[mods[-1].mod_id]["phase"] == "queued"
    return case, mods


@pytest.mark.parametrize("scope", ["all", "individual"])
def test_pausing_shared_transfer_includes_queued_update_consumers(shared_network_updates, qtbot, scope):
    case, mods = shared_network_updates
    service = case.service
    task = case.downloads.get_task(case.file.file_id)
    assert task.last_progress[0] == 65536
    if scope == "all":
        assert service.pause_all_updates()
    else:
        for mod in mods:
            assert service.pause_update(mod.mod_id)
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
    qtbot.waitUntil(lambda: not task.is_running, timeout=1500)
    assert task.state == "paused"
    assert task.last_progress[0] == 65536
    assert not case.body.second_received.is_set()

    assert service.resume_update(mods[0].mod_id)
    qtbot.waitUntil(
        lambda: service.update_task_snapshots()[mods[0].mod_id]["phase"] in {"done", "failed"}, timeout=15000
    )
    assert service.update_task_snapshots()[mods[0].mod_id]["phase"] == "done"
    assert all(service.update_task_snapshots()[mod.mod_id]["phase"] == "paused" for mod in mods[1:])


def test_stopping_all_cancels_transfer_retained_by_a_queued_update(shared_network_updates, qtbot):
    case, mods = shared_network_updates
    service = case.service
    task = case.downloads.get_task(case.file.file_id)
    before = {mod.mod_id: Path(mod.path).read_bytes() for mod in mods}
    token = service.begin_stop_confirmation()
    assert token.count == len(mods)
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
    service.finish_stop_confirmation(token, confirmed=True)
    qtbot.waitUntil(lambda: not task.is_running and not service.active_update_ids(), timeout=1500)
    assert task.state == "canceled"
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"canceled"}
    assert not case.downloads.has_other_consumers(case.file.file_id, set())
    for mod in mods:
        assert case.manager.get_mod(mod.mod_id).info.version == "v1"
        assert Path(mod.path).read_bytes() == before[mod.mod_id]
