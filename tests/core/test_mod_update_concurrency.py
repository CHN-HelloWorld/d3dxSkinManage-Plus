from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.core.config import Config
from src.core.mod_update_service import ModUpdateService
from src.services.gamebanana.downloader import GbDownloadTask
from src.services.gamebanana.models import GbModFile
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.core.test_deploy_concurrency import hardware as hardware
from tests.helpers import registered_games as registered_games


@pytest.mark.parametrize(
    ("cpus", "memory_gib", "expected"),
    [
        (1, 4, 1),
        (4, 8, 2),
        (8, 16, 4),
        (32, 64, 4),
        (32, 4, 1),
        (None, None, 1),
        (16, None, 2),
    ],
)
def test_auto_update_limit_respects_cpu_memory_and_caps_recommendation(hardware, cpus, memory_gib, expected):
    from src.core.mod_update_concurrency import effective_update_concurrency

    hardware(cpus, memory_gib)
    assert effective_update_concurrency(0) == expected


def test_manual_update_limit_can_exceed_recommendation_and_survives_reload(temp_data_dir, hardware):
    from src.core.mod_update_concurrency import effective_update_concurrency

    hardware(2, 4)
    config = Config()
    config.set("gamebanana.update_check.concurrency", 12)
    config.reload()
    assert effective_update_concurrency(config.get("gamebanana.update_check.concurrency")) == 12


@pytest.mark.parametrize("value", [-1, True, "4", 1.5, None, 17, 1024, 2**31])
def test_invalid_update_limit_falls_back_to_auto_without_losing_other_preferences(temp_data_dir, hardware, value):
    from src.core.mod_update_concurrency import effective_update_concurrency

    hardware(8, 16)
    path = temp_data_dir / "config" / "settings.json"
    path.write_text(
        json.dumps(
            {
                "gamebanana": {
                    "update_check": {
                        "concurrency": value,
                        "on_startup": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config()
    assert config.get("gamebanana.update_check.concurrency") == 0
    assert effective_update_concurrency(value) == 4
    assert config.get("gamebanana.update_check.on_startup") is False


@pytest.fixture
def controlled_updates(qtbot, temp_data_dir):
    """使用真实服务和任务，仅暂停外部下载边界。"""
    config = Config()
    manager = SimpleNamespace(
        current_game_id="game_a",
        get_mod=lambda mid: SimpleNamespace(
            display_name=mid,
            folder_name=mid,
            info=SimpleNamespace(custom_data={}),
        ),
    )

    class Downloads(UpdateDownloadBoundary):
        def __init__(self):
            self.tasks = {}

        def request(self, profile, file, *, game_id, consumer=None):
            task = GbDownloadTask(profile, file, temp_data_dir / file.file_name, game_id=game_id)
            task._set_state("downloading")
            self.tasks[file.file_id] = task
            return task

        def get_task(self, file_id):
            return self.tasks.get(file_id)

        def cancel(self, file_id):
            self.tasks[file_id].cancel()

        def fail(self, file_id):
            task = self.tasks[file_id]
            task._set_state("failed")
            task.failed.emit("Controlled transfer failure")

    service = ModUpdateService(manager, config, object())
    downloads = Downloads()
    service.set_download_manager(downloads)

    def start(count):
        for index in range(count):
            file = GbModFile(
                file_id=index + 1,
                file_name=f"{index}.zip",
                description="",
                download_url=f"https://download.invalid/{index}",
                size_bytes=100,
                date_added_ts=2000,
                md5_checksum="",
                av_result="clean",
                version="v2",
            )
            service.apply_update(str(index), chosen_file=file)

    yield SimpleNamespace(config=config, service=service, downloads=downloads, start=start)
    service.shutdown_updates()


@pytest.mark.parametrize("limit", [1, 2, 6])
def test_configured_limit_controls_update_download_admission(controlled_updates, limit):
    case = controlled_updates
    case.config.set("gamebanana.update_check.concurrency", limit)
    case.start(7)
    states = case.service.update_task_snapshots()
    assert sum(state["phase"] == "downloading" for state in states.values()) == limit
    assert [mid for mid, state in states.items() if state["phase"] == "queued"] == [
        str(index) for index in range(limit, 7)
    ]


def test_default_queue_limit_uses_hardware_recommendation(controlled_updates, hardware):
    hardware(4, 8)
    case = controlled_updates
    case.start(4)
    assert [state["phase"] for state in case.service.update_task_snapshots().values()] == [
        "downloading",
        "downloading",
        "queued",
        "queued",
    ]


def test_finished_update_releases_a_slot_at_the_configured_limit(controlled_updates):
    case = controlled_updates
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.start(3)
    assert [state["phase"] for state in case.service.update_task_snapshots().values()] == [
        "downloading",
        "queued",
        "queued",
    ]
    case.downloads.fail(1)
    assert [state["phase"] for state in case.service.update_task_snapshots().values()] == [
        "failed",
        "downloading",
        "queued",
    ]
