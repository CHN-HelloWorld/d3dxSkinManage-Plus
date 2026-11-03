"""验证部署目录展开、原子发布及短暂文件占用的处理。"""

import os
import zipfile
from pathlib import Path

import pytest

from src.core.mod_package import write_container_marker
from src.core.mod_storage import ModStorage
from src.models.mod import Mod
from src.utils import fs


@pytest.mark.parametrize("nested_name", ["skin", "skin/skin", "SKIN"])
def test_materialize_preserves_content_with_repeated_wrapper_name(tmp_path, nested_name):
    """同名目录展开后仍保留内部层级，不将外层目录误报为外部占用。"""
    container = tmp_path / "source"
    container.mkdir()
    body = container / "skin.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr(f"skin/{nested_name}/mod.ini", "[Constants]\nx=1\n")
        archive.writestr("skin/readme.txt", "package notes")
        archive.writestr("skin/.unpack/keep.txt", "nested payload")
    identity = "abcdefabcdef4def8defabcdefabcdef"
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=identity, imported_at="2026-10-07T00:00:00"
    )
    original = body.read_bytes()
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")

    deployed = storage.materialize_deployment(body, identity, "Skin")

    assert fs.read_text(deployed / nested_name / "mod.ini") == "[Constants]\nx=1\n"
    assert fs.read_text(deployed / "readme.txt") == "package notes"
    assert fs.read_text(deployed / ".unpack/keep.txt") == "nested payload"
    assert body.read_bytes() == original
    assert fs.list_children(deployed.parent) == [deployed]


def test_materialize_preserves_root_file_named_after_archive(tmp_path):
    container = tmp_path / "source"
    container.mkdir()
    body = container / "skin.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("skin", "root payload")
        archive.writestr("mod.ini", "[Constants]\nx=1\n")
    identity = "abcdefabcdef4def8defabcdefabcdef"
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=identity, imported_at="2026-10-07T00:00:00"
    )
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")

    deployed = storage.materialize_deployment(body, identity, "Skin")

    assert fs.read_text(deployed / "skin") == "root payload"
    assert fs.read_text(deployed / "mod.ini") == "[Constants]\nx=1\n"
    assert fs.list_children(deployed.parent) == [deployed]


@pytest.mark.parametrize("locked_phase", ["flatten", "publish"])
def test_materialize_retries_transient_windows_directory_lock(tmp_path, monkeypatch, locked_phase):
    container = tmp_path / "source"
    container.mkdir()
    body = container / "skin.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("wrapper/skin/mod.ini", "[Constants]\nx=1\n")
    identity = "abcdefabcdef4def8defabcdefabcdef"
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=identity, imported_at="2026-10-07T00:00:00"
    )
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    calls = []
    rename, replace = os.rename, os.replace

    def blocked(operation, source, target):
        phase = "publish" if Path(source).name.endswith(".partial") else "flatten"
        if phase == locked_phase and len(calls) < 2:
            calls.append(phase)
            error = PermissionError("directory temporarily in use")
            error.winerror = 5
            raise error
        return operation(source, target)

    monkeypatch.setattr(os, "rename", lambda s, t: blocked(rename, s, t))
    monkeypatch.setattr(os, "replace", lambda s, t: blocked(replace, s, t))
    deployed = storage.materialize_deployment(body, identity, "Skin")
    assert (deployed / "skin/mod.ini").read_text() == "[Constants]\nx=1\n"
    assert calls == [locked_phase, locked_phase]
    assert not any(p.name.endswith(".partial") for p in deployed.parent.iterdir())
