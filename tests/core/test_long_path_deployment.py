"""验证未启用系统长路径选项时的部署、INI 覆盖和清理。"""

import builtins
import io
import os
import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import read_deployment_identity
from src.core.mod_package import read_override, write_container_marker, write_override
from src.core.mod_storage import ModStorage
from src.models.mod import Mod
from src.utils import fs

_IDENTITY = "abcdefabcdef4def8defabcdefabcdef"
_INI = "[Constants]\nx=1\n"
_OVERRIDE = "[Constants]\nx=2\n"


@pytest.fixture
def legacy_windows_paths(monkeypatch):
    """保留真实文件操作，只模拟 Win32 未启用长路径时对普通路径的拒绝。"""
    if os.name != "nt":
        return

    def guard(operation, path_count):
        """检查操作的路径参数，扩展长度路径继续交给真实文件系统处理。"""

        def call(*args, **kwargs):
            """按 Windows UTF-16 长度限制拦截未加前缀的路径。"""
            for value in args[:path_count]:
                if not isinstance(value, (str, os.PathLike)):
                    continue
                raw = os.fspath(value)
                if not raw.startswith("\\\\?\\") and len(os.path.abspath(raw).encode("utf-16-le")) // 2 >= 260:
                    raise OSError(22, "Legacy Windows path limit", raw, 206)
            return operation(*args, **kwargs)

        return call

    for name in ("stat", "lstat", "scandir", "mkdir", "unlink", "rmdir", "open", "chmod"):
        monkeypatch.setattr(os, name, guard(getattr(os, name), 1))
    for name in ("rename", "replace", "link", "symlink"):
        monkeypatch.setattr(os, name, guard(getattr(os, name), 2))
    monkeypatch.setattr(builtins, "open", guard(builtins.open, 1))
    monkeypatch.setattr(io, "open", guard(io.open, 1))


def _deep(parent: Path) -> Path:
    """构造每段合法而总长度超过 MAX_PATH 的目录。"""
    for index in range(6):
        parent /= f"segment_{index}_" + "长" * 24
    assert len(str(parent)) > 260
    return parent


def _source(root: Path) -> Path:
    """创建含嵌套 INI 与元数据的真实 ZIP 容器。"""
    container = _deep(root / "source")
    fs.ensure_dir(container)
    source = container / "skin.zip"
    with zipfile.ZipFile(fs.native_path(source), "w") as archive:
        archive.writestr("wrapper/nested/mod.ini", _INI)
        archive.writestr("wrapper/.d3dxmeta/info.json", "{}")
    assert write_container_marker(
        container, source.name, Mod.compute_sha1(source), mod_id=_IDENTITY, imported_at="2026-10-07T00:00:00"
    )
    assert write_override(source, "nested/mod.ini", _OVERRIDE)
    return source


@pytest.mark.usefixtures("legacy_windows_paths")
@pytest.mark.parametrize("extended_deploy_path", [False, True])
def test_long_path_deployment_lifecycle_preserves_ini_edits(tmp_path, extended_deploy_path):
    source = _source(tmp_path)
    original = fs.read_bytes(source)
    storage = ModStorage()
    storage.set_migoto_mods_dir(_deep(tmp_path / "deploy"))

    deployed = storage.materialize_deployment(source, _IDENTITY, "Skin")

    assert fs.read_text(deployed / "nested/mod.ini") == _OVERRIDE
    assert not fs.exists(deployed / ".d3dxmeta")
    assert read_deployment_identity(deployed)["mod_id"] == _IDENTITY
    assert storage.scan_deployments(force=True)[_IDENTITY].path == deployed
    parked = storage.park_deployment(deployed, _IDENTITY)
    assert parked is not None and not fs.exists(deployed)
    restored = storage.restore_parked_deployment(parked, deployed.name)
    assert restored == deployed
    assert fs.read_text(restored / "nested/mod.ini") == _OVERRIDE

    # 模拟游戏在独立副本中留下较新的设置，禁用前须回收至覆盖目录。
    fs.unlink(restored / "nested/mod.ini")
    fs.write_bytes(restored / "nested/mod.ini", b"[Constants]\nx=3\n")
    target = fs.native_path(restored) if extended_deploy_path else restored
    assert storage.undeploy_mod(source, deploy_path=target, mod_id=_IDENTITY)
    assert not fs.exists(restored)
    assert read_override(source, "nested/mod.ini") == "[Constants]\nx=3\n"
    assert fs.read_bytes(source) == original


@pytest.mark.usefixtures("legacy_windows_paths")
def test_long_path_failed_extraction_removes_partial(tmp_path):
    source = _source(tmp_path)
    fs.write_bytes(source, b"broken archive")
    storage = ModStorage()
    storage.set_migoto_mods_dir(_deep(tmp_path / "deploy"))
    active, parked, partial = storage.deployment_paths(_IDENTITY, "Skin")

    with pytest.raises(OSError, match="解压失败"):
        storage.materialize_deployment(source, _IDENTITY, "Skin")

    assert not any(fs.exists(path) for path in (active, parked, partial))
    assert fs.read_bytes(source) == b"broken archive"


@pytest.mark.usefixtures("legacy_windows_paths")
@pytest.mark.parametrize("method", ["hardlink", "copy"])
def test_long_path_ini_link_fallback_preserves_content(tmp_path, monkeypatch, method):
    override = _deep(tmp_path / "source") / "mod.ini"
    deployed = _deep(tmp_path / "deploy") / "mod.ini"
    fs.write_text(override, _OVERRIDE)
    fs.write_text(deployed, _INI)

    def unavailable(*args, **kwargs):
        """模拟文件系统不支持当前链接方式，后续回退仍执行真实操作。"""
        raise OSError("Link unavailable")

    monkeypatch.setattr(os, "symlink", unavailable)
    if method == "copy":
        monkeypatch.setattr(os, "link", unavailable)

    assert ModStorage().link_ini(override, deployed) == method
    assert fs.read_text(deployed) == _OVERRIDE
    assert fs.read_text(override) == _OVERRIDE
