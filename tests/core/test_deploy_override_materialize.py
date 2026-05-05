"""验证部署时叠加 INI 覆盖文件、保持本体不变，并仅携带部署身份元数据。"""

from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import read_source_mod_id
from src.core.mod_package import (
    META_DIR,
    build_local_manifest,
    override_path_for,
    write_container_marker,
    write_override,
    write_sidecar,
)
from src.core.mod_storage import ModStorage

_INI = "[KeySwap]\nkey = VK_A\nback = VK_B\n"
_INI_EDITED = "[KeySwap]\nkey = VK_F5\nback = VK_B\n"
_INI_OFFLINE = "[KeySwap]\nkey = VK_F5\nback = VK_OFFLINE\n"


def _archive_container(root: Path, name: str) -> Path:
    container = root / name
    container.mkdir(parents=True)
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr(f"{name}/merged.ini", _INI)
    write_container_marker(container, body.name, "0" * 40, imported_at="2026-01-01T00:00:00")
    assert write_sidecar(body, build_local_manifest({"name": name, "info": {}}, {"mod_id": uuid.uuid4().hex}))
    return body


def _folder_container(root: Path, name: str) -> Path:
    container = root / name
    body = container / name
    body.mkdir(parents=True)
    (body / "merged.ini").write_bytes(_INI.encode("utf-8"))
    write_container_marker(container, name, "0" * 40, imported_at="2026-01-01T00:00:00")
    assert write_sidecar(body, build_local_manifest({"name": name, "info": {}}, {"mod_id": uuid.uuid4().hex}))
    return body


def _force_ini_copy_fallback(monkeypatch, storage: ModStorage) -> None:
    """稳定模拟 symlink/hardlink 都不可用时的独立 copy 回退处理。"""

    def _copy_only(override_path: Path, deploy_path: Path) -> str:
        deploy_path.parent.mkdir(parents=True, exist_ok=True)
        deploy_path.unlink(missing_ok=True)
        deploy_path.write_bytes(Path(override_path).read_bytes())
        return "copy"

    monkeypatch.setattr(storage, "link_ini", _copy_only)


def test_archive_deploy_applies_override(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "KappaSkin")
    # 编辑落 override（本体不动）
    assert write_override(str(body), "merged.ini", _INI_EDITED)

    deploy = tmp_path / "Mods"
    st = ModStorage()
    st.set_migoto_mods_dir(deploy)
    result = st.deploy_mod(str(body))
    assert result is not None
    deployed_ini = Path(result) / "merged.ini"
    assert deployed_ini.is_file()
    # 部署产物 ini = override 内容（游戏读到编辑后的键位）
    assert "VK_F5" in deployed_ini.read_text(encoding="utf-8")
    # 归档本体一个字节不动
    with zipfile.ZipFile(body) as zf:
        assert zf.read("KappaSkin/merged.ini").decode("utf-8") == _INI
    # 部署产物不含 .d3dxmeta
    assert not (Path(result) / META_DIR).exists()
    assert (Path(result) / ".d3dxmod").is_file()


