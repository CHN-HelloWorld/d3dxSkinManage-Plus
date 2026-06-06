from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from src.core.mod_exporter import ExportItem, ModExporter
from src.core.mod_intake import IntakeError, ModIntakeService
from src.core.mod_manager import ModManager
from src.core.mod_package import MANIFEST_KIND, META_DIR, read_container_marker
from src.core.mod_variant_store import ModVariantStore
from src.models.mod import Mod
from src.models.mod_variant import ModVariant, VariantVariable
from src.services.archive import PY7ZR_AVAILABLE, ArchiveService, find_winrar_rar_exe, rar_compress_available
from src.utils import fs
from tests.helpers import register_games, reset_game_service

EXPLICIT_INI = "namespace = Portable\n[Constants]\nglobal persist $color = 0\nglobal persist $pattern = 1\n"
IMPLICIT_INI = "[Constants]\nglobal persist $hat = 1\n"


@pytest.fixture
def library(qapp, temp_data_dir):
    register_games("portable")
    manager = ModManager()
    manager.set_game("portable")
    manager._config.set("import.format", "zip")
    node = manager.add_character(name="Character", node_type="character")
    yield manager, node
    manager.stop_ini_sync()
    reset_game_service()


def _body(path: Path, payload=b"body") -> Path:
    (path / "extras").mkdir(parents=True)
    (path / "mod.ini").write_text(EXPLICIT_INI, encoding="utf-8")
    (path / "extras" / "hat.ini").write_text(IMPLICIT_INI, encoding="utf-8")
    (path / "body.buf").write_bytes(payload)
    return path


def _variant(identity="white", name="White", value="2"):
    return ModVariant(
        identity,
        name,
        (
            VariantVariable("mod.ini", "color", "explicit", value, "portable"),
            VariantVariable("mod.ini", "pattern", "explicit", "3", "portable"),
            VariantVariable("extras/hat.ini", "hat", "implicit", "0"),
        ),
        created_at="2026-10-19T10:00:00",
        updated_at="2026-10-19T11:00:00",
    )


def _saved_mod(library, tmp_path):
    manager, node = library
    source = _body(tmp_path / "Original")
    mod = manager.import_mod(str(source), node.id)
    assert mod is not None
    store = ModVariantStore(mod.path)
    store.save(_variant())
    store.save(_variant("black", "Black", "4"))
    return mod, store


def _intake(library, source, **kwargs):
    manager, node = library
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source, game_id="portable", node_id=node.id, external_copy=True, **kwargs
    )
    return service, request


