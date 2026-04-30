import zipfile
from pathlib import Path

import pytest

from src.core.import_format import (
    PreparedImportSource,
    materialize_staged_previews,
    stage_import_metadata,
)
from src.core.mod_manager import ModManager
from src.models.mod import Mod
from src.utils import fs
from tests.helpers import register_games, reset_game_service

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def preview_import(qapp, temp_data_dir):
    register_games("preview-game")
    manager = ModManager()
    manager.set_game("preview-game")
    manager._config.set("import.format", "zip")
    character = manager.add_character(name="Character", node_type="character")
    archive = temp_data_dir / "Mod-A-v1.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("Top/mod.ini", "[Constants]\nx=1\n")
        stream.writestr("Top/a.png", _PNG)
        stream.writestr("Top/b.png", _PNG)
    yield manager, character, archive
    if manager._ini_sync is not None:
        manager._ini_sync.stop()
    manager._index.close()
    reset_game_service()


@pytest.mark.parametrize("partial_file", [False, True])
def test_partial_preview_extraction_imports_body_without_publishing_cache(
    preview_import,
    monkeypatch,
    temp_data_dir,
    partial_file,
):
    manager, character, archive = preview_import
    original_extract = zipfile.ZipFile.extract

    def fail_second_preview(self, member, *args, **kwargs):
        if str(member).endswith("b.png"):
            if partial_file:
                destination = Path(fs.normal_path(args[0])) / str(member)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"partial image")
            raise OSError("second preview write failed")
        return original_extract(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "extract", fail_second_preview)
    imported = manager.import_mod(str(archive), character.id)
    assert imported is not None
    assert Mod.compute_sha1(imported.path) == Mod.compute_sha1(archive)
    assert imported.preview_images == []
    cache_root = temp_data_dir / "data" / manager._res_dir() / "preview_cache"
    assert not list(cache_root.rglob("*.png"))

    monkeypatch.setattr(zipfile.ZipFile, "extract", original_extract)
    manager.rebuild_index_from_filesystem()
    rebuilt = manager.get_mods()
    assert len(rebuilt) == 1
    assert len(rebuilt[0].preview_images) == 2
    assert all(Path(path).read_bytes() == _PNG for path in rebuilt[0].preview_images)


def test_complete_import_cache_is_reused_without_json(
    preview_import,
    monkeypatch,
    temp_data_dir,
):
    manager, character, archive = preview_import
    imported = manager.import_mod(str(archive), character.id)
    assert imported is not None
    assert len(imported.preview_images) == 2
    assert all(Path(path).read_bytes() == _PNG for path in imported.preview_images)
    assert not list((temp_data_dir / "data").rglob(".preview-content.json"))
    monkeypatch.setattr(
        manager._archive, "extract_files", lambda *args, **kwargs: pytest.fail("complete import cache was reopened")
    )
    manager.rebuild_index_from_filesystem()
    rebuilt = manager.get_mods()
    assert len(rebuilt) == 1
    assert len(rebuilt[0].preview_images) == 2
    assert all(Path(path).read_bytes() == _PNG for path in rebuilt[0].preview_images)


@pytest.mark.parametrize("failure", ["copy", "cancel"])
def test_failed_preview_publication_leaves_no_partial_candidate(
    preview_import,
    monkeypatch,
    temp_data_dir,
    failure,
):
    manager, _character, archive = preview_import
    prepared = PreparedImportSource(archive, "zip", "zip", passthrough_archive=True)
    assert stage_import_metadata(prepared, manager._archive, temp_data_dir / "import-work")
    assert len(prepared.staged_body_previews) == 2
    cache = temp_data_dir / "data" / "preview_cache" / "new-instance"
    original_copy = fs.copy_path
    copied_targets = []
    canceled = False

    def copy(source, target, **kwargs):
        nonlocal canceled
        copied_targets.append(Path(target))
        if failure == "copy" and len(copied_targets) == 2:
            Path(target).write_bytes(b"partial image")
            raise OSError("preview copy failed")
        result = original_copy(source, target, **kwargs)
        canceled = failure == "cancel"
        return result

    monkeypatch.setattr(fs, "copy_path", copy)
    try:
        with pytest.raises(OSError if failure == "copy" else InterruptedError):
            materialize_staged_previews(
                prepared,
                archive,
                cache_dir=cache,
                should_cancel=lambda: canceled,
            )
        assert copied_targets
        assert not list(cache.rglob("*.png"))
        assert not any(path.exists() for path in copied_targets)
    finally:
        prepared.cleanup()