def test_folder_body_deploy_rejected(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _folder_container(lib, "EpsilonSkin")
    assert write_override(str(body), "merged.ini", _INI_EDITED)

    deploy = tmp_path / "Mods"
    st = ModStorage()
    st.set_migoto_mods_dir(deploy)
    assert st.deploy_mod(str(body)) is None
    # 拒绝即止：部署目录里不留任何产物
    assert not (deploy / "EpsilonSkin").exists()
    # 源本体一个字节不动
    assert (body / "merged.ini").read_bytes() == _INI.encode("utf-8")


def test_folder_body_materialize_rejected(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _folder_container(lib, "ZetaSkin")

    st = ModStorage()
    st.set_migoto_mods_dir(tmp_path / "Mods")
    identity = read_source_mod_id(body)
    active, disabled, partial = st.deployment_paths(identity, "ZetaSkin")
    with pytest.raises(ValueError):
        st.materialize_deployment(str(body), identity, "ZetaSkin")
    for p in (active, disabled, partial):
        assert not p.exists()


def test_deploy_is_always_independent_copy(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "IotaSkin")
    assert write_override(str(body), "merged.ini", _INI_EDITED)

    deploy = tmp_path / "Mods"
    st = ModStorage()
    st.set_migoto_mods_dir(deploy)
    result = st.deploy_mod(str(body))
    assert result is not None
    rp = Path(result)
    # 真实副本（非链接）：是真目录、且改的 ini 已叠加
    assert rp.is_dir() and not rp.is_symlink()
    assert "VK_F5" in (rp / "merged.ini").read_text(encoding="utf-8")
    # 源本体未被污染
    with zipfile.ZipFile(body) as zf:
        assert zf.read("IotaSkin/merged.ini").decode("utf-8") == _INI


def test_no_override_deploy_unchanged(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "PlainSkin")

    deploy = tmp_path / "Mods"
    st = ModStorage()
    st.set_migoto_mods_dir(deploy)
    result = st.deploy_mod(str(body))
    assert result is not None
    assert (Path(result) / "merged.ini").read_text(encoding="utf-8") == _INI


def test_redeploy_preserves_newer_offline_copy_edit(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "OfflineSkin")
    assert write_override(str(body), "merged.ini", _INI_EDITED)

    st = ModStorage()
    st.set_migoto_mods_dir(tmp_path / "Mods")
    _force_ini_copy_fallback(monkeypatch, st)
    result = st.deploy_mod(str(body))
    assert result is not None

    deployed_ini = Path(result) / "merged.ini"
    deployed_ini.write_text(_INI_OFFLINE, encoding="utf-8")
    op = override_path_for(str(body), "merged.ini")
    newer = op.stat().st_mtime_ns + 2_000_000_000
    os.utime(deployed_ini, ns=(newer, newer))

    redeployed = st.deploy_mod(str(body))
    assert redeployed is not None
    assert op.read_text(encoding="utf-8") == _INI_OFFLINE
    assert (Path(redeployed) / "merged.ini").read_text(encoding="utf-8") == _INI_OFFLINE


def test_redeploy_keeps_newer_override_over_stale_copy(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "SourceNewerSkin")
    assert write_override(str(body), "merged.ini", _INI_EDITED)

    st = ModStorage()
    st.set_migoto_mods_dir(tmp_path / "Mods")
    _force_ini_copy_fallback(monkeypatch, st)
    result = st.deploy_mod(str(body))
    assert result is not None
    deployed_ini = Path(result) / "merged.ini"
    deployed_ini.write_text(_INI_OFFLINE, encoding="utf-8")

    source_newer = "[KeySwap]\nkey = VK_SOURCE_NEWER\nback = VK_B\n"
    assert write_override(str(body), "merged.ini", source_newer)
    op = override_path_for(str(body), "merged.ini")
    newer = deployed_ini.stat().st_mtime_ns + 2_000_000_000
    os.utime(op, ns=(newer, newer))

    redeployed = st.deploy_mod(str(body))
    assert redeployed is not None
    assert op.read_text(encoding="utf-8") == source_newer
    assert (Path(redeployed) / "merged.ini").read_text(encoding="utf-8") == source_newer


def test_undeploy_recovers_offline_copy_without_existing_override(tmp_path):
    """未设置 INI 覆盖时，禁用前仍应采集部署副本中的离线修改。

    归档成员没有可直接比较的文件修改时间，因此使用归档本身的修改时间作为版本基线。
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    body = _archive_container(lib, "ArchiveOfflineSkin")

    st = ModStorage()
    st.set_migoto_mods_dir(tmp_path / "Mods")
    result = st.deploy_mod(str(body))
    assert result is not None
    deployed_ini = Path(result) / "merged.ini"
    deployed_ini.write_text(_INI_OFFLINE, encoding="utf-8")
    newer = body.stat().st_mtime_ns + 2_000_000_000
    os.utime(deployed_ini, ns=(newer, newer))

    assert st.undeploy_mod(str(body)) is True
    assert not Path(result).exists()
    op = override_path_for(str(body), "merged.ini")
    assert op.read_text(encoding="utf-8") == _INI_OFFLINE
