import json
import zipfile
from pathlib import Path

import pytest

from src.core import mod_package as package

IDENTITY = "abcdefabcdef4def8defabcdefabcdef"
STAMP = "2026-10-05T10:00:00"


def test_marker_keeps_first_import_time(tmp_path):
    body = tmp_path / "body.zip"
    body.write_bytes(b"payload")
    package.write_container_marker(tmp_path, body.name, "a" * 40, mod_id=IDENTITY, imported_at=STAMP)
    package.write_container_marker(tmp_path, body.name, "b" * 40, mod_id=IDENTITY, imported_at="2026-10-06T10:00:00")
    marker = package.read_container_marker(tmp_path)
    assert marker["mod_id"] == IDENTITY
    assert marker["imported_at"] == STAMP
    assert "deployment_id" not in marker


def test_local_information_and_share_whitelist_are_distinct():
    meta = {
        "info": {
            "name": "obsolete",
            "author": "Author",
            "custom_data": {"private": 42, "gamebanana": {"mod_id": 123}},
            "imported_at": STAMP,
            "mod_id": IDENTITY,
        }
    }
    local = package.build_local_manifest(meta)
    shared = package.build_manifest(meta, [])
    assert local["info"]["custom_data"]["private"] == 42
    assert "name" not in local["info"]
    assert "mod_id" not in shared["info"]
    assert "imported_at" not in shared["info"]
    assert shared["info"]["custom_data"] == {"gamebanana": {"mod_id": 123, "last_checked_iso": ""}}


@pytest.mark.parametrize("declared", [None, "gone.zip"])
def test_missing_body_recovers_only_unambiguous_candidate(tmp_path, declared):
    (tmp_path / "actual.zip").write_bytes(b"payload")
    data = {"kind": package.CONTAINER_KIND, "content_hash": "a" * 40, "body": declared}
    (tmp_path / ".d3dxmod").write_text(json.dumps(data))
    assert package.read_container_marker(tmp_path)["body"] == "actual.zip"
    (tmp_path / "second.zip").write_bytes(b"payload")
    with pytest.raises(package.InvalidManagedMetadataError):
        package.read_container_marker(tmp_path)


def test_unreadable_body_is_not_reidentified(tmp_path, monkeypatch):
    (tmp_path / "actual.zip").write_bytes(b"payload")
    data = {"kind": package.CONTAINER_KIND, "content_hash": "a" * 40, "body": "locked.zip"}
    marker = tmp_path / ".d3dxmod"
    marker.write_text(json.dumps(data))
    original = package.os.stat

    def locked(path, *args, **kwargs):
        if Path(path).name == "locked.zip":
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(package.os, "stat", locked)
    with pytest.raises(PermissionError):
        package.read_container_marker(tmp_path)
    assert json.loads(marker.read_text()) == data


@pytest.mark.parametrize("fmt", ["folder", "zip", "7z"])
@pytest.mark.parametrize("include_meta", [False, True])
def test_container_share_never_leaks_identity(tmp_path, temp_data_dir, fmt, include_meta):
    from src.core.mod_exporter import ExportItem, ModExporter
    from src.services.archive import ArchiveService

    source = tmp_path / "container"
    source.mkdir()
    with zipfile.ZipFile(source / "body.zip", "w") as archive:
        archive.writestr("main.ini", "[Constants]\nglobal persist $choice = 0\n")
    package.write_container_marker(source, "body.zip", "a" * 40, mod_id=IDENTITY, imported_at=STAMP)
    result = ModExporter().export_one(
        ExportItem(
            name="Shared",
            src_path=str(source),
            meta={"info": {"mod_id": IDENTITY, "imported_at": STAMP, "custom_data": {"gamebanana": {"mod_id": 123}}}},
        ),
        tmp_path / "output",
        fmt=fmt,
        include_meta=include_meta,
    )
    assert result.ok, result.error
    output = Path(result.out_path)
    if output.is_file():
        output = Path(ArchiveService().extract(output, tmp_path / "inspect"))
    assert not list(output.rglob(".d3dxmod"))
    for info in output.rglob("info.json"):
        text = info.read_text(encoding="utf-8")
        assert IDENTITY not in text and STAMP not in text
        assert json.loads(text)["info"]["custom_data"]["gamebanana"]["mod_id"] == 123