def _share(path: Path, variants=None, *, payload=b"body") -> Path:
    _body(path, payload)
    variants = [_variant().to_dict()] if variants is None else variants
    meta = path / META_DIR
    (meta / "variants").mkdir(parents=True)
    manifest = {"kind": MANIFEST_KIND, "schema": 2, "name": "Shared", "info": {}, "previews": [], "variants": []}
    for value in variants:
        relative = f"variants/{value['variant_id']}.json"
        (meta / relative).write_text(json.dumps(value), encoding="utf-8")
        manifest["variants"].append(relative)
    (meta / "info.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _files(path: Path) -> dict[str, bytes]:
    if path.is_file():
        return {path.name: path.read_bytes()}
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def _archive(folder: Path, tmp_path: Path, fmt: str, *, flat=False) -> Path:
    if fmt == "7z" and not PY7ZR_AVAILABLE:
        pytest.skip("py7zr is unavailable")
    if fmt == "rar" and not rar_compress_available():
        pytest.skip("WinRAR creation is unavailable")
    if fmt == "zip" and flat:
        archive = tmp_path / "flat.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            for rel, data in _files(folder).items():
                stream.writestr(rel, data)
        return archive
    result = ArchiveService().compress(folder, tmp_path / "package", fmt)
    assert result is not None and Path(result).suffix == "." + fmt
    return Path(result)


def _single_member_archive(tmp_path: Path, fmt: str, *, is_directory=False) -> Path:
    member = tmp_path / "main.ini"
    if is_directory:
        member.mkdir()
    else:
        member.write_text(EXPLICIT_INI, encoding="utf-8")
    archive = tmp_path / f"Single.{fmt}"
    if fmt == "zip":
        with zipfile.ZipFile(archive, "w") as stream:
            stream.write(member, member.name)
    elif fmt == "7z":
        py7zr = pytest.importorskip("py7zr")
        with py7zr.SevenZipFile(archive, "w") as stream:
            stream.write(member, member.name)
    else:
        executable = find_winrar_rar_exe()
        if executable is None:
            pytest.skip("WinRAR creation is unavailable")
        subprocess.run(
            [executable, "a", "-ep1", "-r", "-idq", str(archive), member.name],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return archive


@pytest.mark.parametrize("fmt", ["folder", "zip", "7z", "rar"])
def test_case_alias_variant_envelope_cannot_bypass_archive_validation(library, tmp_path, fmt):
    source = _share(tmp_path / "CaseAlias")
    intermediate = source / "metadata_rename"
    (source / META_DIR).rename(intermediate)
    alias = source / META_DIR.upper()
    intermediate.rename(alias)
    (alias / "private_state.json").write_text('{"private":"saved-selection"}', encoding="utf-8")
    archive = source if fmt == "folder" else _archive(source, tmp_path, fmt)
    before = _files(archive)
    service, request = _intake(library, archive)

    with pytest.raises(IntakeError, match="元数据|变体"):
        service.prepare(request)
    assert _files(archive) == before
    assert library[0].get_mods(library[1].id) == []


@pytest.mark.parametrize("fmt", ["folder", "zip", "7z", "rar"])
def test_metadata_off_export_strips_case_alias_reserved_members(library, tmp_path, fmt):
    source = _share(tmp_path / "CaseAlias")
    intermediate = source / "metadata_rename"
    (source / META_DIR).rename(intermediate)
    intermediate.rename(source / META_DIR.upper())
    (source / META_DIR.upper() / "private_state.json").write_text('{"private":true}', encoding="utf-8")
    (source / ".D3DXMOD").write_text("local identity", encoding="utf-8")
    if fmt == "folder":
        # 输入为普通分享目录，导出器不将标记内容视作受管容器身份。
        (source / ".D3DXMOD").unlink()
        export_source = source
    else:
        export_source = _archive(source, tmp_path, fmt)
    result = ModExporter().export_one(
        ExportItem("Reshared", str(export_source)), tmp_path / "out", fmt=fmt, include_meta=False
    )
    assert result.ok, result.error
    output = Path(result.out_path)
    names = _files(output) if output.is_dir() else ArchiveService().list_contents(output)
    assert not any(
        META_DIR in name.replace("\\", "/").casefold().split("/")
        or ".d3dxmod" in name.replace("\\", "/").casefold().split("/")
        for name in names
    )


def test_rebuild_preserves_unknown_identity_when_variant_directory_is_inaccessible(library, tmp_path, monkeypatch):
    mod, store = _saved_mod(library, tmp_path)
    manager, node = library
    assert manager._index.delete_mod(mod.mod_id)
    marker_path = Path(mod.path).parent / ".d3dxmod"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.pop("mod_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = marker_path.read_bytes()
    variant_dir = store.path_for("white").parent.absolute()
    original = os.lstat

    def deny_variants(path, *args, **kwargs):
        if Path(fs.normal_path(path)).absolute() == variant_dir:
            raise PermissionError("variant directory cannot be inspected")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", deny_variants)
        report = manager.rebuild_index_from_filesystem()
    assert report.invalid_metadata
    assert marker_path.read_bytes() == before
    assert manager.get_mods(node.id) == []
    assert {variant.variant_id for variant in store.list()} == {"white", "black"}


@pytest.mark.parametrize("duplicate", ["exact", "case-alias"])
def test_archive_duplicate_ini_is_rejected_before_extraction(library, tmp_path, duplicate):
    source = _share(tmp_path / "DuplicateIni")
    archive = _archive(source, tmp_path, "zip")
    name = source.name + ("/MOD.ini" if duplicate == "case-alias" else "/mod.ini")
    with zipfile.ZipFile(archive, "a") as stream:
        if duplicate == "exact":
            with pytest.warns(UserWarning, match="Duplicate name"):
                stream.writestr(name, EXPLICIT_INI.replace("$color = 0", "$color = 9"))
        else:
            stream.writestr(name, EXPLICIT_INI.replace("$color = 0", "$color = 9"))
    before = archive.read_bytes()
    service, request = _intake(library, archive)

    with pytest.raises(IntakeError, match="重复|冲突"):
        service.prepare(request)
    assert archive.read_bytes() == before
    assert library[0].get_mods(library[1].id) == []


@pytest.mark.parametrize("fmt", ["folder", "zip"])
def test_manifest_only_nested_envelope_cannot_hide_missing_variants(library, tmp_path, fmt):
    manifest = {"kind": MANIFEST_KIND, "variants": ["variants/missing.json"]}
    if fmt == "zip":
        archive = tmp_path / "ManifestOnly.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("mod.ini", EXPLICIT_INI)
            stream.writestr("Other/.d3dxmeta/info.json", json.dumps(manifest))
    else:
        archive = _body(tmp_path / "ManifestOnly")
        nested_meta = archive / "Other" / META_DIR
        nested_meta.mkdir(parents=True)
        (nested_meta / "info.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = _files(archive)
    service, request = _intake(library, archive)

    with pytest.raises(IntakeError, match="变体|资源根目录"):
        service.prepare(request)
    assert _files(archive) == before
    assert library[0].get_mods(library[1].id) == []


def test_folder_rejects_complete_variant_envelope_under_another_resource_root(library, tmp_path):
    source = _body(tmp_path / "AmbiguousRoot")
    _share(source / "Other")
    before = _files(source)
    service, request = _intake(library, source)
    with pytest.raises(IntakeError, match="变体|资源根目录"):
        service.prepare(request)
    assert _files(source) == before
    assert library[0].get_mods(library[1].id) == []


@pytest.mark.parametrize("fmt", ["folder", "zip"])
def test_manifest_only_duplicate_field_is_not_downgraded(library, tmp_path, fmt):
    source = _body(tmp_path / "DuplicateField")
    metadata = source / META_DIR
    metadata.mkdir()
    (metadata / "info.json").write_text(
        '{"kind":' + json.dumps(MANIFEST_KIND) + ',"variants":["variants/missing.json"],"variants":[]}',
        encoding="utf-8",
    )
    source = source if fmt == "folder" else _archive(source, tmp_path, fmt)
    before = _files(source)
    service, request = _intake(library, source)

    with pytest.raises(IntakeError, match="变体|重复"):
        service.prepare(request)
    assert _files(source) == before
    assert library[0].get_mods(library[1].id) == []


@pytest.mark.parametrize("fmt", ["folder", "zip"])
@pytest.mark.parametrize("declares_variants", [False, True])
def test_bad_description_encoding_degrades_only_without_variant_evidence(library, tmp_path, fmt, declares_variants):
    source = _body(tmp_path / "BadDescriptionEncoding")
    metadata = source / META_DIR
    metadata.mkdir()
    manifest = {"kind": MANIFEST_KIND, "name": "Shared", "info": {"author": "作者"}, "previews": []}
    if declares_variants:
        manifest["variants"] = ["variants/missing.json"]
    (metadata / "info.json").write_bytes(json.dumps(manifest, ensure_ascii=False).encode("gbk"))
    source = source if fmt == "folder" else _archive(source, tmp_path, fmt)
    before = _files(source)
    service, request = _intake(library, source)

    if declares_variants:
        with pytest.raises(IntakeError, match="变体"):
            service.prepare(request)
    else:
        prepared = service.prepare(request)
        assert prepared.metadata_notice
        imported = service.commit(prepared)
        assert not imported.info.author
        assert ModVariantStore(imported.path).list() == []
    assert _files(source) == before


def test_container_duplicate_ini_is_rejected_before_strict_transcode(library, tmp_path):
    pytest.importorskip("py7zr")
    mod, _store = _saved_mod(library, tmp_path)
    source = tmp_path / "DuplicateContainer"
    shutil.copytree(Path(mod.path).parent, source)
    body = source / Path(mod.path).name
    with zipfile.ZipFile(body, "a") as stream:
        name = next(name for name in stream.namelist() if name.endswith("/mod.ini") or name == "mod.ini")
        with pytest.warns(UserWarning, match="Duplicate name"):
            stream.writestr(name, EXPLICIT_INI.replace("$color = 0", "$color = 9"))
    before = _files(source)
    manager, node = library
    manager._config.set("import.format", "7z")
    manager._config.set("import.strict_format", True)
    service, request = _intake(library, source)

    with pytest.raises(IntakeError, match="重复|冲突"):
        service.prepare(request)
    assert _files(source) == before
    assert [item.mod_id for item in manager.get_mods(node.id)] == [mod.mod_id]


@pytest.mark.parametrize("fmt", ["folder", "zip", "7z", "rar"])
def test_export_metadata_roundtrip_restores_all_variants_with_new_ids(library, tmp_path, fmt):
    if fmt == "7z" and not PY7ZR_AVAILABLE:
        pytest.skip("py7zr is unavailable")
    if fmt == "rar" and not rar_compress_available():
        pytest.skip("WinRAR creation is unavailable")
    mod, store = _saved_mod(library, tmp_path)
    before = _files(Path(mod.path).parent)
    # 空描述元数据不能阻止保存的变体导出。
    result = ModExporter().export_one(
        ExportItem("Shared", mod.path), tmp_path / "out", fmt=fmt, strict_format=True, include_meta=True
    )
    assert result.ok, result.error
    output = Path(result.out_path)
    if output.is_dir():
        names = _files(output)
        manifest = json.loads(names[".d3dxmeta/info.json"])
    else:
        archive = ArchiveService()
        names = archive.list_contents(output)
        manifest_name = next(name for name in names if name.replace("\\", "/").endswith(".d3dxmeta/info.json"))
        manifest = json.loads(archive.read_file(output, manifest_name))
        names = [name.replace("\\", "/") for name in names]
    assert set(manifest["variants"]) == {"variants/white.json", "variants/black.json"}
    assert sum("/.d3dxmeta/variants/" in "/" + name for name in names) == 2
    service, request = _intake(library, output)
    imported = service.commit(service.prepare(request))
    restored = ModVariantStore(imported.path).list()
    assert {v.name for v in restored} == {"White", "Black"}
    assert {v.variant_id for v in restored}.isdisjoint({"white", "black"})
    assert imported.mod_id != mod.mod_id
    assert imported.content_hash == mod.content_hash
    assert not imported.is_enabled and not imported.deployed_path
    for variant in restored:
        assert [(v.ini_path, v.variable, v.namespace_mode, v.namespace, v.value) for v in variant.variables] == [
            ("mod.ini", "color", "explicit", "portable", "2" if variant.name == "White" else "4"),
            ("mod.ini", "pattern", "explicit", "portable", "3"),
            ("extras/hat.ini", "hat", "implicit", "", "0"),
        ]
        assert variant.created_at == "2026-10-19T10:00:00"
    assert _files(Path(mod.path).parent) == before


@pytest.mark.parametrize("include_meta", [False, True])
def test_export_never_copies_private_container_state(library, tmp_path, include_meta):
    mod, store = _saved_mod(library, tmp_path)
    container = Path(mod.path).parent
    private = container / META_DIR / "update_work" / "recovery"
    private.mkdir(parents=True)
    (private / "mod_preferences.json").write_text('{"secret":"selected"}', encoding="utf-8")
    result = ModExporter().export_one(
        ExportItem("Shared", str(container), meta={"name": "Shared", "info": {}}),
        tmp_path / "out",
        fmt="folder",
        include_meta=include_meta,
    )
    assert result.ok, result.error
    names = _files(Path(result.out_path))
    assert not any("update_work" in name or name.endswith(".d3dxmod") for name in names)
    assert not any(b"secret" in data for data in names.values())
    assert bool([name for name in names if "/variants/" in name]) is include_meta
    assert len(store.list()) == 2


@pytest.mark.parametrize("kind", ["folder", "zip-root", "zip-top", "7z", "rar"])
def test_external_package_import_renames_duplicate_names_without_applying(library, tmp_path, kind):
    first = _variant().to_dict()
    second = _variant("other", " WHITE ", "7").to_dict()
    source = _share(tmp_path / "Shared", [first, second])
    if kind != "folder":
        source = _archive(source, tmp_path, "zip" if kind.startswith("zip") else kind, flat=kind == "zip-root")
    original = _files(source)
    service, request = _intake(library, source)
    imported = service.commit(service.prepare(request))
    variants = ModVariantStore(imported.path).list()
    assert {v.name for v in variants} == {"White", "WHITE (2)"}
    assert {v.variant_id for v in variants}.isdisjoint({"white", "other"})
    assert not imported.is_enabled and not imported.deployed_path
    assert _files(source) == original


@pytest.mark.parametrize("kind", ["folder", "zip", "7z", "rar"])
def test_external_copy_of_local_container_keeps_variants_without_share_list(library, tmp_path, kind):
    mod, store = _saved_mod(library, tmp_path)
    source = tmp_path / "Copied"
    shutil.copytree(Path(mod.path).parent, source)
    manifest = json.loads((source / META_DIR / "info.json").read_text(encoding="utf-8"))
    assert "variants" not in manifest
    if kind != "folder":
        source = _archive(source, tmp_path, kind)
    service, request = _intake(library, source)
    imported = service.commit(service.prepare(request))
    assert imported.content_hash == mod.content_hash
    assert imported.mod_id != mod.mod_id
    variants = ModVariantStore(imported.path).list()
    assert {v.name for v in variants} == {"White", "Black"}
    assert {v.variant_id for v in variants}.isdisjoint({"white", "black"})


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "bad-json",
        "bad-id",
        "infinite",
        "missing-variable",
        "extra-variable",
        "wrong-namespace",
        "wrong-path",
        "duplicate-list",
        "invalid-list",
        "traversal",
        "absolute",
        "duplicate-json-key",
    ],
)
def test_declared_variant_damage_rejects_whole_intake_and_preserves_source(library, tmp_path, damage):
    source = _share(tmp_path / "Broken")
    meta = source / META_DIR
    file = meta / "variants" / "white.json"
    variant = json.loads(file.read_text(encoding="utf-8"))
    manifest = json.loads((meta / "info.json").read_text(encoding="utf-8"))
    if damage == "missing":
        file.unlink()
    elif damage == "bad-json":
        file.write_text("{broken", encoding="utf-8")
    elif damage == "duplicate-json-key":
        duplicated = file.read_text(encoding="utf-8").replace('"name": "White"', '"name": "White", "name": "Other"')
        file.write_text(duplicated, encoding="utf-8")
    elif damage == "duplicate-list":
        manifest["variants"] *= 2
    elif damage == "invalid-list":
        manifest["variants"] = "variants/white.json"
    elif damage in ("traversal", "absolute"):
        manifest["variants"] = ["../variants/white.json" if damage == "traversal" else "C:/private/white.json"]
    else:
        if damage == "bad-id":
            variant["variant_id"] = "foreign"
        elif damage == "infinite":
            variant["variables"][0]["value"] = "1e999"
        elif damage == "missing-variable":
            variant["variables"].pop()
        elif damage == "extra-variable":
            variant["variables"].append(
                {
                    "ini_path": "mod.ini",
                    "variable": "other",
                    "namespace_mode": "explicit",
                    "namespace": "portable",
                    "value": "1",
                }
            )
        elif damage == "wrong-namespace":
            variant["variables"][0]["namespace"] = "another"
        elif damage == "wrong-path":
            variant["variables"][0]["ini_path"] = "../mod.ini"
        file.write_text(json.dumps(variant), encoding="utf-8")
    (meta / "info.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = _files(source)
    manager, node = library
    service, request = _intake(library, source, move_source=True)
    with pytest.raises((IntakeError, ValueError), match="变体"):
        service.prepare(request)
    assert _files(source) == before
    assert manager.get_mods(node.id) == []
    assert not list(manager._get_character_library(node.id).glob("*/.d3dxmod"))


def test_import_save_failure_restores_complete_moved_container(library, tmp_path, monkeypatch):
    source = _share(tmp_path / "Source", [_variant().to_dict(), _variant("black", "Black").to_dict()])
    before = _files(source)
    original_save = ModVariantStore.save
    saves = []

    def save(store, variant):
        saves.append(variant.variant_id)
        if len(saves) == 2:
            raise OSError("second variant write failed")
        return original_save(store, variant)

    monkeypatch.setattr(ModVariantStore, "save", save)
    service, request = _intake(library, source, move_source=True)
    with pytest.raises(OSError, match="second variant"):
        service.prepare(request)
    assert _files(source) == before
    manager, node = library
    assert not manager.get_mods(node.id)
    assert not list(manager._get_character_library(node.id).glob("*/.d3dxmod"))


def test_commit_rechecks_prepared_variant_files_before_index_publication(library, tmp_path):
    source = _share(tmp_path / "Source")
    service, request = _intake(library, source)
    prepared = service.prepare(request)
    store = ModVariantStore(prepared.body_path)
    variants = store.list()
    assert len(variants) == 1
    store.path_for(variants[0].variant_id).unlink()
    with pytest.raises((IntakeError, OSError, ValueError)):
        service.commit(prepared)
    assert not prepared.container_path.exists()
    assert not library[0].get_mods(library[1].id)


def test_export_member_write_failure_removes_partial_product(library, tmp_path, monkeypatch):
    mod, store = _saved_mod(library, tmp_path)
    before = _files(Path(mod.path).parent)
    original = zipfile.ZipFile.writestr

    def write(stream, name, data, *args, **kwargs):
        if "/variants/" in str(name):
            raise OSError("variant member failed")
        return original(stream, name, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", write)
    output = tmp_path / "out"
    result = ModExporter().export_one(ExportItem("Shared", mod.path), output, include_meta=True)
    assert not result.ok and result.error
    assert not list(output.iterdir())
    assert _files(Path(mod.path).parent) == before


def _update(library, tmp_path, variants=None):
    from src.core.mod_update_transaction import make_update_plan
    from src.core.mod_update_worker import prepare_update

    old, store = _saved_mod(library, tmp_path)
    source = _share(tmp_path / "Update", variants=variants, payload=b"new body")
    archive = _archive(source, tmp_path, "zip")
    plan = make_update_plan(library[0], old.mod_id, archive, {"version": "2"})
    return old, store, plan, prepare_update(plan, lambda _event: None)


def _private_state(library, mod, tmp_path):
    from src.core import mod_runtime_prefs

    manager, node = library
    preferences = mod_runtime_prefs.RuntimePrefsStore()
    state = preferences.state_for(mod.mod_id)
    state.selected_variant_id = "white"
    state.values = {r"$\portable\color": "6"}
    path = tmp_path / "data" / "portable" / "mod_preferences.json"
    assert mod_runtime_prefs.save(path, preferences)
    preset = manager.preset_manager.create_preset("portable", "Outfit", snapshot={node.id: [mod.mod_id]})
    manager.preset_manager.set_mod_variant(preset.id, mod.mod_id, "white")
    return path, path.read_bytes(), preset.id, dict(preset.mod_variants)


@pytest.mark.parametrize("operation", ["rename", "move"])
def test_container_lifecycle_preserves_variants_and_private_state(library, tmp_path, operation):
    manager, node = library
    original, store = _saved_mod(library, tmp_path)
    identity = original.mod_id
    content_hash = original.content_hash
    original_path = Path(original.path)
    body_bytes = original_path.read_bytes()
    variant_bytes = _files(store.path_for("white").parent)
    private, private_bytes, preset_id, references = _private_state(library, original, tmp_path)

    if operation == "rename":
        updated = manager.rename_mod(identity, "Renamed", sync_folder=True)
        assert updated is not None
        assert updated.name == "Renamed"
        assert updated.node_id == node.id
    else:
        destination = manager.add_character(name="Destination", node_type="character")
        assert manager.move_mod(identity, destination.id)
        updated = manager.get_mod(identity)
        assert updated is not None
        assert updated.node_id == destination.id

    assert updated.mod_id == identity
    assert updated.content_hash == content_hash
    assert Path(updated.path) != original_path
    assert not original_path.exists()
    assert Path(updated.path).read_bytes() == body_bytes
    restored = ModVariantStore(updated.path)
    assert {variant.variant_id for variant in restored.list()} == {"white", "black"}
    assert _files(restored.path_for("white").parent) == variant_bytes
    marker = read_container_marker(Path(updated.path).parent)
    assert marker is not None and marker["mod_id"] == identity
    assert private.read_bytes() == private_bytes
    assert manager.preset_manager.get_preset(preset_id).mod_variants == references


def test_update_appends_imported_variants_and_preserves_local_bytes_and_references(library, tmp_path):
    from src.core.mod_update_transaction import cleanup_update, commit_update

    payload = [_variant().to_dict(), _variant("another", " WHITE ", "9").to_dict()]
    old, store, plan, prepared = _update(library, tmp_path, payload)
    originals = {v.variant_id: store.path_for(v.variant_id).read_bytes() for v in store.list()}
    private, private_bytes, preset_id, references = _private_state(library, old, tmp_path)
    try:
        updated = commit_update(library[0], plan, prepared)
        restored = ModVariantStore(updated.path)
        assert {v.name for v in restored.list()} == {"White", "Black", "White (2)", "WHITE (3)"}
        for identity, contents in originals.items():
            assert restored.path_for(identity).read_bytes() == contents
        incoming = [v for v in restored.list() if v.variant_id not in originals]
        assert len(incoming) == 2
        assert {v.variant_id for v in incoming}.isdisjoint({"white", "another"})
        assert updated.content_hash != old.content_hash
        assert private.read_bytes() == private_bytes
        assert library[0].preset_manager.get_preset(preset_id).mod_variants == references
    finally:
        cleanup_update(plan)


@pytest.mark.parametrize("failure", ["second-file", "metadata"])
def test_update_failure_rolls_back_appended_variants_with_resource_moves(library, tmp_path, monkeypatch, failure):
    from src.core import mod_update_transaction as transaction

    payload = [_variant().to_dict(), _variant("another", "Other", "9").to_dict()]
    old, store, plan, prepared = _update(library, tmp_path, payload)
    body = Path(old.path)
    original_body = body.read_bytes()
    original_marker = (body.parent / ".d3dxmod").read_bytes()
    original_variants = _files(body.parent / META_DIR / "variants")
    if failure == "metadata":
        monkeypatch.setattr(library[0], "_commit_mod_metadata", lambda _mod: False)
    else:
        original_replace = transaction.os.replace
        written = []

        def replace_file(source, target):
            if (
                Path(source).parent.name == "variants"
                and Path(fs.normal_path(target)).parent == store.path_for("white").parent
            ):
                written.append(str(target))
                if len(written) == 2:
                    raise OSError("second append failed")
            return original_replace(source, target)

        monkeypatch.setattr(transaction.os, "replace", replace_file)
    try:
        with pytest.raises(OSError):
            transaction.commit_update(library[0], plan, prepared)
        assert body.read_bytes() == original_body
        assert (body.parent / ".d3dxmod").read_bytes() == original_marker
        assert _files(body.parent / META_DIR / "variants") == original_variants
        assert library[0].get_mod(old.mod_id).content_hash == old.content_hash
    finally:
        transaction.cleanup_update(plan)


@pytest.mark.parametrize("collision", ["id", "name"])
def test_update_rechecks_late_local_variant_collisions(library, tmp_path, collision):
    from src.core.mod_update_transaction import UpdateContextChanged, cleanup_update, commit_update

    old, store, plan, prepared = _update(library, tmp_path)
    assert len(prepared.get("variants", [])) == 1
    staged = ModVariant.from_dict(prepared["variants"][0])
    late = replace(
        _variant(),
        variant_id=staged.variant_id if collision == "id" else "late",
        name=staged.name if collision == "name" else "Late local",
    )
    store.save(late)
    before = _files(Path(old.path).parent / META_DIR / "variants")
    body_bytes = Path(old.path).read_bytes()
    try:
        with pytest.raises(UpdateContextChanged, match="变体"):
            commit_update(library[0], plan, prepared)
        assert _files(Path(old.path).parent / META_DIR / "variants") == before
        assert Path(old.path).read_bytes() == body_bytes
    finally:
        cleanup_update(plan)


def test_invalid_update_variants_preserve_existing_local_files(library, tmp_path):
    from src.core.mod_update_transaction import cleanup_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    old, store = _saved_mod(library, tmp_path)
    variant = _variant().to_dict()
    variant["variables"].pop()
    archive = _archive(_share(tmp_path / "Update", [variant], payload=b"new"), tmp_path, "zip")
    before = _files(Path(old.path).parent)
    plan = make_update_plan(library[0], old.mod_id, archive, {})
    try:
        with pytest.raises((OSError, ValueError), match="变体"):
            prepare_update(plan, lambda _event: None)
    finally:
        cleanup_update(plan)
    assert _files(Path(old.path).parent) == before


@pytest.mark.parametrize("entry", ["plan", "commit"])
def test_direct_update_scope_blocks_competing_variant_and_preserves_async_token(library, tmp_path, monkeypatch, entry):
    from src.core import mod_update_transaction as transaction
    from src.core.library_protection import LibraryOperationBusy

    old, store, plan, prepared = _update(library, tmp_path)
    manager = library[0]
    registry = manager._library_protection
    asynchronous = "update:" + plan["job_id"]
    registry.protect(asynchronous, paths=(Path(old.path).parent,), mod_ids=(old.mod_id,))
    check = transaction._check_ownership

    def ownership(*args):
        check(*args)

        def competing_variant():
            with pytest.raises(LibraryOperationBusy):
                with registry.variant_operation("competing", mod_ids=(old.mod_id,), excluding=(asynchronous,)):
                    pass

        with ThreadPoolExecutor(1) as pool:
            pool.submit(competing_variant).result()

    monkeypatch.setattr(transaction, "_check_ownership", ownership)
    other = None
    try:
        if entry == "plan":
            other = transaction.make_update_plan(manager, old.mod_id, plan["archive"], {})
        else:
            transaction.commit_update(manager, plan, prepared)
        assert old.mod_id in registry.snapshot()[1]
    finally:
        registry.release(asynchronous)
        if other:
            transaction.cleanup_update(other)
        transaction.cleanup_update(plan)


def test_rebuild_restores_persistent_ids_without_changing_variants_or_private_state(library, tmp_path):
    old, store = _saved_mod(library, tmp_path)
    private, private_bytes, preset_id, references = _private_state(library, old, tmp_path)
    before = _files(Path(old.path).parent)
    assert library[0]._index.delete_mod(old.mod_id)
    report = library[0].rebuild_index_from_filesystem()
    assert report.registered == 1
    rebuilt = library[0].get_mod(old.mod_id)
    assert rebuilt is not None
    assert {v.variant_id for v in ModVariantStore(rebuilt.path).list()} == {"white", "black"}
    assert _files(Path(rebuilt.path).parent) == before
    assert private.read_bytes() == private_bytes
    assert library[0].preset_manager.get_preset(preset_id).mod_variants == references


def test_rebuild_reports_broken_variant_but_preserves_original_bytes(library, tmp_path):
    old, store = _saved_mod(library, tmp_path)
    file = store.path_for("white")
    file.write_bytes(b"{broken variant")
    before = _files(Path(old.path).parent)
    report = library[0].rebuild_index_from_filesystem()
    assert any("white.json" in problem for problem in report.invalid_metadata)
    assert _files(Path(old.path).parent) == before
    assert library[0].get_mod(old.mod_id) is not None


@pytest.mark.parametrize("identity", [None, "invalid"])
def test_rebuild_does_not_invent_identity_for_container_with_saved_variants(library, tmp_path, identity):
    old, store = _saved_mod(library, tmp_path)
    assert library[0]._index.delete_mod(old.mod_id)
    path = Path(old.path).parent / ".d3dxmod"
    marker = json.loads(path.read_text(encoding="utf-8"))
    marker["mod_id"] = identity
    path.write_text(json.dumps(marker), encoding="utf-8")
    before = _files(path.parent)
    report = library[0].rebuild_index_from_filesystem()
    assert report.invalid_metadata
    assert not library[0].get_mods(library[1].id)
    assert _files(path.parent) == before


@pytest.mark.parametrize("duplicate", ["exact", "case-alias"])
def test_archive_with_duplicate_variant_member_is_rejected_before_extraction(library, tmp_path, duplicate):
    source = _share(tmp_path / "Shared")
    archive = _archive(source, tmp_path, "zip")
    member = "Shared/.d3dxmeta/variants/white.json"
    if duplicate == "case-alias":
        member = "Shared/.d3dxmeta/variants/WHITE.JSON"
    with zipfile.ZipFile(archive, "a") as stream:
        if duplicate == "exact":
            with pytest.warns(UserWarning, match="Duplicate name"):
                stream.writestr(member, json.dumps(_variant(value="8").to_dict()))
        else:
            stream.writestr(member, json.dumps(_variant(value="8").to_dict()))
    before = archive.read_bytes()
    service, request = _intake(library, archive)
    with pytest.raises(IntakeError, match="变体"):
        service.prepare(request)
    assert archive.read_bytes() == before


def test_archive_cannot_hide_variant_envelope_under_ambiguous_resource_roots(library, tmp_path):
    source = _share(tmp_path / "Shared")
    archive = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for relative, data in _files(source).items():
            stream.writestr("Shared/" + relative, data)
        stream.writestr("unrelated.bin", b"another top level")
    service, request = _intake(library, archive)
    with pytest.raises(IntakeError, match="变体"):
        service.prepare(request)


def test_update_cannot_drop_declared_variants_from_worker_result(library, tmp_path):
    from src.core.mod_update_transaction import cleanup_update, commit_update

    old, store, plan, prepared = _update(library, tmp_path)
    assert prepared["variants"]
    prepared["variants"] = []
    before = Path(old.path).read_bytes()
    try:
        with pytest.raises((ValueError, OSError), match="变体"):
            commit_update(library[0], plan, prepared)
        assert Path(old.path).read_bytes() == before
    finally:
        cleanup_update(plan)


def test_update_failed_variant_rollback_retains_recovery_files(library, tmp_path, monkeypatch):
    from src.core import mod_update_transaction as transaction

    old, store, plan, prepared = _update(library, tmp_path)
    imported_id = prepared["variants"][0]["variant_id"]
    original_replace = transaction.os.replace
    monkeypatch.setattr(library[0], "_commit_mod_metadata", lambda _mod: False)

    def fail_restoring_variant(source, target):
        if Path(fs.normal_path(source)) == store.path_for(imported_id):
            raise OSError("cannot restore imported variant")
        return original_replace(source, target)

    monkeypatch.setattr(transaction.os, "replace", fail_restoring_variant)
    with pytest.raises(OSError):
        transaction.commit_update(library[0], plan, prepared)
    assert (Path(plan["work_root"]) / "recovery_required").is_file()
    transaction.cleanup_update(plan)
    assert Path(plan["work_root"]).is_dir()
    assert store.path_for("white").is_file()
    assert store.path_for(imported_id).is_file()


def test_rediscovery_preserves_variant_ids_and_rejects_unknown_mod_identity(library, tmp_path):
    old, store = _saved_mod(library, tmp_path)
    manager, node = library
    assert manager._index.delete_mod(old.mod_id)
    service, request = _intake(library, Path(old.path), context="rediscovery")
    prepared = service.prepare(request)
    restored = service.commit(prepared)
    assert restored.mod_id == old.mod_id
    assert {variant.variant_id for variant in ModVariantStore(restored.path).list()} == {"white", "black"}
    assert manager._index.delete_mod(old.mod_id)
    marker_path = Path(old.path).parent / ".d3dxmod"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.pop("mod_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = _files(marker_path.parent)
    service, request = _intake(library, Path(old.path), context="rediscovery")
    with pytest.raises((IntakeError, ValueError), match="身份"):
        service.prepare(request)
    assert _files(marker_path.parent) == before


def test_existing_container_with_malformed_variant_manifest_is_not_downgraded(library, tmp_path):
    old, store = _saved_mod(library, tmp_path)
    source = tmp_path / "Copied"
    shutil.copytree(Path(old.path).parent, source)
    (source / META_DIR / "info.json").write_text(
        '{"kind":"' + MANIFEST_KIND + '", "variants":["variants/white.json"], "variants":[]}', encoding="utf-8"
    )
    service, request = _intake(library, source)
    with pytest.raises(IntakeError, match="变体"):
        service.prepare(request)


def test_import_readback_failure_rolls_back_new_container(library, tmp_path, monkeypatch):
    source = _share(tmp_path / "Source")
    original_get = ModVariantStore.get

    def get(store, identity):
        variant = original_get(store, identity)
        return replace(variant, name="Changed while writing")

    monkeypatch.setattr(ModVariantStore, "get", get)
    service, request = _intake(library, source, move_source=True)
    before = _files(source)
    with pytest.raises(ValueError, match="变体"):
        service.prepare(request)
    assert _files(source) == before
    assert not library[0].get_mods(library[1].id)


def test_variant_file_changes_do_not_change_resource_fingerprint(library, tmp_path):
    old, store = _saved_mod(library, tmp_path)
    original = Mod.compute_sha1(old.path)
    store.save(replace(store.get("white"), name="Renamed", variables=_variant(value="5").variables))
    store.save(_variant("extra", "Extra"))
    store.delete("black")
    assert Mod.compute_sha1(old.path) == original == old.content_hash


@pytest.mark.parametrize("fmt", ["folder", "zip"])
def test_exporting_complete_container_uses_its_resource_body(library, tmp_path, fmt):
    old, store = _saved_mod(library, tmp_path)
    result = ModExporter().export_one(
        ExportItem("Shared", str(Path(old.path).parent)), tmp_path / "out", fmt=fmt, include_meta=True
    )
    assert result.ok, result.error
    service, request = _intake(library, Path(result.out_path))
    imported = service.commit(service.prepare(request))
    assert imported.content_hash == old.content_hash
    assert {v.name for v in ModVariantStore(imported.path).list()} == {"White", "Black"}


def test_unreadable_original_declarations_cannot_validate_an_empty_variant(library, tmp_path, monkeypatch):
    old, store = _saved_mod(library, tmp_path)
    store.delete("black")
    store.save(replace(store.get("white"), variables=()))
    source = tmp_path / "Copied"
    shutil.copytree(Path(old.path).parent, source)
    list_contents = ArchiveService.list_contents
    read_all = ArchiveService.read_all_inis

    def unreadable_names(service, path):
        return [] if Path(path).name == Path(old.path).name else list_contents(service, path)

    def unreadable_inis(service, path):
        return {} if Path(path).name == Path(old.path).name else read_all(service, path)

    monkeypatch.setattr(ArchiveService, "list_contents", unreadable_names)
    monkeypatch.setattr(ArchiveService, "read_all_inis", unreadable_inis)
    service, request = _intake(library, source)
    with pytest.raises(IntakeError, match="变体"):
        service.prepare(request)


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
@pytest.mark.parametrize("is_directory", [False, True])
def test_single_member_archive_extracts_a_directory(tmp_path, fmt, is_directory):
    archive = _single_member_archive(tmp_path, fmt, is_directory=is_directory)
    extracted = ArchiveService().extract(archive, tmp_path / "extracted")
    assert extracted is not None and extracted.is_dir()
    if is_directory:
        assert extracted.name == "main.ini"
        assert not list(extracted.iterdir())
    else:
        assert (extracted / "main.ini").read_text(encoding="utf-8") == EXPLICIT_INI


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
@pytest.mark.parametrize("as_folder", [False, True])
@pytest.mark.parametrize("include_meta", [False, True])
def test_single_root_ini_container_roundtrip(library, tmp_path, fmt, as_folder, include_meta):
    manager, node = library
    archive = _single_member_archive(tmp_path, fmt)
    mod = manager.import_mod(str(archive), node.id)
    assert mod is not None
    assert set(ArchiveService().list_contents(mod.path)) == {"main.ini"}
    variant = ModVariant(
        "single",
        "Single",
        (
            VariantVariable("main.ini", "color", "explicit", "2", "portable"),
            VariantVariable("main.ini", "pattern", "explicit", "3", "portable"),
        ),
    )
    ModVariantStore(mod.path).save(variant)
    before = _files(Path(mod.path).parent)
    result = ModExporter().export_one(
        ExportItem("Shared", str(Path(mod.path).parent)),
        tmp_path / "out",
        fmt="folder" if as_folder else fmt,
        include_meta=include_meta,
    )
    assert result.ok, result.error
    output = Path(result.out_path)
    extracted = output if output.is_dir() else ArchiveService().extract(output, tmp_path / "inspected")
    assert extracted is not None and extracted.is_dir()
    assert (extracted / "main.ini").read_text(encoding="utf-8") == EXPLICIT_INI
    assert (extracted / META_DIR / "variants" / "single.json").is_file() is include_meta
    service, request = _intake(library, output)
    imported = service.commit(service.prepare(request))
    assert imported.mod_id != mod.mod_id and imported.content_hash == mod.content_hash
    restored = ModVariantStore(imported.path).list()
    if include_meta:
        assert len(restored) == 1
        assert restored[0].variant_id != "single"
        assert restored[0].variables == variant.variables
    else:
        assert restored == []
    assert _files(Path(mod.path).parent) == before
