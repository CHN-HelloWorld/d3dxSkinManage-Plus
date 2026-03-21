from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.models.mod import Mod
from tests.helpers import register_games, reset_game_service
from tests.helpers import registered_games as registered_games


@pytest.fixture
def manager(temp_data_dir):
    register_games("AA", "BB")
    manager = ModManager()
    yield manager
    manager.stop_ini_sync()
    manager._index.close()
    reset_game_service()


def _make_archive(root: Path, *, payload: bytes = b"preview") -> None:
    root.mkdir(parents=True)
    body = root / "Probe.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("Probe/mod.ini", "[Constants]\n")
        archive.writestr("Probe/preview.png", payload)
    (root / ".d3dxmod").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "d3dxSkinManage-Plus.mod-container",
                "body": body.name,
                "body_type": "archive",
                "content_hash": Mod.compute_sha1(str(body)),
                "mod_id": uuid.uuid4().hex,
            }
        ),
        encoding="utf-8",
    )
    (root / ".d3dxmeta").mkdir()
    (root / ".d3dxmeta" / "info.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "kind": "d3dxSkinManage-Plus.mod-share",
                "name": "Probe",
                "info": {},
                "previews": [],
            }
        ),
        encoding="utf-8",
    )


def _preview(manager: ModManager, game_id: str) -> Path:
    mods = manager.get_mods(game_id=game_id)
    assert len(mods) == 1
    assert len(mods[0].preview_images) == 1
    return Path(mods[0].preview_images[0])


def test_explicit_target_rebuild_still_rejects_active_game_switch(manager, temp_data_dir):
    from src.core.rebuild_session import RebuildSession

    _make_archive(temp_data_dir / "data" / "AA" / "mods" / "Probe")
    manager.set_game("BB")
    session = RebuildSession(manager, "AA")
    try:
        session.prepare()
        while not session.stage_batch():
            pass
        session.validate()
        manager.set_game("AA")
        with pytest.raises(RuntimeError, match="Active game changed"):
            session.commit()
        assert manager.get_mods(game_id="AA") == []
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["inactive_rebuild", "inactive_recover", "other_game"])
def test_rebuild_uses_target_game_without_changing_active_game(manager, temp_data_dir, mode):
    _make_archive(temp_data_dir / "data" / "AA" / "mods" / "Probe")
    if mode == "other_game":
        manager.set_game("BB")
    active_game = manager.current_game_id

    if mode == "inactive_recover":
        result = manager.recover_corrupt_index()
        assert result["reports"]["AA"].registered == 1
    else:
        assert manager.rebuild_index_from_filesystem("AA").registered == 1

    image = _preview(manager, "AA")
    assert image.is_relative_to(temp_data_dir / "data" / "AA" / "preview_cache")
    assert image.read_bytes() == b"preview"
    assert manager.current_game_id == active_game
    assert not (temp_data_dir / "data" / "preview_cache").exists()
    assert not (temp_data_dir / "data" / "BB" / "preview_cache").exists()


def test_rebuild_all_games_keeps_each_cache_alive_during_other_game_cleanup(manager, temp_data_dir):
    # 不同游戏允许相同内容，但缓存归属必须独立。
    for game_id in ("AA", "BB"):
        _make_archive(temp_data_dir / "data" / game_id / "mods" / "Probe")
    manager.set_game("BB")

    reports = manager.rebuild_all_games()
    assert {game: report.registered for game, report in reports.items()} == {"AA": 1, "BB": 1}
    images = {game: _preview(manager, game) for game in ("AA", "BB")}
    for game, image in images.items():
        assert image.is_relative_to(temp_data_dir / "data" / game / "preview_cache")
    assert images["AA"] != images["BB"]

    # 两个根目录都放入孤立文件，以确认清理确实执行。
    for game in ("AA", "BB"):
        orphan = temp_data_dir / "data" / game / "preview_cache" / "orphan"
        orphan.mkdir()
        (orphan / "unused.png").write_bytes(b"unused")
        assert manager._cleanup_orphaned_managed_artifacts(game)
        assert not orphan.exists()
        assert all(image.read_bytes() == b"preview" for image in images.values())
    assert manager.current_game_id == "BB"


def test_rebuild_resolves_target_resource_directory(manager, temp_data_dir):
    store = manager._game_service._store
    games = store.get_games()
    next(game for game in games if game["id"] == "AA")["resource_dir"] = "AA Assets"
    store.set_games(games)
    manager._game_service.reload_games()
    manager._index.refresh_paths()
    _make_archive(temp_data_dir / "data" / "AA Assets" / "mods" / "Probe")

    assert manager.rebuild_index_from_filesystem("AA").registered == 1

    image = _preview(manager, "AA")
    assert image.is_relative_to(temp_data_dir / "data" / "AA Assets" / "preview_cache")
    assert image.read_bytes() == b"preview"
    assert manager.current_game_id == ""


def test_rebuild_dialog_passes_target_game_to_background_hash(manager, temp_data_dir, qtbot):
    from src.ui.dialogs.rebuild_progress import RebuildProgressDialog

    _make_archive(temp_data_dir / "data" / "AA" / "mods" / "Probe")
    manager.set_game("BB")
    dialog = RebuildProgressDialog(manager, "AA")
    qtbot.addWidget(dialog)
    dialog._start()
    qtbot.waitUntil(lambda: dialog.report is not None or bool(dialog.error))
    if dialog._worker is not None:
        assert dialog._worker.wait(5000)
    assert not dialog.error
    assert dialog.report.registered == 1
    image = _preview(manager, "AA")
    assert image.is_relative_to(temp_data_dir / "data" / "AA" / "preview_cache")
    assert image.read_bytes() == b"preview"
    assert manager.current_game_id == "BB"
