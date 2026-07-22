from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.core.mod_package import (
    MANIFEST_KIND,
    META_DIR,
    SCHEMA_VERSION,
    InvalidManagedMetadataError,
    append_envelope_to_zip,
    build_local_manifest,
    read_sidecar,
    write_container_marker,
    write_envelope_to_folder,
    write_sidecar,
)


@pytest.mark.parametrize(
    "references",
    [
        None,
        7,
        {},
        "image.png",
        [None],
        [{}],
        [{"scope": [], "path": "image.png"}],
        [{"scope": "other", "path": "image.png"}],
        [{"scope": "container", "path": "../escape.png"}],
        [{"scope": "node", "path": "C:/escape.png"}],
        [{"scope": "preview_cache", "path": None}],
    ],
)
def test_preview_reference_shape_is_rejected_at_all_boundaries(tmp_path, references):
    from src.core.mod_package import restore_preview_references

    container, body = _folder_container(tmp_path)
    assert write_sidecar(body, _valid_manifest())
    info = container / META_DIR / "info.json"
    original = info.read_bytes()
    malformed = {**_valid_manifest(), "local": {"preview_references": references}}
    with pytest.raises(InvalidManagedMetadataError):
        write_sidecar(body, malformed)
    assert info.read_bytes() == original
    info.write_text(json.dumps(malformed), encoding="utf8")
    with pytest.raises(InvalidManagedMetadataError):
        read_sidecar(body)
    with pytest.raises(InvalidManagedMetadataError):
        restore_preview_references(malformed, [], container=container, preview_cache=tmp_path / "cache")


def _meta():
    return {
        "name": "示例角色丁·旗袍",
        "info": {
            "author": "SampleAuthor",
            "age_rating": "X",
            "tags": ["旗袍", "4K"],
            "custom_data": {"gamebanana": {"mod_id": 12345}},
        },
    }


def _local():
    return {
        "classification": ["示例游戏甲", "示例角色丁"],
        "preview_order": ["01.png", "sub/02.png"],
        "status_hint": "enabled",
    }


def _folder_container(tmp_path: Path) -> tuple[Path, Path]:
    container = tmp_path / "FolderContainer"
    body = container / "FolderBody"
    body.mkdir(parents=True)
    (body / "x.ini").write_text("[K]\n", encoding="utf-8")
    assert write_container_marker(container, body.name, "a" * 40, imported_at="2026-10-05T10:00:00")
    return container, body


def _archive_container(tmp_path: Path) -> tuple[Path, Path]:
    container = tmp_path / "ArchiveContainer"
    container.mkdir()
    body = container / "ArchiveBody.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr("Top/a.ini", "[K]\n")
    assert write_container_marker(container, body.name, "b" * 40, imported_at="2026-10-05T10:00:00")
    return container, body


def _valid_manifest() -> dict:
    return build_local_manifest(_meta(), _local())


def test_schema_is_current():
    manifest = _valid_manifest()
    assert SCHEMA_VERSION == 2
    assert manifest["schema"] == 2
    assert manifest["kind"] == MANIFEST_KIND
    assert manifest["previews"] == []
    assert manifest["local"]["classification"] == ["示例游戏甲", "示例角色丁"]
    assert "local" not in build_local_manifest(_meta(), None)


def test_folder_body_roundtrip_uses_container_level_only(tmp_path):
    container, body = _folder_container(tmp_path)

    assert write_sidecar(body, _valid_manifest())
    assert (container / META_DIR / "info.json").is_file()
    assert not (body / META_DIR).exists()

    manifest = read_sidecar(body)
    assert manifest is not None
    assert manifest["name"] == "示例角色丁·旗袍"
    assert manifest["info"]["age_rating"] == "X"
    assert manifest["local"]["preview_order"] == ["01.png", "sub/02.png"]


def test_archive_body_roundtrip_never_rewrites_archive(tmp_path):
    container, body = _archive_container(tmp_path)
    original = body.read_bytes()

    assert write_sidecar(body, _valid_manifest())

    assert body.read_bytes() == original
    assert (container / META_DIR / "info.json").is_file()
    assert read_sidecar(body)["name"] == "示例角色丁·旗袍"


