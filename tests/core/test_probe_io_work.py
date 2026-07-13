import json
import os
from pathlib import Path

import pytest


def test_marker_read_only_queries_metadata_required_by_the_platform(tmp_path, monkeypatch):
    from src.core import mod_package
    from src.utils import fs

    marker = tmp_path / ".d3dxmod"
    payload = {"kind": "example", "deployment_id": "read-exactly"}
    marker.write_text(json.dumps(payload), encoding="utf8")
    metadata_queries = []
    original = mod_package.os.stat

    def counted(path, *args, **kwargs):
        metadata_queries.append(path)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mod_package.os, "stat", counted)
        result = mod_package._read_container_marker_data(tmp_path)
    assert result == payload
    marker_queries = [Path(fs.normal_path(path)) for path in metadata_queries if Path(fs.normal_path(path)) == marker]
    assert marker_queries == ([marker] if os.name == "nt" else [])


def test_marker_permission_failure_is_not_treated_as_missing(tmp_path, monkeypatch):
    from src.core import mod_package

    (tmp_path / ".d3dxmod").write_text("{}", encoding="utf8")

    def denied(*args, **kwargs):
        raise PermissionError("marker is unreadable")

    if os.name == "nt":
        from src.core import watcher_windows

        monkeypatch.setattr(watcher_windows, "read_marker_text", denied)
    else:
        monkeypatch.setattr(mod_package.fs, "read_text", denied)
    with pytest.raises(mod_package.InvalidManagedMetadataError, match="unreadable"):
        mod_package._read_container_marker_data(tmp_path)
