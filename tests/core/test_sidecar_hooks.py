from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import META_DIR, read_sidecar
from tests.helpers import registered_games as registered_games


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _make_source(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    return d


def test_import_writes_local_sidecar(mm, tmp_path):
    char = mm.add_character(name="示例角色丁", node_type="character")
    src = _make_source(tmp_path, "Skin")
    mod = mm.import_mod(str(src), node_id=char.id, name="示例角色丁·旗袍")
    assert mod is not None
    sc = read_sidecar(mod.path)
    assert sc is not None
    assert sc["schema"] == 2
    assert sc["name"] == "示例角色丁·旗袍"
    assert set(sc["local"]) == {"preview_references"}


@pytest.mark.parametrize("consume_container", [False, True])
def test_confirmed_external_copy_gets_new_identity_despite_duplicate_content(mm, tmp_path, consume_container):
    from src.core.mod_intake import DuplicateIntakeError, ModIntakeService
    from src.core.mod_package import read_container_marker

    source = _make_source(tmp_path, "Original")
    node = mm.add_character(name="Copies", node_type="character")
    original = mm.import_mod(str(source), node_id=node.id)
    assert original is not None
    service = ModIntakeService(mm)
    snapshot = service.planning_snapshot(mm.current_game_id)
    ordinary = service.plan_request(
        source_path=source, game_id=mm.current_game_id, node_id=node.id, planning_snapshot=snapshot
    )
    with pytest.raises(DuplicateIntakeError):
        service.prepare(ordinary)
    if consume_container:
        import shutil

        source = tmp_path / "CopiedContainer"
        shutil.copytree(Path(original.path).parent, source)
    request = service.plan_request(
        source_path=source,
        game_id=mm.current_game_id,
        node_id=node.id,
        external_copy=True,
        move_source=consume_container,
        planning_snapshot=snapshot,
    )
    prepared = service.prepare(request)
    copied = service.commit(prepared)
    assert copied.mod_id != original.mod_id
    assert copied.imported_at != original.imported_at
    assert copied.content_hash == original.content_hash
    assert copied.path != original.path
    marker = read_container_marker(Path(copied.path).parent)
    assert marker["mod_id"] == copied.mod_id
    assert marker["imported_at"] == copied.imported_at
    assert source.exists() is not consume_container


def test_update_mod_refreshes_sidecar(mm, tmp_path):
    char = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=char.id)
    mod.info.tags = ["旗袍", "4K"]
    mod.name = "新名"
    assert mm.update_mod(mod)
    sc = read_sidecar(mod.path)
    assert sc["name"] == "新名"
    assert set(sc["info"]["tags"]) == {"旗袍", "4K"}


def test_rediscovery_restores_all_manual_and_node_preview_references(mm, tmp_path):
    from src.core.mod_intake import ModIntakeService
    from src.core.mod_package import write_sidecar

    node = mm.add_character(name="Manual", node_type="character")
    original = mm.import_mod(str(_make_source(tmp_path, "Original")), node_id=node.id)
    container = Path(original.path).parent
    images = []
    references = []
    for number in range(7):
        path = container / META_DIR / "previews" / f"manual-{number}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"manual")
        images.append(str(path))
        references.append({"scope": "container", "path": path.relative_to(container).as_posix()})
    image = container.parent / "node-manual.png"
    image.write_bytes(b"node")
    images.insert(0, str(image))
    references.insert(0, {"scope": "node", "path": image.name})
    sidecar = read_sidecar(original.path)
    sidecar["local"]["preview_references"] = references
    write_sidecar(original.path, sidecar)
    assert mm._index.delete_mod(original.mod_id)
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=original.path, game_id=mm.current_game_id, node_id=node.id, context="rediscovery"
    )
    restored = service.commit(service.prepare(request))
    assert restored.preview_images == images
    assert mm.get_mod(original.mod_id).preview_images == images


