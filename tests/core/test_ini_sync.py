"""验证 INI 双向同步：单侧变化向另一侧同步，冲突以部署为准，相同内容不重复写入。"""

from __future__ import annotations

import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

import src.core.ini_sync as ini_sync_module
from src.core.ini_sync import IniSyncManager
from src.core.mod_package import (
    override_path_for,
    write_container_marker,
)

_INI = "[KeySwap]\nkey = VK_A\nback = VK_B\n"
_NO_KEY_INI = "[Constants]\nglobal persist $swapvar = 0\n"


def _archive_container(root: Path, name: str) -> Path:
    """归档本体容器；返回本体路径。"""
    container = root / name
    container.mkdir(parents=True)
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr(f"{name}/merged.ini", _INI)
    write_container_marker(container, body.name, "0" * 40, imported_at="2026-10-05T10:00:00")
    return body


def _deploy_dir(root: Path, name: str, ini_text: str) -> Path:
    """模拟部署产物目录（解压+叠加 override 后的样子）。"""
    d = root / "Mods" / name
    d.mkdir(parents=True)
    (d / "merged.ini").write_text(ini_text, encoding="utf-8")
    return d


def _register(mgr: IniSyncManager, body: Path, deploy: Path) -> int:
    return mgr.register_mod("mid1", str(body), str(deploy))


