from __future__ import annotations

import json
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_index import ModIndex
from src.models.mod import Mod
from src.utils.paths import get_app_dir as real_get_app_dir
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


def relocate_app(root: Path, monkeypatch) -> Path:
    """移动应用文件并重新绑定位置，模拟移动后重启。"""
    token = uuid.uuid4().hex[:10]
    moved = root.with_name("app-moved-" + token)
    unrelated = root.with_name("launch-cwd-" + token)
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    assert root.resolve().parent == moved.resolve().parent
    assert not moved.resolve().is_relative_to(root.resolve())
    shutil.move(str(root), str(moved))
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("src."):
            continue
        for attribute, relative in (("get_app_dir", ""), ("get_data_dir", "data"), ("get_config_dir", "config")):
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, lambda rel=relative: moved / rel)
    return moved


def make_indexed_mod(root: Path):
    body = root / "data/Game/mods/Skin/Skin.zip"
    preview = root / "data/Game/mods/Skin/.d3dxmeta/previews/main.png"
    for file, content in ((body, b"archive"), (preview, b"image")):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    index = ModIndex("Game")
    mod = make_mod(
        index, "Skin", mod_id="11111111111141118111111111111111", body_name="Skin.zip", preview_images=[str(preview)]
    )
    # 此部署目录位于应用内，应与资源库一起移动。
    mod.deployed_path = str(root / "external/Mods/Skin")
    mod.info.custom_data = {"gamebanana": {"mod_id": 123}, "notes": "https://example.com/image.png"}
    assert index.add_mod(mod)
    from src.models.deployment import DeploymentRecord, DeploymentState

    index.upsert_deployment(DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, mod.deployed_path))
    return index, mod


