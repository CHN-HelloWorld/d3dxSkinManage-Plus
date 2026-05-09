from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.core import mod_exporter, mod_package
from src.core.mod_exporter import ExportItem, ModExporter
from src.core.mod_identity import read_source_mod_id, write_deployment_identity
from src.core.mod_package import META_DIR, build_local_manifest, read_sidecar, write_container_marker, write_sidecar
from src.models.mod import Mod
from src.services.archive import ArchiveService

IDENTITY = "abcdefabcdef4def8defabcdefabcdef"
OTHER = "123456789abc4def8def123456789abc"


def _managed(tmp_path):
    container = tmp_path / "Managed"
    body = container / "Skin"
    body.mkdir(parents=True)
    (body / "skin.ini").write_text("[Constants]\n", encoding="utf-8")
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=IDENTITY, imported_at="2026-10-05T10:00:00"
    )
    metadata = container / META_DIR
    metadata.mkdir()
    (metadata / "info.json").write_text(
        json.dumps(
            build_local_manifest(
                {"name": "Original", "info": {"author": "Author"}},
                {"mod_id": IDENTITY, "classification": ["Original"]},
            )
        ),
        encoding="utf-8",
    )
    return body


def test_sidecar_rewrite_preserves_identity_when_replacing_classification(temp_data_dir, tmp_path):
    body = _managed(tmp_path)
    assert write_sidecar(
        body,
        build_local_manifest(
            {"name": "Renamed", "info": {}},
            {"classification": ["Moved"]},
        ),
    )
    manifest = read_sidecar(body)
    assert manifest["name"] == "Renamed"
    assert manifest["local"] == {"classification": ["Moved"]}
    assert read_source_mod_id(body) == IDENTITY


@pytest.mark.parametrize("replacement", [OTHER, None, "", "invalid"])
def test_sidecar_rewrite_cannot_replace_or_clear_identity(temp_data_dir, tmp_path, replacement):
    body = _managed(tmp_path)
    marker = body.parent / ".d3dxmod"
    original = marker.read_bytes()
    manifest = build_local_manifest({"name": "Renamed", "info": {}})
    manifest["local"] = {"mod_id": replacement}
    assert write_sidecar(body, manifest)
    assert "mod_id" not in read_sidecar(body).get("local", {})
    assert marker.read_bytes() == original


def test_failed_sidecar_write_keeps_previous_identity_readable(temp_data_dir, tmp_path, monkeypatch):
    body = _managed(tmp_path)
    metadata = body.parent / META_DIR / "info.json"
    original = metadata.read_bytes()

    def partial_write(path, *_args, **_kwargs):
        Path(path).write_text('{"partial"', encoding="utf-8")
        raise OSError("disk write failed")

    monkeypatch.setattr(mod_package.fs, "write_text", partial_write)
    with pytest.raises(OSError):
        write_sidecar(
            body,
            build_local_manifest(
                {"name": "Renamed", "info": {}},
                {"mod_id": IDENTITY},
            ),
        )
    assert metadata.read_bytes() == original
    assert list(metadata.parent.iterdir()) == [metadata]


@pytest.mark.parametrize("fmt", ["folder", "zip", "7z"])
@pytest.mark.parametrize("include_meta", [False, True])
def test_sharing_removes_identity_from_root_and_nested_metadata(
    temp_data_dir,
    tmp_path,
    fmt,
    include_meta,
):
    source = tmp_path / "Source"
    source.mkdir()
    (source / "skin.ini").write_text("[Constants]\n", encoding="utf-8")
    for folder in (source, source / "Nested"):
        meta = folder / META_DIR
        meta.mkdir(parents=True)
        (meta / "info.json").write_text(
            json.dumps(
                build_local_manifest(
                    {"name": "Local", "info": {}},
                    {"mod_id": IDENTITY},
                )
            ),
            encoding="utf-8",
        )
        write_deployment_identity(folder, IDENTITY, "hash")
    result = ModExporter().export_one(
        ExportItem(
            name="Shared",
            src_path=str(source),
            meta={
                "name": "Share name",
                "info": {"author": "Author"},
                "local": {"mod_id": IDENTITY},
            },
        ),
        tmp_path / "out",
        fmt=fmt,
        include_meta=include_meta,
    )
    assert result.ok, result.error
    output = Path(result.out_path)
    if output.is_file():
        output = Path(ArchiveService().extract(output, tmp_path / "inspect"))
    metadata_files = list(output.rglob("info.json"))
    assert len(metadata_files) == int(include_meta)
    for path in metadata_files:
        assert "local" not in json.loads(path.read_text(encoding="utf-8"))
        assert IDENTITY not in path.read_text(encoding="utf-8")
    assert list(output.rglob(".d3dxmod")) == []
    assert (source / ".d3dxmod").is_file()


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_sharing_aborts_and_removes_output_if_metadata_cannot_be_stripped(
    temp_data_dir,
    tmp_path,
    monkeypatch,
    failure,
):
    body = _managed(tmp_path)

    def cannot_strip(_path):
        if failure == "exception":
            raise OSError("metadata locked")
        return False

    monkeypatch.setattr(mod_exporter, "strip_sidecar", cannot_strip)
    output = tmp_path / "out"
    result = ModExporter().export_one(ExportItem(name="Shared", src_path=str(body)), output, fmt="folder")
    assert not result.ok
    assert not any(output.iterdir())
    assert read_source_mod_id(body) == IDENTITY


def test_full_container_archive_retains_identity(temp_data_dir, tmp_path):
    body = _managed(tmp_path)
    archived = ArchiveService().compress(body.parent, tmp_path / "full-backup", "zip")
    assert archived is not None
    with zipfile.ZipFile(archived) as backup:
        member = next(name for name in backup.namelist() if name.endswith(".d3dxmod"))
        assert json.loads(backup.read(member))["mod_id"] == IDENTITY
    assert read_source_mod_id(body) == IDENTITY
