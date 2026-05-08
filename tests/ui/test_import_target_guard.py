from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from src.core.mod_intake import IntakeError, ModIntakeService, PreparedIntake
from src.core.mod_manager import ModManager
from src.ui.dialogs.import_progress import (
    ImportProgressDialog,
    ImportWorkerThread,
    ParallelImportWorkerThread,
)
from src.ui.widgets.task_center import ST_FAILED, TaskCenterPanel
from src.utils import fs
from tests.helpers import register_games, reset_game_service


@pytest.fixture
def manager(temp_data_dir):
    register_games("game_a")
    instance = ModManager()
    instance.set_game("game_a")
    yield instance
    instance.stop_ini_sync()
    reset_game_service()


def _source(parent: Path, name: str) -> Path:
    source = parent / name
    source.mkdir()
    (source / "mod.ini").write_text(f"[Constants]\n; {name}\n", encoding="utf-8")
    return source


@pytest.mark.parametrize("worker_type", [ImportWorkerThread, ParallelImportWorkerThread])
def test_workers_skip_deleted_target_and_continue_other_targets(
    qtbot,
    manager,
    tmp_path,
    worker_type,
):
    deleted_character = manager.add_character("A")
    kept_character = manager.add_character("B")
    service = ModIntakeService(manager)
    requests = [
        service.plan_request(
            source_path=_source(tmp_path, f"Mod{index}"),
            game_id="game_a",
            node_id=character.id,
            target_format="zip",
        )
        for index, character in enumerate([deleted_character, deleted_character, kept_character])
    ]
    assert manager.delete_character(deleted_character.id)
    results = {}
    worker = worker_type(requests, intake_service=service)

    def accept(index, result):
        try:
            results[index] = service.commit(result) if isinstance(result, PreparedIntake) else result
        finally:
            worker.acknowledge_db_done()

    worker.file_prepared.connect(accept)
    try:
        with qtbot.waitSignal(worker.finished, timeout=10000):
            worker.start()
    finally:
        worker.terminate_import()
        assert worker.wait(10000)

    assert isinstance(results[0], IntakeError)
    assert isinstance(results[1], IntakeError)
    assert "目标目录" in str(results[0])
    assert not requests[0].destination_path.exists()
    assert results[2].node_id == kept_character.id
    assert len(manager.get_mods()) == 1
    assert all((request.source_path / "mod.ini").is_file() for request in requests)


@pytest.mark.parametrize("deletion", ["filesystem", "application"])
@pytest.mark.parametrize("delete_before_dialog", [False, True])
def test_dialog_reports_deleted_target_without_recreating_it(
    qtbot,
    manager,
    tmp_path,
    monkeypatch,
    deletion,
    delete_before_dialog,
):
    character = manager.add_character("A")
    destination = manager._get_character_library(character.id, create=False)
    source = _source(tmp_path, "Mod1")
    host = QWidget()
    qtbot.addWidget(host)
    panel = TaskCenterPanel(host, embedded=True)

    def delete_target():
        if deletion == "application":
            assert manager.delete_character(character.id)
        else:
            fs.rmtree(destination)

    if delete_before_dialog:
        delete_target()
    dialog = ImportProgressDialog([(str(source), "Mod1")], character.id, manager, panel=panel)
    qtbot.addWidget(dialog)
    if delete_before_dialog and deletion == "application":
        with monkeypatch.context() as guard:
            guard.setattr(
                fs,
                "list_children",
                lambda path: pytest.fail("Missing destination must not fall back to scanning the working directory"),
            )
            dialog._cleanup_orphaned_files(set())
    if not delete_before_dialog:
        delete_target()

    try:
        with qtbot.waitSignal(dialog.import_finished, timeout=10000):
            dialog.start_import()
    finally:
        if dialog._worker is not None:
            dialog._worker.terminate_import()
            assert dialog._worker.wait(10000)

    assert not destination.exists()
    assert manager.get_mods() == []
    assert panel._items[0]._status == ST_FAILED
    assert "目标目录" in panel._items[0]._state_label.text()
    assert panel._failed
    assert (source / "mod.ini").is_file()


def test_dialog_explains_target_deleted_after_preparation(qtbot, manager, tmp_path):
    character = manager.add_character("A")
    source = _source(tmp_path, "Mod1")
    host = QWidget()
    qtbot.addWidget(host)
    panel = TaskCenterPanel(host, embedded=True)
    dialog = ImportProgressDialog([(str(source), "Mod1")], character.id, manager, panel=panel)
    qtbot.addWidget(dialog)
    request = dialog._plan_intake_requests()[0]
    prepared = dialog._intake_service.prepare(request)
    assert manager.delete_character(character.id)

    dialog._on_file_prepared(0, prepared)

    assert dialog._completed_results == [(0, None)]
    assert panel._items[0]._status == ST_FAILED
    assert "目标目录" in panel._items[0]._state_label.text()
    assert manager.get_mods() == []
