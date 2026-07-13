from __future__ import annotations

import zipfile

import pytest

from src.services.gamebanana.models import GbInstallMetadata
from tests.helpers import registered_games as registered_games


@pytest.fixture
def sample_archive(tmp_path):
    """构造最小有效 ZIP：含一个 .ini 和一个伪 PNG"""
    archive_path = tmp_path / "test_mod.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("ModFolder/dummy.ini", "[Section]\nkey=value\n")
        zf.writestr("ModFolder/preview.png", b"\x89PNG\r\n\x1a\n")
    return archive_path


def test_import_writes_gb_meta(temp_data_dir, sample_archive) -> None:
    from src.core.mod_manager import ModManager

    mm = ModManager()
    # 设置当前游戏（ModIndex.game_id 需要非空才能正确隔离）
    mm.set_game("game_a")

    # 通过公共 API 创建一个目标 character
    char = mm.add_character(name="测试角色", node_type="character")
    assert char is not None
    char_id = char.id

    meta = GbInstallMetadata(
        mod_id=123,
        file_id=456,
        file_md5="abcdef",
        file_ts=1700000000,
        page_url="https://gamebanana.com/mods/123",
        version=None,
        submitter_id=1,
        submitter_name="A",
        last_checked_iso="2026-05-17T00:00:00",
    )

    mod = mm.import_mod(
        source_path=str(sample_archive),
        node_id=char_id,
        gb_meta=meta,
    )
    assert mod is not None, "import_mod 返回 None — 导入失败"

    # 内存中的 mod 对象应该已包含 gb_meta
    gb = mod.info.custom_data.get("gamebanana")
    assert gb is not None
    assert gb["mod_id"] == 123
    assert gb["file_id"] == 456
    assert gb["file_md5"] == "abcdef"
    assert gb["file_ts"] == 1700000000
    assert gb["page_url"] == "https://gamebanana.com/mods/123"
    assert gb["version"] is None
    assert gb["submitter_id"] == 1
    assert gb["submitter_name"] == "A"
    assert gb["last_checked_iso"] == "2026-05-17T00:00:00"

    # 从 SQLite 持久化层读回，验证写入磁盘正确
    persisted = mm.get_mod(mod.mod_id)
    assert persisted is not None
    pgb = persisted.info.custom_data.get("gamebanana")
    assert pgb is not None, "gb_meta 未持久化到 SQLite"
    assert pgb["mod_id"] == 123
    assert pgb["file_md5"] == "abcdef"
    assert pgb["submitter_name"] == "A"


def test_import_without_gb_meta_leaves_custom_data_clean(
    temp_data_dir,
    sample_archive,
) -> None:
    from src.core.mod_manager import ModManager

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="测试角色2", node_type="character")
    assert char is not None

    mod = mm.import_mod(
        source_path=str(sample_archive),
        node_id=char.id,
    )
    assert mod is not None
    assert "gamebanana" not in mod.info.custom_data