def test_plain_folder_share_envelope_is_not_a_local_sidecar(tmp_path):
    body = tmp_path / "PlainFolder"
    body.mkdir()
    (body / "x.ini").write_text("[K]\n", encoding="utf-8")
    assert write_envelope_to_folder(body, _meta())
    original = (body / META_DIR / "info.json").read_bytes()

    assert read_sidecar(body) is None
    with pytest.raises(InvalidManagedMetadataError):
        write_sidecar(body, _valid_manifest())
    assert (body / META_DIR / "info.json").read_bytes() == original


def test_standalone_zip_share_envelope_is_not_a_local_sidecar(tmp_path):
    body = tmp_path / "PlainArchive.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr("Top/a.ini", "[K]\n")
    assert append_envelope_to_zip(body, _meta())
    original = body.read_bytes()

    assert read_sidecar(body) is None
    with pytest.raises(InvalidManagedMetadataError):
        write_sidecar(body, _valid_manifest())
    assert body.read_bytes() == original


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"schema": 2, "kind": "wrong", "name": "n", "info": {}, "previews": []},
        {"schema": 2, "name": "n", "info": {}, "previews": []},
        {"schema": 2, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": ["../secret.png"]},
        {
            "schema": 2,
            "kind": MANIFEST_KIND,
            "name": "n",
            "info": {},
            "previews": [],
            "local": {"preview_references": [{"scope": "container", "path": "../secret.png"}]},
        },
    ],
)
def test_read_sidecar_rejects_invalid_current_manifest(tmp_path, manifest):
    container, body = _folder_container(tmp_path)
    meta_dir = container / META_DIR
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(InvalidManagedMetadataError):
        read_sidecar(body)


@pytest.mark.parametrize(
    ("manifest", "expect"),
    [
        ({"schema": 1, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": []}, {"name": "n"}),
        ({"schema": True, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": []}, {"name": "n"}),
        ({"schema": 999, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": []}, {"name": "n"}),
        ({"kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": []}, {"name": "n"}),
        ({"schema": 2, "kind": MANIFEST_KIND, "info": {}, "previews": []}, {"name": ""}),
        ({"schema": 2, "kind": MANIFEST_KIND, "name": 1, "info": {}, "previews": []}, {"name": ""}),
        ({"schema": 2, "kind": MANIFEST_KIND, "name": "n", "info": [], "previews": []}, {"info": {}}),
        ({"schema": 2, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": "x"}, {"previews": []}),
        (
            {"schema": 2, "kind": MANIFEST_KIND, "name": "n", "info": {}, "previews": [], "local": "示例角色丁"},
            {"local": {}},
        ),
        (
            {
                "schema": 2,
                "kind": MANIFEST_KIND,
                "name": "n",
                "info": {},
                "previews": [],
                "local": {"classification": "示例角色丁", "status_hint": "enabled"},
            },
            {"local": {"classification": "示例角色丁", "status_hint": "enabled"}},
        ),
        (
            {
                "schema": 2,
                "kind": MANIFEST_KIND,
                "name": "n",
                "info": {},
                "previews": [],
                "local": {"preview_order": "01.png", "status_hint": "banana"},
            },
            {"local": {"preview_order": "01.png", "status_hint": "banana"}},
        ),
    ],
)
def test_read_sidecar_defaults_instead_of_discarding(tmp_path, manifest, expect):
    container, body = _folder_container(tmp_path)
    meta_dir = container / META_DIR
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    got = read_sidecar(body)
    assert got is not None
    for key, value in expect.items():
        assert got[key] == value


def test_read_sidecar_keeps_unknown_fields(tmp_path):
    container, body = _folder_container(tmp_path)
    meta_dir = container / META_DIR
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(
            {
                "schema": 999,
                "kind": MANIFEST_KIND,
                "name": "n",
                "info": {},
                "previews": [],
                "future_top": {"a": 1},
                "local": {"status_hint": "enabled", "future_local": ["x"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    got = read_sidecar(body)
    assert got["future_top"] == {"a": 1}
    assert got["local"]["future_local"] == ["x"]


def test_write_sidecar_rejects_invalid_manifest_without_replacing_current(tmp_path):
    container, body = _folder_container(tmp_path)
    assert write_sidecar(body, _valid_manifest())
    info_path = container / META_DIR / "info.json"
    original = info_path.read_bytes()
    invalid = {**_valid_manifest(), "kind": "wrong"}

    with pytest.raises(InvalidManagedMetadataError):
        write_sidecar(body, invalid)

    assert info_path.read_bytes() == original
