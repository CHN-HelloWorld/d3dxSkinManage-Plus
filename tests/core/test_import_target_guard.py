from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_intake import IntakeError, IntakeRequest, ModIntakeService
from src.core.mod_manager import ModManager
from src.utils import fs
from tests.helpers import register_games, reset_game_service
from tests.helpers import registered_games as registered_games


@pytest.fixture
def target_import(temp_data_dir, tmp_path):
    register_games("game_a")
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character("A")
    source = tmp_path / "Mod1"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx = 1\n", encoding="utf-8")
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
        target_format="zip",
        strict_format=True,
    )
    assert request.destination_path.is_relative_to(tmp_path)
    yield manager, service, character, request
    manager.stop_ini_sync()
    reset_game_service()


@pytest.mark.parametrize("deletion", ["filesystem", "application"])
def test_prepare_stops_when_planned_target_was_deleted(target_import, deletion):
    manager, service, character, request = target_import
    destination = request.destination_path
    if deletion == "application":
        assert manager.delete_character(character.id)
    else:
        fs.rmtree(destination)

    with pytest.raises(IntakeError, match="目标目录"):
        service.prepare(request)

    assert not destination.exists()
    assert (request.source_path / "mod.ini").is_file()
    assert manager.get_mods() == []


@pytest.mark.parametrize("deletion", ["directory", "library", "category"])
def test_planning_does_not_recreate_a_deleted_target(target_import, deletion):
    manager, service, character, request = target_import
    deleted = request.destination_path.parent if deletion == "library" else request.destination_path
    if deletion == "category":
        assert manager.delete_character(character.id)
    else:
        fs.rmtree(deleted)

    with pytest.raises(IntakeError, match="目标目录"):
        service.plan_request(
            source_path=request.source_path,
            game_id="game_a",
            node_id=character.id,
        )

    assert not deleted.exists()
    assert manager.get_mods() == []


def test_unplanned_request_does_not_fall_back_to_root_after_category_deletion(target_import):
    manager, service, character, request = target_import
    assert manager.delete_character(character.id)

    with pytest.raises(IntakeError, match="目标目录"):
        service.prepare(
            IntakeRequest(
                source_path=request.source_path,
                game_id="game_a",
                node_id=character.id,
            )
        )

    assert not request.destination_path.exists()
    assert list(request.destination_path.parent.iterdir()) == []
    assert manager.get_mods() == []


@pytest.mark.parametrize("stage", ["校验内容", "复制到 Mod 库"])
def test_target_deleted_during_preparation_is_not_recreated(target_import, stage):
    manager, service, _character, request = target_import
    deleted = False

    def delete_after_start(_value, text):
        nonlocal deleted
        if not deleted and text.startswith(stage):
            fs.rmtree(request.destination_path)
            deleted = True

    with pytest.raises(IntakeError, match="目标目录"):
        service.prepare(request, progress=delete_after_start)

    assert deleted
    assert not request.destination_path.exists()
    assert (request.source_path / "mod.ini").is_file()
    assert manager.get_mods() == []


def test_commit_rejects_deleted_category_when_its_directory_survives(target_import, monkeypatch):
    manager, service, character, request = target_import
    prepared = service.prepare(request)
    original_rmtree = fs.rmtree

    def leave_busy_directory(path, **kwargs):
        # Windows 上目录中的文件被占用时，软件内删除仍可能留下物理目录。
        if Path(path) != request.destination_path:
            return original_rmtree(path, **kwargs)

    monkeypatch.setattr(fs, "rmtree", leave_busy_directory)
    assert manager.delete_character(character.id)
    assert request.destination_path.is_dir()

    with pytest.raises(IntakeError, match="目标目录"):
        service.commit(prepared)

    assert not prepared.container_path.exists()
    assert manager.get_mods() == []
    assert (request.source_path / "mod.ini").is_file()


def test_scan_planning_keeps_other_targets_when_one_disappears(
    target_import,
    monkeypatch,
):
    manager, _service, _character, request = target_import
    other_character = manager.add_character("B")
    other_destination = manager._get_character_library(other_character.id, create=False)
    for destination in (request.destination_path, other_destination):
        fs.write_text(destination / "LooseMod" / "mod.ini", "[Constants]\n")
    discover = manager._discover_library_mods

    def delete_after_discovery(*args, **kwargs):
        descriptors = discover(*args, **kwargs)
        fs.rmtree(request.destination_path)
        return descriptors

    monkeypatch.setattr(manager, "_discover_library_mods", delete_after_discovery)

    requests = manager.plan_scan_intakes()

    assert len(requests) == 1
    assert requests[0].node_id == other_character.id
    assert requests[0].source_path == other_destination / "LooseMod"
    assert (requests[0].source_path / "mod.ini").is_file()
    assert not request.destination_path.exists()
