from __future__ import annotations

import json
import os
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from src.core.mod_package import write_container_marker
from src.core.mod_variant_store import ModVariantStore
from src.models.mod_variant import ModVariant, VariantVariable


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            pytest.skip("当前平台不允许创建 NTFS junction")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"当前平台不允许创建目录链接: {error}")


@pytest.fixture
def store(tmp_path):
    container = tmp_path / "角色容器"
    body = container / "资源"
    body.mkdir(parents=True)
    (body / "merged.ini").write_text("[Constants]\n", encoding="utf-8")
    write_container_marker(
        container,
        body.name,
        "a" * 40,
        imported_at="2026-10-19T10:00:00",
    )
    return ModVariantStore(body)


@pytest.fixture
def white():
    return ModVariant(
        "white-id",
        "White",
        (VariantVariable("merged.ini", "swapvar", "implicit", "1"),),
        created_at="2026-10-19T10:00:00",
        updated_at="2026-10-19T10:00:00",
    )


def test_each_variant_is_an_independent_file_in_the_container(store, white):
    black = replace(white, variant_id="black-id", name="Black")
    store.save(white)
    before = store.path_for(white.variant_id).read_bytes()
    store.save(black)

    assert store.path_for("white-id").parts[-3:] == (".d3dxmeta", "variants", "white-id.json")
    assert {variant.name for variant in store.list()} == {"White", "Black"}
    assert store.path_for(white.variant_id).read_bytes() == before
    assert store.get(black.variant_id) == black
    store.delete(black.variant_id)
    assert store.get(white.variant_id) == white
    with pytest.raises(FileNotFoundError):
        store.get(black.variant_id)


def test_archive_body_uses_its_container_metadata(tmp_path, white):
    container = tmp_path / "归档容器"
    container.mkdir()
    body = container / "资源.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("merged.ini", "[Constants]\n")
    original = body.read_bytes()
    write_container_marker(
        container,
        body.name,
        "b" * 40,
        imported_at="2026-10-19T10:00:00",
    )

    store = ModVariantStore(body)
    store.save(white)

    assert store.path_for(white.variant_id) == container / ".d3dxmeta" / "variants" / "white-id.json"
    assert ModVariantStore(container).get(white.variant_id) == white
    assert body.read_bytes() == original


def test_reading_an_empty_store_does_not_create_metadata(store):
    directory = store.path_for("missing-id").parent
    assert store.list() == []
    assert not directory.exists()
    with pytest.raises(FileNotFoundError):
        store.get("missing-id")
    with pytest.raises(FileNotFoundError):
        store.delete("missing-id")
    assert not directory.exists()


def test_duplicate_names_do_not_replace_existing_variant(store, white):
    store.save(white)
    with pytest.raises(ValueError):
        store.save(replace(white, variant_id=store.new_id(), name="  WHITE  "))
    assert store.get(white.variant_id).name == "White"
    assert len(store.list()) == 1


def test_rename_trims_name_and_keeps_identity_and_created_time(store, white):
    store.save(white)
    store.save(replace(white, name="  白色  ", updated_at="2026-10-19T11:00:00"))
    saved = store.get(white.variant_id)
    assert saved.variant_id == "white-id"
    assert saved.name == "白色"
    assert saved.created_at == "2026-10-19T10:00:00"
    assert saved.updated_at == "2026-10-19T11:00:00"
    assert len(store.list()) == 1


def test_unique_name_checks_casefolded_names_and_new_ids_are_independent(store, white):
    store.save(white)
    name = store.unique_name(" WHITE ")
    assert name != "WHITE"
    store.save(replace(white, variant_id=store.new_id(), name=name))
    next_name = store.unique_name(" WHITE ")
    assert next_name.casefold() not in {variant.name.casefold() for variant in store.list()}
    assert store.unique_name(" New ") == "New"
    assert store.new_id() != store.new_id()


@pytest.mark.parametrize("variant_id", ["", "..", "../escape", r"..\escape", "/root", "C:foo", "id.json", "id\x00"])
def test_path_for_rejects_unportable_or_escaping_ids(store, variant_id):
    with pytest.raises(ValueError):
        store.path_for(variant_id)


def test_unmanaged_path_cannot_be_treated_as_a_container(tmp_path):
    with pytest.raises(ValueError):
        ModVariantStore(tmp_path)


def test_corrupt_file_and_mismatched_identity_are_not_missing(store, white):
    path = store.path_for(white.variant_id)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        store.get(white.variant_id)
    with pytest.raises(ValueError):
        store.list()
    path.write_text(json.dumps(replace(white, variant_id="someone-else").to_dict()), encoding="utf-8")
    with pytest.raises(ValueError):
        store.get(white.variant_id)


def test_duplicate_json_fields_are_rejected_without_rewriting_the_file(store):
    path = store.path_for("white-id")
    path.parent.mkdir(parents=True)
    raw = '{"kind":"d3dx-mod-variant","variant_id":"white-id","name":"White","name":"Other","variables":[]}'
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        store.get("white-id")
    assert path.read_text(encoding="utf-8") == raw


