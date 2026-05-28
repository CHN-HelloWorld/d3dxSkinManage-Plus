"""验证链接部署原地保存时的通知、局部核对与监控生命周期。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from src.core.ini_sync import IniSyncManager
from src.core.mod_package import override_path_for
from src.core.mod_runtime_prefs import PREFS_LOCK
from tests.core.test_ini_sync import _archive_container, _deploy_dir

_INITIAL_INI = "[KeySwap]\nkey = ALT H\n"
_EDITED_INI = "[KeySwap]\nkey = ALT J\n"


def _linked_deployment(root: Path) -> tuple[Path, Path, Path]:
    """创建归档容器和指向覆盖文件的真实部署链接，不支持符号链接时跳过。"""
    body = _archive_container(root / "library", "Sample")
    override = override_path_for(body, "merged.ini")
    assert override is not None
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(_INITIAL_INI, encoding="utf-8")
    deploy = _deploy_dir(root / "runtime", "Sample", _INITIAL_INI)
    ini = deploy / "merged.ini"
    ini.unlink()
    try:
        ini.symlink_to(override)
    except OSError as error:
        pytest.skip(f"当前文件系统无法创建符号链接：{error}")
    return body, deploy, override


@pytest.mark.parametrize("reregister", [False, True])
def test_in_place_symlink_write_notifies_without_periodic_sweep(tmp_path, reregister):
    """以等长内容原地保存部署链接，源库提前重登记后仍由真实文件事件触发通知。"""
    body, deploy, override = _linked_deployment(tmp_path)
    ini = deploy / "merged.ini"
    manager = IniSyncManager(debounce_seconds=0.03, sweep_seconds=0)
    received = threading.Event()
    notifications: list[str] = []

    def on_synced(mod_id: str) -> None:
        """记录后台通知并唤醒断言线程，不主动触发同步。"""
        notifications.append(mod_id)
        received.set()

    manager.on_synced = on_synced
    try:
        assert manager.register_mod("sample", str(body), str(deploy), source_watch_root=tmp_path / "library") == 1
        identity = ini.stat().st_ino
        # 配置锁使后台核对等到重登记之后，稳定覆盖两个监控器之间的事件顺序。
        with PREFS_LOCK:
            ini.write_text(_EDITED_INI, encoding="utf-8")
            if reregister:
                assert (
                    manager.register_mod("sample", str(body), str(deploy), source_watch_root=tmp_path / "library") == 1
                )
        assert ini.is_symlink() and ini.stat().st_ino == identity
        assert override.read_text(encoding="utf-8") == _EDITED_INI
        assert received.wait(3), "原地保存部署链接后未收到按键同步通知"
        assert notifications == ["sample"]
    finally:
        manager.stop()


@pytest.mark.parametrize("changed", [_EDITED_INI, "[Constants]\nvalue = 2\n"])
def test_reregister_preserves_unnotified_link_content_change(tmp_path, changed):
    """源库先重新登记同一部署时，后续 INI 局部核对仍须识别按键修改或删除。"""
    body, deploy, override = _linked_deployment(tmp_path)
    ini = deploy / "merged.ini"
    manager = IniSyncManager(enable_watch=False)
    notifications: list[str] = []
    manager.on_synced = notifications.append
    try:
        assert manager.register_mod("sample", str(body), str(deploy)) == 1
        queued = [("sample", pair) for pair in manager._pairs["sample"]]
        ini.write_text(changed, encoding="utf-8")
        assert manager.register_mod("sample", str(body), str(deploy)) == 1

        assert manager._reconcile_refs(queued) == 0
        assert manager.reconcile_paths([override, ini]) == 1
        assert notifications == ["sample"]
        assert manager.reconcile_paths([override, ini]) == 0
        assert notifications == ["sample"]
        assert ini.is_symlink()
        assert override.read_text(encoding="utf-8") == changed
    finally:
        manager.stop()


def test_register_changed_deployment_path_starts_new_baseline(tmp_path):
    """同一 Mod 更换部署路径后不继承旧部署的待通知摘要，后续变化仍可正常核对。"""
    body, deploy, override = _linked_deployment(tmp_path)
    replacement = _deploy_dir(tmp_path / "other-runtime", "Sample", _EDITED_INI)
    replacement_ini = replacement / "merged.ini"
    manager = IniSyncManager(enable_watch=False)
    notifications: list[str] = []
    manager.on_synced = notifications.append
    try:
        assert manager.register_mod("sample", str(body), str(deploy)) == 1
        (deploy / "merged.ini").write_text(_EDITED_INI, encoding="utf-8")
        assert manager.register_mod("sample", str(body), str(replacement)) == 1
        assert manager.reconcile_paths([override, replacement_ini]) == 0
        assert notifications == []

        replacement_ini.write_text("[KeySwap]\nkey = ALT K\n", encoding="utf-8")
        assert manager.reconcile_paths([replacement_ini]) == 1
        assert override.read_text(encoding="utf-8") == "[KeySwap]\nkey = ALT K\n"
        assert notifications == ["sample"]
    finally:
        manager.stop()


def test_reregister_preserves_runtime_conflict_direction(tmp_path):
    """同一路径重登记仍按运行期共同基线处理冲突，不采用冷启动的修改时间规则。"""
    body = _archive_container(tmp_path / "library", "Sample")
    deploy = _deploy_dir(tmp_path / "runtime", "Sample", _INITIAL_INI)
    ini = deploy / "merged.ini"
    override = override_path_for(body, "merged.ini")
    assert override is not None
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(_INITIAL_INI, encoding="utf-8")
    manager = IniSyncManager(enable_watch=False)
    notifications: list[str] = []
    manager.on_synced = notifications.append
    try:
        assert manager.register_mod("sample", str(body), str(deploy)) == 1
        ini.write_text(_EDITED_INI, encoding="utf-8")
        override.write_text("[KeySwap]\nkey = ALT K\n", encoding="utf-8")
        os.utime(ini, ns=(1_000_000_000, 1_000_000_000))
        os.utime(override, ns=(2_000_000_000, 2_000_000_000))

        assert manager.register_mod("sample", str(body), str(deploy)) == 1
        manager.reconcile_paths([override, ini])

        assert ini.read_text(encoding="utf-8") == _EDITED_INI
        assert override.read_text(encoding="utf-8") == _EDITED_INI
        assert notifications == ["sample"]
    finally:
        manager.stop()
