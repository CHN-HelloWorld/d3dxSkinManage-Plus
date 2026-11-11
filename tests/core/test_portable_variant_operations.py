"""验证搬迁应用后的事务恢复只访问当前副本，并保留内容与清单校验。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from src.core.mod_package import inspect_effective_inis, write_container_marker
from src.core.variant_operation import FileChange, IniInventoryChanged, IniInventoryGuard, VariantOperation
from src.utils import paths


@pytest.fixture
def portable_app(temp_data_dir, monkeypatch):
    """在隔离目录中建立应用副本，并让路径解析独立于启动工作目录。"""
    app = temp_data_dir / "original-app"
    app.mkdir()
    monkeypatch.setattr(paths, "get_app_dir", lambda: app)
    unrelated = temp_data_dir / "launch-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    return app


def _copy_app(app: Path, monkeypatch) -> Path:
    """保留旧副本，模拟跨设备复制后从新位置启动。"""
    copied = app.with_name("copied-app")
    shutil.copytree(app, copied)
    monkeypatch.setattr(paths, "get_app_dir", lambda: copied)
    return copied


def _source_guard(app: Path, *, archive: bool = False) -> IniInventoryGuard:
    """建立带覆盖层的真实源清单，归档副本具有不同的文件身份。"""
    container = app / "data/Game/mods/Skin"
    container.mkdir(parents=True)
    body = container / ("Body.zip" if archive else "Body")
    if archive:
        with zipfile.ZipFile(body, "w") as package:
            package.writestr("mod.ini", "[Constants]\n")
    else:
        body.mkdir()
        (body / "mod.ini").write_bytes(b"[Constants]\n")
    write_container_marker(container, body.name, "a" * 40, imported_at="2026-01-01T00:00:00")
    return IniInventoryGuard.source(inspect_effective_inis(body))


@pytest.mark.parametrize("archive", [False, True])
def test_copied_operation_recovers_new_copy_and_preserves_source_identity(portable_app, monkeypatch, archive):
    """目录清单可重新验证；归档身份变化拒绝继续提交，但不妨碍按字节回滚。"""
    app = portable_app
    guard = _source_guard(app, archive=archive)
    target = app / "data/Game/preferences.json"
    target.write_bytes(b"old")
    op = VariantOperation(
        app / "data/Game/variant_operations", "Game", ["mod"], [guard, FileChange(target, b"old", b"new")]
    )
    op.apply()
    stored = json.loads(op.journal_path.read_text(encoding="utf-8"))
    assert stored["changes"][0]["path"] == "data/Game/preferences.json"
    source_image = json.loads(stored["prerequisites"][0]["expected"])
    assert source_image["body"] == "data/Game/mods/Skin/" + ("Body.zip" if archive else "Body")
    assert source_image["override_root"] == "data/Game/mods/Skin/.d3dxmeta/ini_overrides"

    copied = _copy_app(app, monkeypatch)
    copied_journal = copied / "data/Game/variant_operations" / op.journal_path.name
    loaded = VariantOperation.load(copied_journal, allowed_roots=[copied / "data"])
    assert loaded.changes[0].path == copied / "data/Game/preferences.json"
    assert loaded.prerequisites[0].path == copied / "data/Game/mods/Skin" / guard.path.name
    if archive:
        with pytest.raises(IniInventoryChanged):
            loaded.verify_applied()
    else:
        loaded.verify_applied()
        (loaded.prerequisites[0].path / "external.ini").write_bytes(b"external")
        with pytest.raises(IniInventoryChanged):
            loaded.verify_applied()
    assert loaded.rollback() == ()
    assert (copied / "data/Game/preferences.json").read_bytes() == b"old"
    assert target.read_bytes() == b"new"
    assert not copied_journal.exists()
    assert op.journal_path.exists()


def test_copied_partially_applied_operation_preserves_read_only_changes(portable_app, monkeypatch):
    app = portable_app
    target = app / "state.ini"
    author = app / "author.ini"
    target.write_bytes(b"old")
    author.write_bytes(b"author")
    op = VariantOperation(
        app / "operations",
        "Game",
        ["mod"],
        [FileChange(target, b"old", b"new"), FileChange(author, b"author", b"author")],
    )
    op.prepare()
    target.write_bytes(b"new")
    copied = _copy_app(app, monkeypatch)
    (copied / "author.ini").write_bytes(b"external")
    loaded = VariantOperation.load(copied / "operations" / op.journal_path.name, allowed_roots=[copied])
    assert loaded.rollback() == ()
    assert (copied / "state.ini").read_bytes() == b"old"
    assert (copied / "author.ini").read_bytes() == b"external"
    assert target.read_bytes() == b"new"


def test_copied_operation_preserves_external_edits_across_recovery_retries(portable_app, monkeypatch):
    app = portable_app
    target = app / "state.ini"
    target.write_bytes(b"old")
    op = VariantOperation(app / "operations", "Game", ["mod"], [FileChange(target, b"old", b"new")])
    op.apply()
    copied = _copy_app(app, monkeypatch)
    (copied / "state.ini").write_bytes(b"external")
    journal = copied / "operations" / op.journal_path.name
    loaded = VariantOperation.load(journal, allowed_roots=[copied])
    assert loaded.rollback()
    assert json.loads(journal.read_text(encoding="utf-8"))["changes"][0]["path"] == "state.ini"
    retried = VariantOperation.load(journal, allowed_roots=[copied])
    assert retried.rollback()
    assert (copied / "state.ini").read_bytes() == b"external"
    assert target.read_bytes() == b"new"


def test_legacy_absolute_journal_and_external_targets_remain_recoverable(portable_app):
    app = portable_app
    guard = _source_guard(app)
    target = app.parent / "external.ini"
    target.write_bytes(b"old")
    op = VariantOperation(app / "operations", "Game", ["mod"], [guard, FileChange(target, b"old", b"new")])
    op.apply()
    stored = json.loads(op.journal_path.read_text(encoding="utf-8"))
    assert stored["changes"][0]["path"] == str(target)
    stored["prerequisites"][0] = {"path": str(guard.path), "scope": guard.scope, "expected": guard.expected}
    op.journal_path.write_text(json.dumps(stored), encoding="utf-8")
    loaded = VariantOperation.load(op.journal_path, allowed_roots=[app, target])
    loaded.verify_applied()
    assert loaded.rollback() == ()
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("section", ["changes", "prerequisites"])
def test_relative_recovery_paths_cannot_escape_allowed_roots(portable_app, section):
    app = portable_app
    source = app / "source"
    source.mkdir()
    op = VariantOperation(
        app / "operations",
        "Game",
        ["mod"],
        [IniInventoryGuard.deployed(source, []), FileChange(app / "state.ini", None, b"new")],
    )
    op.prepare()
    stored = json.loads(op.journal_path.read_text(encoding="utf-8"))
    stored[section][0]["path"] = "../outside.ini"
    op.journal_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="路径"):
        VariantOperation.load(op.journal_path, allowed_roots=[app])


def test_copied_journal_still_rejects_corrupt_content_hash(portable_app, monkeypatch):
    app = portable_app
    target = app / "state.ini"
    target.write_bytes(b"old")
    op = VariantOperation(app / "operations", "Game", ["mod"], [FileChange(target, b"old", b"new")])
    op.apply()
    copied = _copy_app(app, monkeypatch)
    journal = copied / "operations" / op.journal_path.name
    stored = json.loads(journal.read_text(encoding="utf-8"))
    stored["changes"][0]["after_hash"] = "corrupt"
    journal.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        VariantOperation.load(journal, allowed_roots=[copied])
    assert (copied / "state.ini").read_bytes() == b"new"
    assert target.read_bytes() == b"new"