def test_failed_atomic_replace_keeps_previous_file_and_removes_only_own_temp(store, white, monkeypatch):
    store.save(white)
    path = store.path_for(white.variant_id)
    original = path.read_bytes()
    unrelated = path.parent / ".unrelated.tmp"
    unrelated.write_text("keep", encoding="utf-8")

    def fail_replace(source, target):
        assert Path(os.fspath(target)).name == "white-id.json"
        raise PermissionError("simulated locked destination")

    monkeypatch.setattr("src.core.mod_variant_store.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        store.save(replace(white, name="Changed"))

    assert path.read_bytes() == original
    assert {child.name for child in path.parent.iterdir()} == {"white-id.json", ".unrelated.tmp"}


def test_read_permission_error_propagates(store, white, monkeypatch):
    store.save(white)

    def deny_read(*args, **kwargs):
        raise PermissionError("simulated denied read")

    monkeypatch.setattr("src.core.mod_variant_store.fs.read_bytes", deny_read)
    with pytest.raises(PermissionError):
        store.get(white.variant_id)


def test_simultaneous_store_instances_cannot_create_duplicate_names(store, white):
    container = store.path_for("unused").parents[2]
    start = Barrier(4)

    def save_from_another_instance(number):
        other_store = ModVariantStore(container)
        candidate = replace(white, variant_id=f"variant-{number}")
        start.wait(timeout=5)
        try:
            other_store.save(candidate)
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save_from_another_instance, range(4)))
    assert sum(results) == 1
    assert [item.name for item in store.list()] == ["White"]


def test_optional_metadata_defaults_without_accepting_private_payloads(white):
    payload = white.to_dict()
    for key in ("created_at", "updated_at", "source_content_hash", "declaration_fingerprint"):
        payload.pop(key)
    loaded = ModVariant.from_dict(payload)
    assert loaded.created_at == loaded.updated_at == loaded.source_content_hash == loaded.declaration_fingerprint == ""
    assert loaded.to_dict()["kind"] == "d3dx-mod-variant"
    payload["preset_id"] = "private-preset"
    with pytest.raises(ValueError):
        ModVariant.from_dict(payload)


@pytest.mark.parametrize(
    "change", [{"kind": "wrong"}, {"variant_id": None}, {"name": " \t"}, {"variables": {}}, {"created_at": 123}]
)
def test_model_rejects_invalid_json_fields(white, change):
    with pytest.raises(ValueError):
        ModVariant.from_dict({**white.to_dict(), **change})


@pytest.mark.parametrize("layer", ["metadata", "variants"])
@pytest.mark.parametrize("target_kind", ["body", "outside"])
def test_metadata_directory_redirection_is_rejected_without_changing_resources(
    store, white, tmp_path, layer, target_kind
):
    container = store.path_for(white.variant_id).parents[2]
    body = container / "资源"
    before = (body / "merged.ini").read_bytes()
    target = body if target_kind == "body" else tmp_path / "outside"
    target.mkdir(exist_ok=True)
    before_files = set(target.rglob("*"))
    metadata = container / ".d3dxmeta"
    if layer == "variants":
        metadata.mkdir()
    link = metadata if layer == "metadata" else metadata / "variants"
    _link_directory(link, target)
    try:
        with pytest.raises(ValueError):
            store.save(white)
        with pytest.raises(ValueError):
            ModVariantStore(container)
        assert (body / "merged.ini").read_bytes() == before
        assert set(target.rglob("*")) == before_files
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("layer", ["metadata", "variants"])
def test_save_rechecks_directory_after_listing_before_creating_temporary_file(store, white, monkeypatch, layer):
    directory = store.path_for(white.variant_id).parent
    container = directory.parents[1]
    body = container / "资源"
    before = (body / "merged.ini").read_bytes()
    if layer == "variants":
        directory.parent.mkdir()
    link = directory.parent if layer == "metadata" else directory
    original_list = store.list

    def list_then_redirect():
        result = original_list()
        _link_directory(link, body)
        return result

    monkeypatch.setattr(store, "list", list_then_redirect)
    try:
        with pytest.raises(ValueError):
            store.save(white)
        assert (body / "merged.ini").read_bytes() == before
        assert {path.name for path in body.iterdir()} == {"merged.ini"}
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("suffix", [".JSON", ".JsOn"])
def test_json_suffix_casing_is_listed_updated_and_deleted_without_duplicate_files(store, white, suffix):
    store.save(white)
    original = store.path_for(white.variant_id)
    alternate = original.with_suffix(suffix)
    original.rename(alternate)

    assert store.list() == [white]
    assert store.get(white.variant_id) == white
    renamed = replace(white, name="Renamed")
    store.save(renamed)
    assert store.list() == [renamed]
    assert len(list(alternate.parent.iterdir())) == 1
    store.delete(white.variant_id)
    assert store.list() == []


@pytest.mark.parametrize("suffix", [".JSON", ".JsOn"])
def test_json_suffix_casing_cannot_bypass_duplicate_name_validation(store, white, suffix):
    store.save(white)
    original = store.path_for(white.variant_id)
    alternate = original.with_suffix(suffix)
    original.rename(alternate)
    before = alternate.read_bytes()

    with pytest.raises(ValueError):
        store.save(replace(white, variant_id="other-id", name=" WHITE "))
    assert alternate.read_bytes() == before
    assert len(list(alternate.parent.iterdir())) == 1


def test_uppercase_json_suffix_still_checks_the_actual_filename_identity(store, white):
    store.save(white)
    original = store.path_for(white.variant_id)
    original.rename(original.with_name("wrong-id.JSON"))
    with pytest.raises(ValueError):
        store.list()
