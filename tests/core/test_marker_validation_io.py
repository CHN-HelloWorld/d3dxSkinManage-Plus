import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import mod_package
from src.utils import fs


@pytest.mark.parametrize("body_type", ["folder", "archive"])
def test_plain_direct_body_is_validated_without_resolving_ancestors(tmp_path, monkeypatch, body_type):
    container = tmp_path / "container"
    container.mkdir()
    body = container / ("body" if body_type == "folder" else "body.zip")
    body.mkdir() if body_type == "folder" else body.write_bytes(b"archive")
    mod_package.write_container_marker(container, body.name, "a" * 40, imported_at="2026-10-05T10:00:00")
    stats = []
    original = mod_package.os.stat

    def counted(path, *args, **kwargs):
        if not isinstance(path, int) and Path(fs.normal_path(path)) == body:
            stats.append(kwargs.get("follow_symlinks", True))
        return original(path, *args, **kwargs)

    def unexpected(*args, **kwargs):
        raise AssertionError("ordinary basename does not require parent realpath walks")

    monkeypatch.setattr(mod_package.os, "stat", counted)
    monkeypatch.setattr(mod_package.os.path, "realpath", unexpected)
    assert mod_package.read_container_marker(container)["body_type"] == body_type
    assert stats == [False]


def test_reparse_body_keeps_canonical_containment_validation(tmp_path, monkeypatch):
    container = tmp_path / "container"
    container.mkdir()
    body = container / "body"
    body.mkdir()
    mod_package.write_container_marker(container, body.name, "a" * 40, imported_at="2026-10-05T10:00:00")
    original = mod_package.os.stat

    def reparse(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if not isinstance(path, int) and Path(fs.normal_path(path)) == body and kwargs.get("follow_symlinks") is False:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    def resolved(path, *args, **kwargs):
        return str(tmp_path / "outside") if Path(fs.normal_path(path)) == body else str(container)

    monkeypatch.setattr(mod_package.os, "stat", reparse)
    monkeypatch.setattr(mod_package.os.path, "realpath", resolved)
    with pytest.raises(mod_package.InvalidManagedMetadataError, match="容器之外"):
        mod_package.read_container_marker(container)


def test_invalid_os_basename_keeps_per_container_metadata_error(tmp_path):
    container = tmp_path / "container"
    container.mkdir()
    (container / ".d3dxmod").write_text(
        json.dumps(
            dict(
                kind=mod_package.CONTAINER_KIND,
                schema=mod_package.CONTAINER_SCHEMA_VERSION,
                body="bad\x00.zip",
                body_type="archive",
                content_hash="a" * 40,
            )
        ),
        encoding="utf8",
    )
    with pytest.raises(mod_package.InvalidManagedMetadataError):
        mod_package.read_container_marker(container)