def test_frozen_application_keeps_selected_data_root_for_the_process(tmp_path, monkeypatch):
    from src.utils import paths

    monkeypatch.setattr(paths, "get_app_dir", real_get_app_dir)

    application = tmp_path / "portable-app"
    application.mkdir()
    monkeypatch.setattr(sys, "executable", str(application / "manager.exe"))
    monkeypatch.setattr(paths, "__compiled__", SimpleNamespace(onefile=False), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert paths.get_app_dir() == application
    # 后续临时写入探测失败不得将已有引用转向另一资源库。
    monkeypatch.setattr(paths, "_is_writable", lambda directory: False)
    assert paths.resolve_stored_path("data/Skin.zip") == str(application / "data/Skin.zip")


def test_index_persists_managed_references_without_application_prefix(temp_data_dir):
    index, mod = make_indexed_mod(temp_data_dir)
    try:
        row = index._db().execute("SELECT * FROM mods WHERE mod_id='11111111111141118111111111111111'").fetchone()
        assert row["path"] == "Skin.zip"
        assert json.loads(row["preview_images_json"]) == [{"scope": "container", "path": ".d3dxmeta/previews/main.png"}]
        info = json.loads(row["info_json"])
        assert info["custom_data"]["gamebanana"]["mod_id"] == 123
        assert index.get_deployment(mod.mod_id).actual_path == mod.deployed_path
        assert mod.path == str(temp_data_dir / "data/Game/mods/Skin/Skin.zip")
    finally:
        index.close()


def test_index_loads_after_application_moves_from_unrelated_cwd(temp_data_dir, monkeypatch):
    index, original = make_indexed_mod(temp_data_dir)
    external = str(temp_data_dir.parent / "external-skin.zip")
    assert not index.add_mod(make_mod(index, "External", path=external))
    index.close()
    moved = relocate_app(temp_data_dir, monkeypatch)
    reopened = ModIndex("Game")
    try:
        mod = reopened.get_mod("11111111111141118111111111111111")
        assert Path(mod.path).read_bytes() == b"archive"
        assert Path(mod.primary_preview).read_bytes() == b"image"
        assert mod.info.custom_data["gamebanana"]["mod_id"] == 123
        assert Path(mod.path).is_relative_to(moved)
        assert mod.deployed_path == str(moved / "external/Mods/Skin")
        assert mod.mod_id == original.mod_id
        assert mod.info.custom_data["notes"] == "https://example.com/image.png"
        assert reopened.find_mod_id_by_path(mod.path, "Game") == mod.mod_id
        assert reopened.find_mod_id_by_path(original.path, "Game") is None
        assert reopened.find_mod_id_by_path(external, "Game") is None
    finally:
        reopened.close()


def test_mod_dictionary_roundtrip_uses_new_application_location(temp_data_dir, monkeypatch):
    index, mod = make_indexed_mod(temp_data_dir)
    index.close()
    serialized = mod.to_dict()
    moved = relocate_app(temp_data_dir, monkeypatch)
    restored = Mod.from_dict(serialized)
    assert restored.path == str(moved / "data/Game/mods/Skin/Skin.zip")
    assert Path(restored.primary_preview).read_bytes() == b"image"
    assert restored.deployed_path == str(moved / "external/Mods/Skin")


def test_preset_journal_keeps_internal_source_and_deployment_portable(temp_data_dir, monkeypatch):
    from src.core.preset_deployment_plan import DeploymentAction, PresetDeploymentPlan

    source = temp_data_dir / "data/Game/mods/Skin/Skin.zip"
    deploy = str(temp_data_dir / "chosen-external/Mods/Skin")
    plan = PresetDeploymentPlan(
        "op1",
        "Game",
        "p1",
        "p2",
        (
            DeploymentAction(
                "11111111111141118111111111111111",
                str(source),
                deploy,
                deploy + "-off",
                deploy + ".part",
                "materialize_active",
            ),
        ),
    )
    serialized = plan.to_json()
    moved = relocate_app(temp_data_dir, monkeypatch)
    restored = PresetDeploymentPlan.from_json(serialized)
    assert restored.actions[0].source_path == str(moved / "data/Game/mods/Skin/Skin.zip")
    assert restored.actions[0].active_path == str(moved / "chosen-external/Mods/Skin")
    assert json.loads(serialized)["actions"][0]["source_path"] == "data/Game/mods/Skin/Skin.zip"


def test_worker_protocol_uses_its_file_location_after_move(temp_data_dir, monkeypatch):
    from src.core.mod_update_paths import load_update_plan, load_update_result, store_update_plan, store_update_result

    relative_work = "data/Game/mods/Skin/.d3dxmeta/update_work/job"
    work = temp_data_dir / relative_work
    source = temp_data_dir / "data/Game/mods/Skin/Skin.zip"
    deploy = str(temp_data_dir / "user-selected/Mods/Skin")
    plan = {
        "work_root": str(work),
        "old_path": str(source),
        "deploy_target": deploy,
        "archive": str(temp_data_dir / "data/downloads/new.zip"),
        "old_deployed_path": deploy,
    }
    data = store_update_plan(plan)
    result = store_update_result({"body": str(work / "container/Skin.zip"), "deployment": deploy}, work)
    moved = relocate_app(temp_data_dir, monkeypatch)
    restored = load_update_plan(data, moved / relative_work)
    prepared = load_update_result(result, moved / relative_work)
    assert data["work_root"] == "."
    assert restored["old_path"] == str(moved / "data/Game/mods/Skin/Skin.zip")
    assert restored["archive"] == str(moved / "data/downloads/new.zip")
    assert restored["deploy_target"] == str(moved / "user-selected/Mods/Skin")
    assert restored["old_deployed_path"] == str(moved / "user-selected/Mods/Skin")
    assert prepared["body"] == str(moved / relative_work / "container/Skin.zip")
    assert prepared["deployment"] == str(moved / "user-selected/Mods/Skin")


def test_character_thumbnail_reference_survives_move(temp_data_dir, monkeypatch):
    index = ModIndex("Game")
    reference = {"scope": "node", "path": "portrait.png"}
    index._db().execute(
        "INSERT INTO nodes(id,game_id,directory_name,thumbnail) VALUES('node','Game','Character',?)",
        (json.dumps(reference),),
    )
    index._db().commit()
    image = index.paths.resolve_image("node", reference)
    image.parent.mkdir(parents=True)
    image.write_bytes(b"portrait")
    index.close()
    moved = relocate_app(temp_data_dir, monkeypatch)
    reopened = ModIndex("Game")
    restored = json.loads(reopened._db().execute("SELECT thumbnail FROM nodes WHERE id='node'").fetchone()[0])
    assert restored == reference
    assert reopened.paths.resolve_image("node", restored).read_bytes() == b"portrait"
    assert reopened.paths.resolve_image("node", restored).is_relative_to(moved)
    reopened.close()


@pytest.mark.parametrize("external", [False, True])
def test_background_path_survives_move_and_atomic_settings_save(temp_data_dir, monkeypatch, external):
    from src.core.config import Config

    background = temp_data_dir.parent / "wallpaper.png" if external else temp_data_dir / "data/appearance/wallpaper.png"
    background.parent.mkdir(parents=True, exist_ok=True)
    background.write_bytes(b"wallpaper")
    config = Config()
    config.set("appearance.background.path", str(background))
    assert config.set_atomic("general.theme", "light")
    stored = json.loads((temp_data_dir / "config/settings.json").read_text(encoding="utf-8"))
    expected = str(background) if external else "data/appearance/wallpaper.png"
    assert stored["appearance"]["background"]["path"] == expected
    moved = relocate_app(temp_data_dir, monkeypatch)
    Config._instance = None
    restored = Config()
    path = restored.get("appearance.background")["path"]
    assert Path(path).read_bytes() == b"wallpaper"
    assert path == str(background if external else moved / "data/appearance/wallpaper.png")


def test_game_preview_and_internal_runtime_locations_follow_application(temp_data_dir, monkeypatch):
    from src.core.game_config_store import GameConfigStore

    preview = temp_data_dir / "data/Game/preview.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"game image")
    internal = {key: str(temp_data_dir / "user-selected" / key) for key in ("mods_path", "migoto_path", "exe_path")}
    store = GameConfigStore()
    assert store.set_games(
        [{"id": "Game", "name": "Game", "name_en": "Game", "resource_dir": "Game", "preview": str(preview), **internal}]
    )
    stored = json.loads((temp_data_dir / "config/games.json").read_text(encoding="utf-8"))["games"][0]
    assert stored["preview"] == "data/Game/preview.png"
    moved = relocate_app(temp_data_dir, monkeypatch)
    GameConfigStore._instance = None
    restored = GameConfigStore().get_games()[0]
    assert restored["preview"] == str(moved / "data/Game/preview.png")
    assert Path(restored["preview"]).read_bytes() == b"game image"
    assert {key: restored[key] for key in internal} == {key: str(moved / "user-selected" / key) for key in internal}


def test_download_history_opens_existing_archive_after_move(temp_data_dir, monkeypatch):
    from src.services.gamebanana.downloader import GbDownloadManager

    archive = temp_data_dir / "data/downloads/skin.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"downloaded")
    manager = GbDownloadManager(archive.parent)
    file = SimpleNamespace(
        file_id=12, file_name="skin.zip", md5_checksum="", download_url="https://example.com/skin.zip"
    )
    manager._persist_history(file, archive, SimpleNamespace(mod_id=34), "Game")
    stored = json.loads((archive.parent / "queue.json").read_text(encoding="utf-8"))
    assert stored["items"][0]["path"] == "data/downloads/skin.zip"
    moved = relocate_app(temp_data_dir, monkeypatch)
    restored = GbDownloadManager(moved / "data/downloads")
    assert restored.get_task(12).archive_path.read_bytes() == b"downloaded"


def test_pending_install_and_update_preview_survive_move(temp_data_dir, monkeypatch):
    from src.services.gamebanana.pending_install_store import PendingInstall
    from src.services.gamebanana.update_checker import UpdateInfo

    archive = temp_data_dir / "data/downloads/skin.zip"
    pending = PendingInstall(
        "Game", str(archive), 34, "Skin", file_id=12, preview_images=["https://example.com/preview.png"]
    )
    update = UpdateInfo.from_dict({"local_mod_id": "m1", "local_preview": str(temp_data_dir / "data/preview.png")})
    pending_data = pending.to_dict()
    update_data = update.to_dict()
    moved = relocate_app(temp_data_dir, monkeypatch)
    assert PendingInstall.from_dict(pending_data).archive_path == str(moved / "data/downloads/skin.zip")
    assert UpdateInfo.from_dict(update_data).local_preview == str(moved / "data/preview.png")
    assert pending_data["preview_images"] == ["https://example.com/preview.png"]


@pytest.fixture
def library(temp_data_dir):
    from src.core.mod_manager import ModManager
    from tests.helpers import register_games, reset_game_service

    register_games("Game")
    manager = ModManager()
    manager.set_game("Game")
    yield manager
    if manager._ini_sync is not None:
        manager._ini_sync.stop()
    manager._index.close()
    reset_game_service()


def test_category_rename_keeps_sql_paths_portable(library, temp_data_dir):
    character = library.add_character(name="Before")
    archive = temp_data_dir / "skin.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("skin/mod.ini", "[Constants]\n")
    mod = library.import_mod(str(archive), character.id)
    assert mod is not None
    thumbnail = temp_data_dir / "data/Game/portraits/char.png"
    from PIL import Image

    thumbnail.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "red").save(thumbnail)
    assert library.update_character_thumbnail(character.id, str(thumbnail))
    original_path = mod.path
    assert library.update_character(character.id, "After")
    row = library._index._db().execute("SELECT path FROM mods WHERE mod_id=?", (mod.mod_id,)).fetchone()
    assert row["path"] == Path(mod.path).name
    moved = library._index.get_mod(mod.mod_id)
    assert library._index.find_mod_id_by_path(moved.path, "Game") == mod.mod_id
    assert library._index.find_mod_id_by_path(original_path, "Game") is None
    row = library._index._db().execute("SELECT thumbnail FROM nodes WHERE id=?", (character.id,)).fetchone()
    reference = json.loads(row["thumbnail"])
    assert reference["scope"] == "node"
    assert library._index.paths.resolve_image(character.id, reference).is_file()