@pytest.mark.parametrize(
    "fmt, writer",
    [("folder", "write_envelope_to_folder"), ("zip", "append_envelope_to_zip"), ("7z", "append_envelope_to_archive")],
)
def test_export_fails_when_requested_metadata_write_fails(tmp_path, temp_data_dir, monkeypatch, fmt, writer):
    from src.core import mod_exporter

    source = tmp_path / "source"
    source.mkdir()
    (source / "file.ini").write_text("payload")
    monkeypatch.setattr(mod_exporter, writer, lambda *args: False)
    output = tmp_path / "output"
    result = mod_exporter.ModExporter().export_one(
        mod_exporter.ExportItem(name="Shared", src_path=str(source), meta={"info": {}}),
        output,
        fmt=fmt,
        include_meta=True,
    )
    assert not result.ok
    assert not list(output.iterdir())


@pytest.mark.parametrize("fmt", ["zip", "7z"])
@pytest.mark.parametrize("include_meta", [False, True])
def test_direct_archive_share_strips_local_fields(tmp_path, temp_data_dir, fmt, include_meta):
    from src.core.mod_exporter import ExportItem, ModExporter
    from src.services.archive import ArchiveService

    source = tmp_path / "source"
    source.mkdir()
    (source / "body.ini").write_text("payload")
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": IDENTITY, "imported_at": STAMP}))
    archive = ArchiveService().compress(source, tmp_path / "original", fmt)
    result = ModExporter().export_one(
        ExportItem(
            name="Shared",
            src_path=str(archive),
            meta={"info": {"mod_id": IDENTITY, "imported_at": STAMP, "author": "Author"}},
        ),
        tmp_path / "output",
        fmt=fmt,
        include_meta=include_meta,
    )
    assert result.ok, result.error
    output = Path(ArchiveService().extract(result.out_path, tmp_path / "inspect"))
    assert not list(output.rglob(".d3dxmod"))
    for info in output.rglob("info.json"):
        text = info.read_text(encoding="utf-8")
        assert IDENTITY not in text and STAMP not in text


def test_failed_marker_replace_retains_original(tmp_path, monkeypatch):
    (tmp_path / "body.zip").write_bytes(b"payload")
    package.write_container_marker(tmp_path, "body.zip", "a" * 40, mod_id=IDENTITY, imported_at=STAMP)
    marker = tmp_path / ".d3dxmod"
    original = marker.read_bytes()

    def fail(*args):
        raise OSError("disk locked")

    monkeypatch.setattr(package.os, "replace", fail)
    with pytest.raises(OSError):
        package.write_container_marker(tmp_path, "body.zip", "b" * 40, mod_id=IDENTITY, imported_at=STAMP)
    assert marker.read_bytes() == original
    assert not list(tmp_path.glob(".marker-*.tmp"))


@pytest.mark.parametrize("identity", [None, "invalid"])
def test_invalid_source_identity_can_be_repaired_by_caller(tmp_path, identity):
    (tmp_path / "body.zip").write_bytes(b"payload")
    (tmp_path / ".d3dxmod").write_text(
        json.dumps(
            {
                "kind": package.CONTAINER_KIND,
                "body": "body.zip",
                "content_hash": "a" * 40,
                "mod_id": identity,
                "imported_at": STAMP,
            }
        )
    )
    package.write_container_marker(tmp_path, "body.zip", "a" * 40, mod_id=IDENTITY, imported_at="2026-10-06T10:00:00")
    marker = package.read_container_marker(tmp_path)
    assert marker["mod_id"] == IDENTITY and marker["imported_at"] == STAMP
