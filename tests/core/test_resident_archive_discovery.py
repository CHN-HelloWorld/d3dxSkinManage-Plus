from __future__ import annotations

from pathlib import Path

import py7zr
import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import read_sidecar, write_sidecar
from tests.helpers import registered_games as registered_games
from tests.helpers import scan_and_sync


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _make_7z(path: Path, top: str) -> None:
    """造一个自带单顶层目录 ``top``、含 mod.ini 的 .7z（真实 mod 压缩包同此结构）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(str(path), "w") as z:
        z.writestr(b"[Constants]\n", f"{top}/mod.ini")


def test_scan_and_sync_registers_7z_as_resident(mm):
    """外部 7z 归档纳管后保留格式及内容，仅将原文件移动到新容器。

    容器同时保存标记和分享元数据；修改元数据不应重写归档本体。
    """
    from src.core.mod_package import (
        MOD_CONTAINER_MARKER,
        build_local_manifest,
        is_mod_container,
    )
    from src.models.mod import Mod

    mm._config.set("import.format", "7z")
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _make_7z(lib / "KappaSkin.7z", top="KappaSkin")

    count = scan_and_sync(mm)

    assert count == 1
    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    assert mods[0].folder_name == "KappaSkin"
    body = Path(mods[0].path)
    assert body.suffix.lower() == ".7z"  # 保持为 7z，未被规范化转 zip

    # 外部拖入的 .7z 被裹进容器（含正向 marker .d3dxmod），本体在容器内
    container = body.parent
    assert is_mod_container(container)
    assert (container / MOD_CONTAINER_MARKER).is_file()
    assert body == lib / "KappaSkin" / "KappaSkin.7z"

    # 编辑元数据：写容器级 .d3dxmeta、归档一个字节不动（0ms、不重压）
    hash_before = Mod.compute_sha1(str(body))
    assert write_sidecar(
        str(body),
        build_local_manifest({"name": "Kappa", "info": {}}),
    )
    assert read_sidecar(str(body)) is not None
    assert Mod.compute_sha1(str(body)) == hash_before  # 归档未被改动
    assert (container / ".d3dxmeta" / "info.json").is_file()  # sidecar 在容器级
    assert not Path(str(body) + ".d3dxmeta.json").exists()  # 绝不外置污染目录
    # 分享元数据保存在容器中，不写入归档内部。
    from src.services.archive import ArchiveService

    names = ArchiveService().list_contents(str(body))
    assert not any(".d3dxmeta" in n.replace("\\", "/").split("/") for n in names)


def test_scan_and_sync_seeds_archive_ini_overrides(mm):
    """需求：外部拖入的归档在**发现时**就把包内 ini 播种到 override 层（与导入路径对齐），
    使日后点卡片直接读 override、永不开包（无「首次点击惰性补种」的可感知卡顿）。"""
    from src.core.mod_package import list_overrides, overrides_seeded

    mm._config.set("import.format", "7z")
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _make_7z(lib / "KeyMod.7z", top="KeyMod")

    assert scan_and_sync(mm) == 1
    body = Path(mm.get_mods(node_id=char.id)[0].path)

    assert overrides_seeded(str(body))  # 已落 .seeded 标记
    assert "mod.ini" in list_overrides(str(body))  # 包内 ini 已镜像进 override 层