@pytest.mark.parametrize("changed_file", ["sidecar", "marker", "repaired_marker"])
def test_rediscovery_rejects_new_metadata_without_overwriting_it(mm, tmp_path, changed_file):
    import json

    from src.core.mod_intake import IntakeError, ModIntakeService

    node = mm.add_character(name="Versions", node_type="character")
    original = mm.import_mod(str(_make_source(tmp_path, "Original")), node_id=node.id)
    assert mm._index.delete_mod(original.mod_id)
    container = Path(original.path).parent
    path = container / (META_DIR + "/info.json" if changed_file == "sidecar" else ".d3dxmod")
    if changed_file == "repaired_marker":
        before = json.loads(path.read_text(encoding="utf8"))
        before.pop("imported_at")
        path.write_text(json.dumps(before), encoding="utf8")
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=original.path, game_id=mm.current_game_id, node_id=node.id, context="rediscovery"
    )
    prepared = service.prepare(request)
    newer = json.loads(path.read_text(encoding="utf8"))
    newer["name" if changed_file == "sidecar" else "imported_at"] = (
        "External edit" if changed_file == "sidecar" else "2026-10-05T23:01:02"
    )
    path.write_text(json.dumps(newer), encoding="utf8")
    expected = path.read_bytes()
    with pytest.raises(IntakeError, match="metadata changed"):
        service.commit(prepared)
    assert path.read_bytes() == expected
    assert mm.get_mod(original.mod_id) is None
    assert original.mod_id not in mm.inflight_mod_ids()


def test_confirmed_copy_rollback_restores_complete_input_container(mm, tmp_path, monkeypatch):
    import shutil

    from src.core.mod_intake import IntakeError, ModIntakeService

    node = mm.add_character(name="Copies", node_type="character")
    original = mm.import_mod(str(_make_source(tmp_path, "Original")), node_id=node.id)
    source = tmp_path / "CopiedContainer"
    shutil.copytree(Path(original.path).parent, source)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=source, game_id=mm.current_game_id, node_id=node.id, external_copy=True, move_source=True
    )
    prepared = service.prepare(request)
    monkeypatch.setattr(mm._index, "add_mod", lambda *args, **kwargs: False)
    with pytest.raises(IntakeError, match="failed to add Mod"):
        service.commit(prepared)
    after = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert after == before
    assert not prepared.container_path.exists()
    assert mm.get_mod(original.mod_id) is not None


def test_intake_protects_destination_before_creation_until_rollback_finishes(mm, tmp_path, monkeypatch):
    from src.core.mod_intake import ModIntakeService
    from src.utils import fs

    node = mm.add_character(name="Protected", node_type="character")
    source = _make_source(tmp_path, "Input")
    service = ModIntakeService(mm)
    request = service.plan_request(source_path=source, game_id=mm.current_game_id, node_id=node.id, move_source=True)
    copy_path = fs.copy_path
    observed = []

    def guarded_copy(src, dst, **kwargs):
        if Path(dst).parent.parent == mm._get_character_library(node.id):
            assert mm.is_library_path_protected(str(dst))
            observed.append(str(dst))
        return copy_path(src, dst, **kwargs)

    monkeypatch.setattr(fs, "copy_path", guarded_copy)
    prepared = service.prepare(request)
    assert observed
    assert mm.is_library_path_protected(str(source))
    restore = service._restore_source

    def guarded_restore(*args):
        assert mm.is_library_path_protected(str(source))
        restore(*args)
        assert mm.is_library_path_protected(str(source))

    monkeypatch.setattr(service, "_restore_source", guarded_restore)
    service.rollback(prepared)
    assert source.exists()
    assert not mm.is_library_path_protected(str(source))
    assert not mm.is_library_path_protected(str(prepared.container_path))


