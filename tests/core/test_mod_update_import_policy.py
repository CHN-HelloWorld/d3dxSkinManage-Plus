from pathlib import Path

import pytest

from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
from src.core.mod_update_worker import prepare_update
from src.services.archive import ArchiveService
from tests.core.test_mod_update_transaction import _metadata
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.helpers import registered_games as registered_games


@pytest.mark.parametrize("target, strict", [("7z", False), ("rar", False), ("zip", True)])
def test_plain_update_keeps_archive_bytes_and_has_no_extraction_phase(update_case, monkeypatch, target, strict):
    manager, old, archive = update_case
    manager._config.set("import.format", target)
    manager._config.set("import.strict_format", strict)
    original = Path(old.path).read_bytes()
    downloaded = archive.read_bytes()

    def forbidden_extraction(*args, **kwargs):
        raise AssertionError("A plain archive that keeps its format must not be extracted")

    monkeypatch.setattr(ArchiveService, "extract", forbidden_extraction)
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    events = []
    try:
        prepared = prepare_update(plan, events.append)
        assert Path(prepared["body"]).suffix == ".zip"
        assert Path(prepared["body"]).read_bytes() == downloaded
        assert {event["phase"] for event in events} == {"verifying", "installing"}
        assert prepared["deployment"] == ""
        assert Path(old.path).read_bytes() == original
        updated = commit_update(manager, plan, prepared)
        assert Path(updated.path).read_bytes() == downloaded
        assert updated.info.version == "v2"
    finally:
        cleanup_update(plan)


def test_strict_different_format_extracts_and_converts_the_update(update_case):
    manager, old, archive = update_case
    manager._config.set("import.format", "7z")
    manager._config.set("import.strict_format", True)
    original = Path(old.path).read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    events = []
    try:
        prepared = prepare_update(plan, events.append)
        body = Path(prepared["body"])
        assert body.suffix == ".7z"
        assert body.read_bytes() != archive.read_bytes()
        assert ArchiveService().read_file(body, "Skin/body.buf") == b"new body"
        assert {event["phase"] for event in events} == {"verifying", "extracting", "installing"}
        assert prepared["deployment"] == ""
        assert Path(old.path).read_bytes() == original
        updated = commit_update(manager, plan, prepared)
        assert Path(updated.path).suffix == ".7z"
        assert updated.info.version == "v2"
    finally:
        cleanup_update(plan)
    assert not Path(old.path).exists()
    assert ArchiveService().read_file(manager.get_mod(old.mod_id).path, "Skin/body.buf") == b"new body"


@pytest.mark.parametrize("parked", [False, True])
def test_passthrough_archive_extracts_only_for_real_deployment(update_case, temp_data_dir, monkeypatch, parked):
    manager, old, archive = update_case
    manager._config.set("import.format", "7z")
    manager._config.set("import.strict_format", False)
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    assert manager.toggle_mod(old.mod_id).is_enabled
    if parked:
        manager.preset_manager.create_preset(
            "game-a",
            "Keep enabled",
            snapshot={old.node_id: [old.mod_id]},
        )
        assert not manager.toggle_mod(old.mod_id).is_enabled
    current = manager.get_mod(old.mod_id)
    manager.stop_ini_sync()
    target = Path(current.deployed_path)
    downloaded = archive.read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    real_extract = ArchiveService.extract
    events = []

    def extract_deployment_only(service, source, destination, **kwargs):
        assert Path(source).parent == Path(plan["work_root"]) / "container"
        assert Path(destination) == Path(plan["deploy_work"]) / "unpacked"
        assert Path(source).read_bytes() == downloaded
        return real_extract(service, source, destination, **kwargs)

    monkeypatch.setattr(ArchiveService, "extract", extract_deployment_only)
    try:
        prepared = prepare_update(plan, events.append)
        assert Path(prepared["body"]).read_bytes() == downloaded
        assert (Path(prepared["deployment"]) / "body.buf").read_bytes() == b"new body"
        assert (target / "body.buf").read_bytes() == b"old body"
        assert any(event["phase"] == "extracting" for event in events)
        updated = commit_update(manager, plan, prepared)
        assert updated.is_enabled is not parked
        assert Path(updated.deployed_path) == target
        assert (target / "body.buf").read_bytes() == b"new body"
    finally:
        cleanup_update(plan)
