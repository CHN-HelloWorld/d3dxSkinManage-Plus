from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_intake import DuplicateIntakeError, ModIntakeService
from src.core.mod_manager import ModManager
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _make_zip_mod(mm: ModManager, char_id: str, name: str) -> Path:
    """在角色库里造一个真实存在的 .zip Mod 文件，返回其路径。"""
    lib = mm._get_character_library(char_id)
    lib.mkdir(parents=True, exist_ok=True)
    zip_path = lib / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{name}/mod.ini", "[Constants]\n")
    return zip_path


def test_commit_rejects_second_record_for_same_path(mm, tmp_path):
    from src.core.config import Config

    char = mm.add_character(name="示例角色戊", node_type="character")
    source = tmp_path / "Kappa"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")

    Config().set("import.format", "zip")  # zip 本体便于往包内追加成员改内容哈希
    mod = mm.import_mod(source_path=str(source), node_id=char.id, name="Kappa")
    assert mod is not None

    # 伪造抢跑：同一容器内本体再走一次纳管（内容哈希已改，避开哈希判重这一道）。
    body = Path(mod.path)
    with zipfile.ZipFile(body, "a") as zf:
        zf.writestr("extra.ini", "[Constants]\ny=2\n")
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=body,
        game_id="game_a",
        node_id=char.id,
        display_name=body.name,
        context="rediscovery",
    )
    from src.core.mod_identity import IdentityConflict

    with pytest.raises((DuplicateIntakeError, IdentityConflict)):
        service.commit(service.prepare(request))

    assert len(mm.get_mods(node_id=char.id)) == 1


def test_index_rejects_second_record_of_same_container(mm):
    char = mm.add_character(name="Target", node_type="character")
    original = make_mod(mm._index, "kappa", node_id=char.id)
    assert mm._index.add_mod(original)
    duplicate = make_mod(mm._index, "kappa", node_id=char.id, content_hash="b" * 40)
    assert not mm._index.add_mod(duplicate)
    assert len(mm.get_mods(node_id=char.id)) == 1


def test_reconcile_removes_orphan_instead_of_relocating(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    zip_path = _make_zip_mod(mm, char.id, "kappa")
    mm._get_character_library(char.id)

    mm._index.game_id = "game_a"
    # 唯一记录，path 指向已不存在的旧文件夹，但同名 zip 在磁盘上
    missing = make_mod(mm._index, "kappa", node_id=char.id)
    assert mm._index.add_mod(missing)

    report = mm.reconcile_library("game_a", trigger="manual")

    assert report.removed_sources == 1
    assert mm.get_mods(node_id=char.id) == []
    # 同名 zip 是新输入，交给纳管事务，而不是被旧记录认领
    assert [Path(r.source_path) for r in report.intake_requests] == [zip_path]
