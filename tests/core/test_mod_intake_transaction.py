from __future__ import annotations

import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.core.mod_intake import (
    DuplicateIntakeError,
    IntakeError,
    IntakeRequest,
    ModIntakeService,
)
from src.core.mod_manager import ModManager
from src.core.mod_package import is_mod_container, read_sidecar
from tests.helpers import registered_games as registered_games
from tests.helpers import scan_and_sync


def _source_folder(parent: Path, name: str = "Skin") -> Path:
    source = parent / name
    source.mkdir(parents=True)
    (source / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return source


def test_prepare_builds_current_container_and_commit_alone_writes_db(
    temp_data_dir,
    tmp_path,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path)
    service = ModIntakeService(manager)

    prepared = service.prepare(
        IntakeRequest(
            source_path=source,
            game_id="game_a",
            node_id=character.id,
            display_name="Display Skin",
        )
    )

    assert source.is_dir()
    assert manager.get_mods(node_id=character.id) == []
    assert is_mod_container(prepared.container_path)
    assert read_sidecar(prepared.body_path) is not None

    mod = service.commit(prepared)

    assert mod.name == "Display Skin"
    assert Path(mod.path) == prepared.body_path
    assert manager.get_mod(mod.mod_id) is not None
    assert source.is_dir()


def test_direct_zip_db_failure_restores_original_after_format_conversion(
    temp_data_dir,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    # 强制统一为 7z：制造「源 zip → 库内 7z」的格式转换，再让 DB 失败验证原档还原
    manager._config.set("import.format", "7z")
    manager._config.set("import.strict_format", True)
    character = manager.add_character(name="Epsilon", node_type="character")
    library = manager._get_character_library(character.id)
    source = library / "DirectSkin.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("DirectSkin/mod.ini", "[Constants]\n")
    original_bytes = source.read_bytes()
    service = ModIntakeService(manager)

    prepared = service.prepare(
        IntakeRequest(
            source_path=source,
            game_id="game_a",
            node_id=character.id,
            move_source=True,
        )
    )
    assert prepared.body_path.is_file() and prepared.body_path.suffix == ".7z"
    assert prepared.source_was_moved is True
    monkeypatch.setattr(manager._index, "add_mod", lambda _mod, **_kwargs: False)

    with pytest.raises(IntakeError, match="index"):
        service.commit(prepared)

    assert source.read_bytes() == original_bytes
    assert not (library / "DirectSkin").exists()
    assert manager.get_mods(node_id=character.id) == []


def test_commit_can_defer_failure_rollback_to_background_worker(
    temp_data_dir,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    library = manager._get_character_library(character.id)
    source = _source_folder(library, "DeferredRollback")
    service = ModIntakeService(manager)
    prepared = service.prepare(
        IntakeRequest(
            source_path=source,
            game_id="game_a",
            node_id=character.id,
            move_source=True,
        )
    )
    monkeypatch.setattr(manager._index, "add_mod", lambda _mod, **_kwargs: False)

    with pytest.raises(IntakeError, match="index"):
        service.commit(prepared, rollback_on_error=False)

    assert (source / ".d3dxmod").is_file()
    assert prepared.source_backup_path is not None
    assert (prepared.source_backup_path / "mod.ini").is_file()
    assert prepared.container_path.exists()

    service.rollback(prepared)

    assert (source / "mod.ini").is_file()
    assert not (source / ".d3dxmod").exists()
    assert not prepared.body_path.exists()


def test_direct_drop_cancel_restores_original_source(temp_data_dir, tmp_path):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    library = manager._get_character_library(character.id)
    source = _source_folder(library, "CancelledSkin")
    service = ModIntakeService(manager)
    cancelled = False

    def progress(_value, text: str) -> None:
        nonlocal cancelled
        if text.startswith("复制到 Mod 库"):
            cancelled = True

    with pytest.raises(IntakeError):
        service.prepare(
            IntakeRequest(
                source_path=source,
                game_id="game_a",
                node_id=character.id,
                move_source=True,
            ),
            progress=progress,
            should_cancel=lambda: cancelled,
        )

    assert (source / "mod.ini").is_file()
    assert not (source / source.name).exists()
    assert manager.get_mods(node_id=character.id) == []


def test_empty_content_hash_keeps_direct_drop_untouched(
    temp_data_dir,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    library = manager._get_character_library(character.id)
    source = _source_folder(library, "EmptyHash")
    service = ModIntakeService(manager)
    monkeypatch.setattr(
        "src.core.mod_intake.Mod.compute_sha1",
        lambda *_args, **_kwargs: "",
    )

    with pytest.raises(IntakeError, match="hash is empty"):
        service.prepare(
            IntakeRequest(
                source_path=source,
                game_id="game_a",
                node_id=character.id,
                move_source=True,
            )
        )

    assert (source / "mod.ini").is_file()
    assert not (source / ".d3dxmod").exists()
    assert manager.get_mods(node_id=character.id) == []


def test_direct_drop_sidecar_failure_restores_original(
    temp_data_dir,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    library = manager._get_character_library(character.id)
    source = _source_folder(library, "SidecarFailure")
    service = ModIntakeService(manager)
    monkeypatch.setattr(
        "src.core.mod_intake.write_sidecar",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(IntakeError, match="sidecar"):
        service.prepare(
            IntakeRequest(
                source_path=source,
                game_id="game_a",
                node_id=character.id,
                move_source=True,
            )
        )

    assert (source / "mod.ini").is_file()
    assert not (source / source.name).exists()
    assert manager.get_mods(node_id=character.id) == []


def test_override_install_failure_rolls_back_format_conversion(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path, "OverrideFailure")
    service = ModIntakeService(manager)
    monkeypatch.setattr(
        "src.core.mod_intake.install_staged_ini_overrides",
        lambda *_args, **_kwargs: False,
    )

    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
        target_format="zip",
        strict_format=True,
    )

    with pytest.raises(IntakeError, match="overrides"):
        service.prepare(request)

    assert source.is_dir()
    assert (source / "mod.ini").is_file()
    assert manager.get_mods(node_id=character.id) == []
    assert not any(
        library_item.name == "OverrideFailure"
        for library_item in manager._get_character_library(character.id).iterdir()
    )


def test_prepare_reports_indeterminate_hash_and_real_conversion_progress(
    temp_data_dir,
    tmp_path,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path, "ProgressSkin")
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
        target_format="zip",
    )
    events: list[tuple[float | None, str]] = []

    prepared = service.prepare(
        request,
        progress=lambda value, text: events.append((value, text)),
    )
    service.rollback(prepared)

    assert any(value is None and text.startswith("校验内容") for value, text in events)
    assert any(value is not None and text.startswith("转为 ZIP") for value, text in events)
    assert any(value is not None and text.startswith("复制到 Mod 库") for value, text in events)


def test_two_prepared_equal_imports_commit_only_one_and_keep_first_container(
    temp_data_dir,
    tmp_path,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    first_source = _source_folder(tmp_path / "one", "SameSkin")
    second_source = _source_folder(tmp_path / "two", "SameSkin")
    service = ModIntakeService(manager)
    request_args = {
        "game_id": "game_a",
        "node_id": character.id,
    }
    first = service.prepare(IntakeRequest(source_path=first_source, **request_args))
    second = service.prepare(IntakeRequest(source_path=second_source, **request_args))

    first_mod = service.commit(first)
    with pytest.raises(DuplicateIntakeError):
        service.commit(second)

    assert Path(first_mod.path).is_file()  # 库内本体恒为压缩档
    assert first.container_path.is_dir()
    assert not second.container_path.exists()
    assert first_source.is_dir()
    assert second_source.is_dir()
    assert len(manager.get_mods(node_id=character.id)) == 1


def test_planned_existing_hash_skips_before_container_copy(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    first_source = _source_folder(tmp_path / "one", "SameSkin")
    second_source = _source_folder(tmp_path / "two", "SameSkin")
    service = ModIntakeService(manager)
    first = service.plan_request(
        planning_snapshot=service.planning_snapshot("game_a"),
        source_path=first_source,
        game_id="game_a",
        node_id=character.id,
    )
    service.commit(service.prepare(first))
    duplicate = service.plan_request(
        planning_snapshot=service.planning_snapshot("game_a"),
        source_path=second_source,
        game_id="game_a",
        node_id=character.id,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("duplicate content reached container copy")

    monkeypatch.setattr(
        manager._storage,
        "copy_mod_into_container",
        forbidden,
    )

    with pytest.raises(DuplicateIntakeError):
        service.prepare(duplicate)

    assert second_source.is_dir()


def test_commit_uses_one_explicit_index_transaction(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path)
    service = ModIntakeService(manager)
    prepared = service.prepare(
        IntakeRequest(
            source_path=source,
            game_id="game_a",
            node_id=character.id,
        )
    )
    original_transaction = manager._index.transaction
    entered = 0

    @contextmanager
    def tracking_transaction():
        nonlocal entered
        entered += 1
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(manager._index, "transaction", tracking_transaction)

    service.commit(prepared)

    assert entered == 1


def test_final_container_validation_failure_rolls_back_prepared_copy(
    temp_data_dir,
    tmp_path,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path)
    service = ModIntakeService(manager)
    prepared = service.prepare(
        IntakeRequest(
            source_path=source,
            game_id="game_a",
            node_id=character.id,
        )
    )
    (prepared.container_path / ".d3dxmod").unlink()

    with pytest.raises(IntakeError, match="container"):
        service.commit(prepared)

    assert not prepared.container_path.exists()
    assert source.is_dir()
    assert manager.get_mods(node_id=character.id) == []


def test_planned_prepare_does_not_read_shared_index(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path)
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("prepare accessed the main-thread index")

    monkeypatch.setattr(manager, "_get_character_library", forbidden)
    monkeypatch.setattr(manager, "get_characters", forbidden)
    monkeypatch.setattr(manager, "_write_mod_sidecar", forbidden)

    prepared = service.prepare(request)

    assert prepared.body_path.is_file()  # 库内本体恒为压缩档
    assert "classification" not in read_sidecar(prepared.body_path)["local"]
    service.rollback(prepared)


def test_mod_manager_import_delegates_to_unified_intake(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(tmp_path)
    calls: list[str] = []
    sentinel = object()

    def plan_request(_service, **kwargs):
        calls.append("plan")
        assert Path(kwargs["source_path"]) == source
        assert kwargs["node_id"] == character.id
        return "request"

    def prepare(_service, request):
        calls.append("prepare")
        assert request == "request"
        return "prepared"

    def commit(_service, prepared):
        calls.append("commit")
        assert prepared == "prepared"
        return sentinel

    monkeypatch.setattr(ModIntakeService, "plan_request", plan_request)
    monkeypatch.setattr(ModIntakeService, "prepare", prepare)
    monkeypatch.setattr(ModIntakeService, "commit", commit)

    result = manager.import_mod(
        source_path=str(source),
        node_id=character.id,
        name="Display Skin",
    )

    assert result is sentinel
    assert calls == ["plan", "prepare", "commit"]


def test_scan_and_sync_delegates_bare_drop_to_unified_intake(
    temp_data_dir,
    monkeypatch,
):
    manager = ModManager()
    manager.set_game("game_a")
    character = manager.add_character(name="Epsilon", node_type="character")
    source = _source_folder(
        manager._get_character_library(character.id),
        "DirectSkin",
    )
    calls: list[object] = []

    def plan_request(_service, **kwargs):
        calls.append(("plan", kwargs))
        assert Path(kwargs["source_path"]) == source
        assert kwargs["move_source"] is True
        return "request"

    def prepare(_service, request):
        calls.append(("prepare", request))
        return "prepared"

    def commit(_service, prepared):
        calls.append(("commit", prepared))
        return object()

    monkeypatch.setattr(ModIntakeService, "plan_request", plan_request)
    monkeypatch.setattr(ModIntakeService, "prepare", prepare)
    monkeypatch.setattr(ModIntakeService, "commit", commit)

    assert scan_and_sync(manager) == 1
    assert [call[0] for call in calls] == ["plan", "prepare", "commit"]