def test_register_creates_pairs(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    n = _register(mgr, body, deploy)
    assert n == 1


def test_queued_pair_is_discarded_after_unregistration(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    try:
        _register(mgr, body, deploy)
        queued = [("mid1", pair) for pair in mgr._pairs["mid1"]]
        mgr.unregister_mod("mid1")
        (deploy / "merged.ini").write_text("[KeySwap]\nkey = F9\n", encoding="utf-8")
        assert mgr._reconcile_refs(queued) == 0
        assert not override_path_for(body, "merged.ini").exists()
        _register(mgr, body, deploy)
        (deploy / "merged.ini").write_text("[KeySwap]\nkey = F8\n", encoding="utf-8")
        assert mgr.reconcile_mod("mid1") == 1
        assert "F8" in override_path_for(body, "merged.ini").read_text(encoding="utf-8")
    finally:
        mgr.stop()


def test_register_repairs_deployment_symlink_after_source_directory_moves(tmp_path):
    body = _archive_container(tmp_path / "application", "M")
    override = override_path_for(body, "merged.ini")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(_INI, encoding="utf-8")
    deploy = _deploy_dir(tmp_path, "M", "placeholder")
    ini = deploy / "merged.ini"
    ini.unlink()
    try:
        ini.symlink_to(override)
    except OSError as error:
        pytest.skip(f"symbolic link creation is unavailable: {error}")
    original_app = tmp_path / "application"
    moved_app = tmp_path / "moved-application"
    original_app.rename(moved_app)
    assert ini.is_symlink() and not ini.exists()
    moved_body = moved_app / "M/M.zip"
    mgr = IniSyncManager(enable_watch=False)
    try:
        assert _register(mgr, moved_body, deploy) == 1
        assert ini.read_text(encoding="utf-8") == _INI
        ini.write_text("[Constants]\nvalue = 2\n", encoding="utf-8")
        mgr.reconcile_mod("mid1")
        assert override_path_for(moved_body, "merged.ini").read_text(encoding="utf-8") == "[Constants]\nvalue = 2\n"
        assert not original_app.exists()
    finally:
        mgr.stop()


def test_register_repairs_dangling_link_with_filesystem_type_simulated(tmp_path, monkeypatch):
    """验证无法创建原生符号链接时的真实修复流程。

    只替换悬空路径的两次类型查询；发现、源移动、修复、读取和后续同步均使用实际文件。
    """
    body = _archive_container(tmp_path / "application", "M")
    override = override_path_for(body, "merged.ini")
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(_INI, encoding="utf-8")
    deploy = _deploy_dir(tmp_path, "M", "dangling link entry")
    ini = deploy / "merged.ini"
    entry_inode = ini.stat().st_ino
    native_is_file, native_is_symlink = Path.is_file, Path.is_symlink

    def is_dangling_entry(path):
        try:
            return path == ini and path.stat().st_ino == entry_inode
        except OSError:
            return False

    monkeypatch.setattr(Path, "is_file", lambda path: False if is_dangling_entry(path) else native_is_file(path))
    monkeypatch.setattr(Path, "is_symlink", lambda path: True if is_dangling_entry(path) else native_is_symlink(path))
    (tmp_path / "application").rename(tmp_path / "moved-application")
    moved_body = tmp_path / "moved-application/M/M.zip"
    manager = IniSyncManager(enable_watch=False)
    try:
        assert _register(manager, moved_body, deploy) == 1
        assert ini.read_text(encoding="utf-8") == _INI
        ini.write_text("[Constants]\nvalue = 2\n", encoding="utf-8")
        manager.reconcile_mod("mid1")
        assert override_path_for(moved_body, "merged.ini").read_text(encoding="utf-8") == "[Constants]\nvalue = 2\n"
    finally:
        manager.stop()


def test_source_watch_uses_container_root_and_allows_container_move(tmp_path):
    body = _archive_container(tmp_path, "M")
    container = body.parent
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=True)
    try:
        assert _register(mgr, body, deploy) == 1
        assert ini_sync_module._path_key(container) in mgr._watched
        assert ini_sync_module._path_key(container / ".d3dxmeta" / "ini_overrides") not in mgr._watched

        moved = tmp_path / "M-moved"
        os.rename(container, moved)
        assert moved.is_dir()
    finally:
        mgr.stop()


def test_unregister_releases_all_directory_watches(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=True)
    try:
        assert _register(mgr, body, deploy) == 1
        assert mgr._watched
        assert mgr._mod_watch_keys["mid1"]

        mgr.unregister_mod("mid1")

        assert mgr._watched == set()
        assert mgr._watch_handles == {}
        assert mgr._watch_refcounts == {}
        assert mgr._mod_watch_keys == {}
        assert mgr._pairs == {}
    finally:
        mgr.stop()


def test_watch_changes_allow_concurrent_ini_event_delivery(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    manager = IniSyncManager(enable_watch=True, debounce_seconds=30, sweep_seconds=0)
    delivered = []
    threads = []

    class Observer:
        def interact(self, operation):
            done = threading.Event()

            def dispatch():
                manager.schedule_reconcile_paths([deploy / "merged.ini"])
                done.set()

            thread = threading.Thread(target=dispatch, daemon=True)
            threads.append(thread)
            thread.start()
            # 真实监控器在回调返回前保持分发锁。
            delivered.append((operation, done.wait(0.3)))

        def schedule(self, handler, path, recursive):
            self.interact("schedule")
            return object()

        def unschedule(self, watch):
            self.interact("unschedule")

        def stop(self):
            self.interact("stop")

        def join(self, timeout):
            pass

    manager._observer = Observer()
    try:
        assert _register(manager, body, deploy) == 1
        assert _register(manager, body, deploy) == 1
        manager.unregister_mod("mid1")
        manager.stop()
        for thread in threads:
            thread.join(1)
        assert {"schedule", "unschedule", "stop"} <= {name for name, _ in delivered}
        assert all(done for _, done in delivered), delivered
        assert not manager._watched and not manager._pairs
    finally:
        manager.stop()
        for thread in threads:
            thread.join(1)


def test_shared_library_root_allows_parent_category_move_and_refcounts(tmp_path):
    library = tmp_path / "library"
    category = library / "Category"
    body1 = _archive_container(category, "M1")
    body2 = _archive_container(category, "M2")
    deploy1 = _deploy_dir(tmp_path / "deploy1", "M1", _INI)
    deploy2 = _deploy_dir(tmp_path / "deploy2", "M2", _INI)
    mgr = IniSyncManager(enable_watch=True)
    root_key = ini_sync_module._path_key(library)
    try:
        assert mgr.register_mod("mid1", str(body1), str(deploy1), source_watch_root=library) == 1
        assert mgr.register_mod("mid2", str(body2), str(deploy2), source_watch_root=library) == 1
        assert root_key in mgr._watched
        assert mgr._watch_refcounts[root_key] == 2

        moved_category = library / "Category-moved"
        os.rename(category, moved_category)
        assert moved_category.is_dir()

        mgr.unregister_mod("mid1")
        assert mgr._watch_refcounts[root_key] == 1
        mgr.unregister_mod("mid2")
        assert mgr._watched == set()
    finally:
        mgr.stop()


def test_register_recovers_newer_deploy_edit_from_previous_process(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    op = override_path_for(str(body), "merged.ini")
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(_INI, encoding="utf-8")
    changed = "[KeySwap]\nkey = VK_Z\nback = VK_B\n"
    (deploy / "merged.ini").write_text(changed, encoding="utf-8")
    os.utime(op, ns=(1_000_000_000, 1_000_000_000))
    os.utime(deploy / "merged.ini", ns=(2_000_000_000, 2_000_000_000))

    mgr = IniSyncManager(enable_watch=False)
    assert _register(mgr, body, deploy) == 1
    assert op.read_text(encoding="utf-8") == changed
    assert mgr.reconcile_all() == 0


def test_register_preserves_newer_override_edit(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    op = override_path_for(str(body), "merged.ini")
    op.parent.mkdir(parents=True, exist_ok=True)
    changed = "[KeySwap]\nkey = VK_X\nback = VK_B\n"
    op.write_text(changed, encoding="utf-8")
    os.utime(deploy / "merged.ini", ns=(1_000_000_000, 1_000_000_000))
    os.utime(op, ns=(2_000_000_000, 2_000_000_000))

    mgr = IniSyncManager(enable_watch=False)
    assert _register(mgr, body, deploy) == 1
    assert (deploy / "merged.ini").read_text(encoding="utf-8") == changed
    assert mgr.reconcile_all() == 0


def test_deploy_writeback_to_override(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    # 起始无 override
    op = override_path_for(str(body), "merged.ini")
    assert not op.is_file()

    # 游戏写回部署副本
    (deploy / "merged.ini").write_text("[KeySwap]\nkey = VK_A\nback = VK_Z\n", encoding="utf-8")
    changed = mgr.reconcile_all()
    assert changed == 1
    # 运行期值持久化回源 override
    assert op.is_file()
    assert "VK_Z" in op.read_text(encoding="utf-8")
    # 再对账 no-op（回声消除）
    assert mgr.reconcile_all() == 0


def test_override_edit_to_deploy(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    # 先建一个与部署一致的 override（模拟已有 override 部署态）
    op = override_path_for(str(body), "merged.ini")
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(_INI, encoding="utf-8")
    mgr.reconcile_all()  # 让 last_hash 收敛到一致态

    # 外部改 override
    op.write_text("[KeySwap]\nkey = VK_X\nback = VK_B\n", encoding="utf-8")
    changed = mgr.reconcile_all()
    assert changed == 1
    # 部署副本被同步
    assert "VK_X" in (deploy / "merged.ini").read_text(encoding="utf-8")
    assert mgr.reconcile_all() == 0


def test_synced_is_noop(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    assert mgr.reconcile_all() == 0  # 两侧一致（部署时叠加 override，last=部署 hash）


def test_conflict_deploy_wins(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    op = override_path_for(str(body), "merged.ini")
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(_INI, encoding="utf-8")
    mgr.reconcile_all()  # 收敛一致

    # 两侧都改成不同内容
    op.write_text("[KeySwap]\nkey = VK_SRC\nback = VK_B\n", encoding="utf-8")
    (deploy / "merged.ini").write_text("[KeySwap]\nkey = VK_DEPLOY\nback = VK_B\n", encoding="utf-8")
    mgr.reconcile_all()
    # 部署优先：源被部署内容覆盖
    assert "VK_DEPLOY" in op.read_text(encoding="utf-8")
    assert "VK_DEPLOY" in (deploy / "merged.ini").read_text(encoding="utf-8")


def test_reconcile_mod_scoped(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    (deploy / "merged.ini").write_text("[KeySwap]\nkey = VK_A\nback = VK_Q\n", encoding="utf-8")
    assert mgr.reconcile_mod("mid1") == 1
    op = override_path_for(str(body), "merged.ini")
    assert "VK_Q" in op.read_text(encoding="utf-8")
    # 未知 mod → 0
    assert mgr.reconcile_mod("nope") == 0


def test_non_key_ini_change_does_not_notify_key_panel(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _NO_KEY_INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    notified: list[str] = []
    mgr.on_synced = notified.append

    changed = "[Constants]\nglobal persist $swapvar = 3\n"
    (deploy / "merged.ini").write_text(changed, encoding="utf-8")
    assert mgr.reconcile_all() == 1
    assert override_path_for(str(body), "merged.ini").read_text(encoding="utf-8") == changed
    assert notified == []


def test_key_ini_change_notifies_key_panel(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    notified: list[str] = []
    mgr.on_synced = notified.append

    (deploy / "merged.ini").write_text("[KeySwap]\nkey = VK_F6\nback = VK_B\n", encoding="utf-8")
    assert mgr.reconcile_all() == 1
    assert notified == ["mid1"]


def test_removing_last_key_still_notifies_key_panel(tmp_path):
    body = _archive_container(tmp_path, "M")
    deploy = _deploy_dir(tmp_path, "M", _INI)
    mgr = IniSyncManager(enable_watch=False)
    _register(mgr, body, deploy)
    notified: list[str] = []
    mgr.on_synced = notified.append

    (deploy / "merged.ini").write_text(_NO_KEY_INI, encoding="utf-8")
    assert mgr.reconcile_all() == 1
    assert notified == ["mid1"]


def test_reconcile_paths_hashes_only_matching_pair(tmp_path, monkeypatch):
    body1 = _archive_container(tmp_path, "M1")
    body2 = _archive_container(tmp_path, "M2")
    deploy1 = _deploy_dir(tmp_path, "M1", _INI)
    deploy2 = _deploy_dir(tmp_path, "M2", _INI)
    mgr = IniSyncManager(enable_watch=False)
    assert mgr.register_mod("mid1", str(body1), str(deploy1)) == 1
    assert mgr.register_mod("mid2", str(body2), str(deploy2)) == 1

    original_hash = ini_sync_module._hash_file
    hashed: list[Path] = []

    def _record_hash(path: Path):
        hashed.append(Path(path))
        return original_hash(path)

    monkeypatch.setattr(ini_sync_module, "_hash_file", _record_hash)
    changed_ini = deploy1 / "merged.ini"
    changed_ini.write_text("[KeySwap]\nkey = VK_A\nback = VK_LOCAL\n", encoding="utf-8")

    assert mgr.reconcile_paths([changed_ini]) == 1
    assert deploy1 / "merged.ini" in hashed
    assert deploy2 / "merged.ini" not in hashed
    assert override_path_for(str(body2), "merged.ini") not in hashed
    mgr.stop()


def test_schedule_reconcile_paths_debounces_event_burst(tmp_path, monkeypatch):
    mgr = IniSyncManager(enable_watch=False, debounce_seconds=0.05)
    calls: list[set[str]] = []
    done = threading.Event()

    def _record(paths):
        calls.append(set(paths))
        done.set()
        return 0

    monkeypatch.setattr(mgr, "reconcile_paths", _record)
    p1 = tmp_path / "one.ini"
    p2 = tmp_path / "two.ini"
    for _ in range(8):
        mgr.schedule_reconcile_paths([p1])
        mgr.schedule_reconcile_paths([p2])

    assert done.wait(1.0)
    time.sleep(0.12)  # 确认静默窗口续期只产生一次最终执行
    assert len(calls) == 1
    assert calls[0] == {
        ini_sync_module._path_key(p1),
        ini_sync_module._path_key(p2),
    }
    mgr.stop()
