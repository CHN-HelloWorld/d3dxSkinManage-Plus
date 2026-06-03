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
