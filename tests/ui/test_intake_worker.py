from __future__ import annotations

from pathlib import Path

from src.core.mod_intake import IntakeRequest, ModIntakeService
from src.core.mod_manager import ModManager
from src.ui.intake_worker import IntakeWorker
from tests.helpers import registered_games as registered_games


def _source_folder(parent: Path, name: str = "Skin") -> Path:
    source = parent / name
    source.mkdir(parents=True)
    (source / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return source


def test_worker_only_prepares_until_main_thread_acknowledges():
    prepared = object()
    calls: list[str] = []
    progress_events: list[tuple[float | None, str, int, int]] = []
    results = []

    class _Service:
        def prepare(self, request, *, progress, should_cancel):
            calls.append(f"prepare:{request.display_name}")
            progress(None, "校验内容: Skin")
            progress(0.5, "复制到 Mod 库: Skin")
            assert not should_cancel()
            return prepared

        def rollback(self, _prepared) -> None:
            calls.append("rollback")

    request = IntakeRequest(
        source_path=Path("Skin"),
        game_id="game_a",
        node_id="epsilon",
        display_name="Skin",
    )
    worker = IntakeWorker([request], _Service())
    worker.progress_updated.connect(
        lambda value, text, current, total: progress_events.append(
            (value, text, current, total),
        ),
    )
    worker.item_prepared.connect(
        lambda _index, _prepared: worker.acknowledge(committed=True),
    )
    worker.batch_finished.connect(results.append)

    worker.run()

    assert calls == ["prepare:Skin"]
    assert progress_events == [
        (None, "校验内容: Skin", 1, 1),
        (0.5, "复制到 Mod 库: Skin", 1, 1),
    ]
    assert results[0].committed == 1
    assert results[0].failed == 0
    assert results[0].cancelled is False


def test_worker_cancel_rolls_back_prepared_direct_drop(
    temp_data_dir,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(
        manager._get_character_library(character.id),
        "CancelledSkin",
    )
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
        move_source=True,
    )
    results = []
    worker = IntakeWorker([request], service)

    def cancel_before_commit(_index, _prepared) -> None:
        worker.request_cancel()
        worker.acknowledge(committed=False)

    worker.item_prepared.connect(cancel_before_commit)
    worker.batch_finished.connect(results.append)

    worker.run()

    assert (source / "mod.ini").is_file()
    assert manager.get_mods(node_id=character.id) == []
    assert results[0].committed == 0
    assert results[0].cancelled is True
    assert results[0].restored == 1


def test_qthread_prepare_and_main_thread_commit_complete_one_item(
    qtbot,
    temp_data_dir,
    tmp_path,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path, "ThreadedSkin")
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
    )
    worker = IntakeWorker([request], service)

    def commit_prepared(_index, prepared) -> None:
        service.commit(prepared)
        worker.acknowledge(committed=True)

    worker.item_prepared.connect(commit_prepared)
    with qtbot.waitSignal(worker.batch_finished, timeout=5000) as blocker:
        worker.start()
    worker.wait(1000)

    result = blocker.args[0]
    assert result.committed == 1
    assert len(manager.get_mods(node_id=character.id)) == 1