def test_move_mod_writes_sidecar_at_new_path(mm, tmp_path):
    a = mm.add_character(name="示例角色丁", node_type="character")
    b = mm.add_character(name="示例角色己", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=a.id)
    old_path = mod.path
    old_container = Path(old_path).parent

    assert mm.move_mod(mod.mod_id, b.id)
    moved = mm.get_mod(mod.mod_id)
    assert moved.path != old_path
    assert not old_container.exists()
    assert Path(moved.path).parent.parent == mm._get_character_library(b.id)
    assert not (Path(old_path) / META_DIR).exists()  # 旧位置已随文件夹移走
    sc = read_sidecar(moved.path)  # 新位置有 sidecar
    assert sc is not None
    assert "classification" not in sc["local"]


def test_rename_syncs_sidecar_to_new_folder(mm, tmp_path):
    char = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=char.id)
    renamed = mm.rename_mod(mod.mod_id, "Delta Qipao", sync_folder=True)
    assert renamed is not None
    from src.core.mod_package import read_container_marker

    body = Path(renamed.path)
    assert body.parent.name == "Delta Qipao"
    assert body.stem == "Delta Qipao"  # 本体是压缩档，名字随容器一起改
    assert read_container_marker(body.parent)["body"] == body.name
    sc = read_sidecar(renamed.path)  # 改名后新物理目录内有 sidecar
    assert sc is not None and sc["name"] == "Delta Qipao"


def test_rewrite_preserves_unknown_metadata_fields(mm, tmp_path):
    """更新受管理字段时保留顶层和 local 中的扩展字段。"""
    import json

    char = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=char.id)
    info_path = Path(mod.path).parent / META_DIR / "info.json"

    # 未知 schema 值不阻断元数据读取，扩展字段须保留原值。
    raw = json.loads(info_path.read_text(encoding="utf-8"))
    raw["schema"] = 999
    raw["future_top"] = {"a": 1}
    raw.setdefault("local", {})["future_local"] = ["x"]
    info_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    mod.name = "新名"
    assert mm.update_mod(mod)

    sc = read_sidecar(mod.path)
    assert sc["name"] == "新名"  # 更新受管理字段。
    assert sc["schema"] == 2  # 版本号按本程序重写（只写不读）
    assert sc["future_top"] == {"a": 1}  # 顶层未知键原样保留
    assert sc["local"]["future_local"] == ["x"]  # local 段未知键原样保留
    assert "classification" not in sc["local"]


def test_move_mod_rolls_back_container_when_sidecar_write_fails(
    mm,
    tmp_path,
    monkeypatch,
):
    source = mm.add_character(name="示例角色丁", node_type="character")
    target = mm.add_character(name="示例角色己", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=source.id)
    old_path = Path(mod.path)
    old_container = old_path.parent

    monkeypatch.setattr(mm, "_write_mod_sidecar", lambda *_args, **_kwargs: False)

    assert mm.move_mod(mod.mod_id, target.id) is False
    current = mm.get_mod(mod.mod_id)
    assert Path(current.path) == old_path
    assert current.node_id == source.id
    assert old_container.is_dir()
    assert read_sidecar(old_path) is not None
    assert not (mm._get_character_library(target.id) / old_container.name).exists()


def test_rename_mod_rolls_back_container_when_sidecar_write_fails(
    mm,
    tmp_path,
    monkeypatch,
):
    char = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(str(_make_source(tmp_path, "Skin")), node_id=char.id)
    old_path = Path(mod.path)
    old_container = old_path.parent

    monkeypatch.setattr(mm, "_write_mod_sidecar", lambda *_args, **_kwargs: False)

    assert mm.rename_mod(mod.mod_id, "Broken Rename", sync_folder=True) is None
    current = mm.get_mod(mod.mod_id)
    assert Path(current.path) == old_path
    assert current.name == "Skin"
    assert old_container.is_dir()
    assert read_sidecar(old_path) is not None
    assert not (old_container.parent / "Broken Rename").exists()
