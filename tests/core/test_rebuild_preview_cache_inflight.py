from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from tests.helpers import registered_games as registered_games
from tests.helpers import scan_and_sync


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


# 1×1 像素 PNG：让 _find_previews 认出包内有预览图，从而提取到 preview_cache
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _make_zip_mod(parent: Path, name: str) -> Path:
    """造一个含 mod.ini + preview.png 的归档 Mod，放进角色目录等 scan_and_sync 纳管。"""
    parent.mkdir(parents=True, exist_ok=True)
    archive = parent / f"{name}.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(f"{name}/mod.ini", "[Constants]\n")
        z.writestr(f"{name}/preview.png", _PNG)
    return archive


def _register_one_archive_mod(mm: ModManager) -> str:
    # 直投纳管按「导入格式」物化本体；必须显式要 zip，否则默认 folder 会把包解开，
    # 预览图落进本体子树而不是 preview_cache，这条竞态就测不到了。
    mm._config.set("import.format", "zip")
    char = mm.add_character(name="示例角色丁", node_type="character")
    _make_zip_mod(mm._get_character_library(char.id), "Delta_A")
    scan_and_sync(mm)
    mods = mm.get_mods()
    assert len(mods) == 1, "前置条件：应纳管出一个 Mod"
    assert Path(mods[0].path).suffix.lower() == ".zip", "前置条件：本体应保持归档"
    assert mods[0].preview_images, "前置条件：导入后应有预览图"
    assert "preview_cache" in mods[0].preview_images[0], "前置条件：归档预览应落中心缓存"
    return "game_a"


def _stage(mm, gid):
    from src.core.rebuild_session import RebuildSession

    session = RebuildSession(mm, gid)
    session.prepare()
    while not session.stage_batch():
        pass
    return session


def test_rebuild_preserves_identity_and_cache_ownership(mm):
    gid = _register_one_archive_mod(mm)
    original = mm.get_mods()[0]
    session = _stage(mm, gid)
    try:
        assert original.mod_id in mm.inflight_mod_ids()
        session.validate()
        session.commit()
    finally:
        session.close()
    assert not mm.inflight_mod_ids()
    assert mm.get_mods()[0].mod_id == original.mod_id


def test_cleanup_during_rebuild_keeps_freshly_extracted_previews(mm):
    gid = _register_one_archive_mod(mm)
    session = _stage(mm, gid)
    try:
        extracted = [Path(p) for mod in mm.get_mods() for p in mod.preview_images]
        assert extracted
        mm._cleanup_orphaned_managed_artifacts(gid)
        assert all(p.exists() for p in extracted)
        session.validate()
        session.commit()
    finally:
        session.close()
    assert all(Path(p).exists() for mod in mm.get_mods() for p in mod.preview_images)


def test_cleanup_after_rebuild_drops_only_orphan_caches(mm):
    gid = _register_one_archive_mod(mm)
    original = mm.get_mods()[0]
    from src.utils.paths import get_data_dir

    stale = get_data_dir() / mm._res_dir(gid) / "preview_cache" / "orphan"
    stale.mkdir()
    (stale / "preview.png").write_bytes(b"unused")
    mm.rebuild_index_from_filesystem(gid)
    mm._cleanup_orphaned_managed_artifacts(gid)
    assert not stale.exists()
    assert mm.get_mods()[0].mod_id == original.mod_id
    assert all(Path(p).exists() for p in original.preview_images)


def test_intake_cache_is_trusted_by_next_rebuild(mm, monkeypatch):
    gid = _register_one_archive_mod(mm)
    monkeypatch.setattr(mm._archive, "extract_files", lambda *a, **k: pytest.fail("intake failed to publish its cache"))
    mm.rebuild_index_from_filesystem(gid)
    assert all(Path(path).exists() for mod in mm.get_mods() for path in mod.preview_images)
