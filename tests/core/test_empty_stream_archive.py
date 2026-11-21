"""验证 7z 空流成员不会阻断内容识别、导入及其他 Mod 的启用。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.mod_intake import ModIntakeService
from src.core.mod_package import _read_complete_archive_inis, override_path_for, read_override
from src.models.mod import Mod
from src.services.archive import ArchiveService
from tests.core.test_intake_source_name import library as library
from tests.core.test_variant_preset_switch import env as env

_INI = "namespace = sample\n[Constants]\nglobal persist $variant = 0\n"


@pytest.fixture
def empty_stream_archive(tmp_path, monkeypatch):
    """写出真实空流成员，所有归档读写均在隔离目录中执行。"""
    py7zr = pytest.importorskip("py7zr")

    class EmptyStreamWriter(py7zr.SevenZipFile):
        """使用 7z 空流表示零字节文件，不为这些成员创建数据流或 CRC。"""

        @staticmethod
        def _make_file_info(target, arcname=None, dereference=False):
            """仅改变写包时的空文件表示，读包仍使用原始后端。"""
            info = py7zr.SevenZipFile._make_file_info(target, arcname, dereference)
            if target.is_file() and target.stat().st_size == 0:
                info["emptystream"] = True
            return info

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    folder = tmp_path / "Sample"
    folder.mkdir()
    (folder / "empty.ib").write_bytes(b"")
    (folder / "empty.ini").write_bytes(b"")
    (folder / "mod.ini").write_bytes(_INI.encode("utf-8"))
    archive = tmp_path / "Sample.7z"
    with EmptyStreamWriter(archive, "w") as writer:
        for name in ("empty.ib", "empty.ini", "mod.ini"):
            writer.write(folder / name, "Sample/" + name)
    return folder, archive


def test_empty_stream_members_keep_complete_inventory_and_content_identity(empty_stream_archive):
    folder, archive = empty_stream_archive

    entries = ArchiveService().list_entries(archive)

    assert entries is not None
    assert ("Sample/empty.ib", 0, 0) in entries
    assert ("Sample/empty.ini", 0, 0) in entries
    assert Mod.compute_sha1(archive) == Mod.compute_sha1(folder)
    assert dict(_read_complete_archive_inis(archive)) == {"empty.ini": "", "mod.ini": _INI}


def test_import_initializes_all_inis_from_empty_stream_archive(library, empty_stream_archive):
    manager, node = library
    _, archive = empty_stream_archive
    original = archive.read_bytes()
    service = ModIntakeService(manager)
    request = service.plan_request(source_path=archive, game_id="game-a", node_id=node.id)

    mod = service.commit(service.prepare(request))

    assert read_override(mod.path, "empty.ini") == ""
    assert read_override(mod.path, "mod.ini") == _INI
    assert Path(mod.path).read_bytes() == original
    assert archive.read_bytes() == original


@pytest.mark.parametrize("failure", ["missing", "nonempty"])
def test_empty_ini_still_requires_complete_content(empty_stream_archive, monkeypatch, failure):
    _, archive = empty_stream_archive
    read_all = ArchiveService.read_all_inis

    def incomplete(service, path):
        """在真实提取后模拟一个空 INI 丢失或被意外写入内容。"""
        result = read_all(service, path)
        if failure == "missing":
            result.pop("Sample/empty.ini")
        else:
            result["Sample/empty.ini"] = b"unexpected"
        return result

    monkeypatch.setattr(ArchiveService, "read_all_inis", incomplete)
    with pytest.raises(OSError):
        _read_complete_archive_inis(archive)


def test_legacy_seeded_empty_stream_mod_does_not_block_another_enable(env, empty_stream_archive):
    """旧初始化标记需重新读取成员表，仍应允许保留原 Mod 并启用其他 Mod。"""
    manager = env["mm"]
    _, archive = empty_stream_archive
    node = manager.add_character(name="示例角色乙", node_type="character")
    service = ModIntakeService(manager)
    request = service.plan_request(source_path=archive, game_id="g1", node_id=node.id)
    existing = service.commit(service.prepare(request))
    enabled_existing = manager.toggle_mod(existing.mod_id)
    assert enabled_existing is not None and enabled_existing.is_enabled
    manager.stop_ini_sync()
    manager.variants.stop_watch()
    override = override_path_for(existing.path, "mod.ini")
    (override.parent / ".seeded").write_text("1", encoding="utf-8")
    before = override.read_bytes()

    enabled = manager.toggle_mod(env["mod"].mod_id)

    assert enabled is not None and enabled.is_enabled
    assert manager.get_mod(existing.mod_id).is_enabled
    assert override.read_bytes() == before
    assert set(manager.variants.build_index().keys_of(existing.mod_id)) == {r"$\sample\variant"}
