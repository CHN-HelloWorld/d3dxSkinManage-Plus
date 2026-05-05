from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from src.core.config import Config
from src.core.ini_sync import IniSyncManager
from src.core.mod_identity import read_deployment_identity, write_deployment_identity
from src.core.mod_manager import ModManager
from src.core.mod_package import iter_effective_inis
from src.core.mod_storage import ModStorage
from tests.helpers import registered_games as registered_games

SOURCE_INI = "[Constants]\nx=1\n"
OTHER_INI = "[Constants]\nx=999\n"


@pytest.fixture
def library(temp_data_dir, tmp_path):
    Config().set("import.format", "zip")
    manager = ModManager()
    manager.set_game("game_a")
    manager.set_deploy_dir(str(tmp_path / "deploy"))
    # 执行真实同步和捕获操作，避免引入后台监控调度竞争。
    manager._ini_sync = IniSyncManager(enable_watch=False)
    yield manager
    manager.stop_ini_sync()


def _import_mod(library, tmp_path, *, name="Skin"):
    character = library.add_character(name=f"Character-{name}", node_type="character")
    source = tmp_path / "input" / name
    source.mkdir(parents=True)
    (source / "mod.ini").write_bytes(SOURCE_INI.encode("utf-8"))
    (source / "mesh.buf").write_bytes(name.encode("utf-8"))
    mod = library.import_mod(source_path=str(source), node_id=character.id)
    assert mod is not None
    return mod


def _enabled_mod(library, tmp_path):
    mod = _import_mod(library, tmp_path)
    enabled = library.toggle_mod(mod.mod_id)
    assert enabled is not None and enabled.is_enabled
    assert dict(iter_effective_inis(enabled.path)) == {"mod.ini": SOURCE_INI}
    return enabled, Path(enabled.deployed_path)


def _replace_deployed_ini(target, source_body):
    deployed_ini = target / "mod.ini"
    # 替换内容时不能沿部署中的硬链接或符号链接修改源文件。
    deployed_ini.unlink()
    deployed_ini.write_bytes(OTHER_INI.encode("utf-8"))
    newer = Path(source_body).stat().st_mtime_ns + 2_000_000_000
    os.utime(deployed_ini, ns=(newer, newer))


def test_delete_mod_rescans_identity_after_cache_was_filled(library, tmp_path):
    mod, target = _enabled_mod(library, tmp_path)
    inventory = library._storage.scan_deployments((target,), force=True)
    assert inventory[mod.mod_id].path == target
    root_timestamp = target.parent.stat().st_mtime_ns
    other_identity = uuid4().hex

    write_deployment_identity(target, other_identity, "other-content")
    assert target.parent.stat().st_mtime_ns == root_timestamp
    assert library._storage.scan_deployments()[mod.mod_id].path == target

    deleted = library.delete_mod(mod.mod_id)

    assert target.is_dir(), "a cached path must not authorize deletion of its new owner"
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert deleted is True
    assert library.get_mod(mod.mod_id) is None


@pytest.mark.parametrize("problem", ["invalid", "duplicate"])
def test_delete_mod_preserves_evidence_when_identity_scan_fails(library, tmp_path, problem):
    mod, target = _enabled_mod(library, tmp_path)
    library._storage.scan_deployments((target,), force=True)
    if problem == "invalid":
        (target / ".d3dxmod").write_text("{invalid json", encoding="utf-8")
    else:
        shutil.copytree(target, target.with_name("DuplicateIdentity"))

    deleted = library.delete_mod(mod.mod_id)

    assert deleted is False
    assert target.is_dir()
    assert Path(mod.path).is_file()
    assert library.get_mod(mod.mod_id) is not None


@pytest.mark.parametrize("park", [False, True])
def test_disable_batch_keeps_reassigned_target_and_source_ini(library, tmp_path, park):
    mod, target = _enabled_mod(library, tmp_path)
    if park:
        library.preset_manager.create_preset("game_a", "Keep", snapshot={mod.node_id: [mod.mod_id]})
    (entry,) = library.prepare_disable_batch([mod.mod_id])
    assert entry.park is park
    assert entry.deploy_targets == (str(target),)
    other_identity = uuid4().hex
    write_deployment_identity(target, other_identity, "other-content")
    _replace_deployed_ini(target, mod.path)

    result = library.disable_batch_step(entry)

    assert target.is_dir(), "an expired disable plan must preserve the replacement directory"
    assert read_deployment_identity(target)["mod_id"] == other_identity
    library._ini_sync.reconcile_mod(mod.mod_id)
    assert dict(iter_effective_inis(mod.path)) == {"mod.ini": SOURCE_INI}
    assert result.ok is False
    assert result.error
    assert library.commit_disable_batch([result]) == []
    assert library.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("marker_state", ["mismatch", "missing", "invalid"])
def test_undeploy_rejects_unowned_target_before_capturing(library, tmp_path, marker_state):
    mod, target = _enabled_mod(library, tmp_path)
    marker = target / ".d3dxmod"
    if marker_state == "mismatch":
        write_deployment_identity(target, uuid4().hex, "other-content")
    elif marker_state == "missing":
        marker.unlink()
    else:
        marker.write_text("{invalid json", encoding="utf-8")
    _replace_deployed_ini(target, mod.path)

    removed = library._storage.undeploy_mod(mod.path, deploy_path=target)

    assert target.is_dir()
    assert dict(iter_effective_inis(mod.path)) == {"mod.ini": SOURCE_INI}
    assert removed is False


def test_storage_delete_rejects_changed_identity_and_keeps_source(library, tmp_path):
    mod, target = _enabled_mod(library, tmp_path)
    other_identity = uuid4().hex
    write_deployment_identity(target, other_identity, "other-content")

    deleted = library._storage.delete_mod(mod.path, deploy_name=mod.folder_name)

    assert target.is_dir()
    assert Path(mod.path).is_file()
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert deleted is False


def test_missing_source_cleanup_uses_saved_uuid_at_renamed_path(library, tmp_path):
    mod, old_target = _enabled_mod(library, tmp_path)
    own_target = old_target.with_name("UserRenamedDeployment")
    old_target.rename(own_target)
    old_target.mkdir()
    other_identity = uuid4().hex
    write_deployment_identity(old_target, other_identity, "other-content")
    shutil.rmtree(Path(mod.path).parent)
    library.set_deploy_dir("")

    assert library.reconcile_missing_sources("game_a") == 1

    assert not own_target.exists()
    assert old_target.is_dir()
    assert read_deployment_identity(old_target)["mod_id"] == other_identity
    assert library.get_mod(mod.mod_id) is None


@pytest.mark.parametrize("parked", [False, True])
def test_normal_delete_removes_only_its_own_deployment(library, tmp_path, parked):
    mod, target = _enabled_mod(library, tmp_path)
    if parked:
        target = library._storage.park_deployment(target, mod.mod_id)
        assert target is not None

    assert library.delete_mod(mod.mod_id) is True

    assert not target.exists()
    assert not Path(mod.path).exists()
    assert library.get_mod(mod.mod_id) is None


def test_undeploy_without_explicit_identity_uses_source_sidecar(library, tmp_path):
    mod, target = _enabled_mod(library, tmp_path)

    assert library._storage.undeploy_mod(mod.path) is True

    assert not target.exists()
    assert Path(mod.path).is_file()


def test_undeploy_without_source_or_saved_identity_keeps_same_name_target(library, tmp_path):
    mod, target = _enabled_mod(library, tmp_path)
    shutil.rmtree(Path(mod.path).parent)

    removed = library._storage.undeploy_mod(mod.path, capture_changes=False)

    assert target.is_dir()
    assert removed is False


def test_redeploy_rechecks_identity_after_finding_existing_copy(library, tmp_path, monkeypatch):
    mod, target = _enabled_mod(library, tmp_path)
    other_identity = uuid4().hex
    scan = library._storage.scan_deployments

    def reassign_after_scan(*args, **kwargs):
        inventory = scan(*args, **kwargs)
        write_deployment_identity(target, other_identity, "other-content")
        _replace_deployed_ini(target, mod.path)
        return inventory

    monkeypatch.setattr(library._storage, "scan_deployments", reassign_after_scan)

    result = library._storage.deploy_mod(mod.path, deploy_name=mod.folder_name)

    assert target.is_dir()
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert dict(iter_effective_inis(mod.path)) == {"mod.ini": SOURCE_INI}
    assert result is None


def test_undeploy_rechecks_identity_after_ini_capture(library, tmp_path, monkeypatch):
    mod, target = _enabled_mod(library, tmp_path)
    other_identity = uuid4().hex
    capture = library._storage._capture_newer_deployed_inis

    def reassign_after_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        write_deployment_identity(target, other_identity, "other-content")
        return result

    monkeypatch.setattr(library._storage, "_capture_newer_deployed_inis", reassign_after_capture)

    removed = library._storage.undeploy_mod(mod.path, deploy_path=target)

    assert target.is_dir()
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert removed is False


@pytest.mark.parametrize("marker_state", ["other", "invalid", "missing", "own"])
def test_recovery_only_removes_partial_owned_by_the_transaction(library, tmp_path, marker_state):
    mod = _import_mod(library, tmp_path)
    other = _import_mod(library, tmp_path, name="Other")
    preset = library.preset_manager.create_preset("game_a", "Enable Skin", snapshot={mod.node_id: [mod.mod_id]})
    plan = library.preset_manager.prepare_preset_apply(preset.id)
    (action,) = [action for action in plan.actions if action.action == "materialize_active"]
    partial = Path(action.partial_path)
    assert not partial.exists()
    library.preset_manager.mark_preset_apply_executing(plan.operation_id)
    partial.mkdir()
    (partial / "payload.buf").write_bytes(b"partially extracted payload")
    if marker_state in ("other", "own"):
        identity = other.mod_id if marker_state == "other" else mod.mod_id
        write_deployment_identity(partial, identity, "partial-content")
    elif marker_state == "invalid":
        (partial / ".d3dxmeta").mkdir()
        (partial / ".d3dxmod").write_text("{invalid json", encoding="utf-8")

    errors = library.preset_manager.recover_incomplete_applies("game_a")

    if marker_state in ("other", "invalid"):
        assert partial.is_dir(), "a foreign or unreadable marker must retain the recovery evidence"
        assert (partial / "payload.buf").read_bytes() == b"partially extracted payload"
        assert errors
        (journal,) = library._index.get_journals("game_a")
        assert journal.operation_id == plan.operation_id
        assert journal.phase == "recovery_required"
    else:
        assert not partial.exists()
        assert errors == []
        assert library._index.get_journals("game_a") == []
    assert Path(mod.path).is_file()
    assert Path(other.path).is_file()


@contextmanager
def _block_windows_delete(path):
    """持有允许读写但禁止删除的真实 Windows 文件句柄。"""
    import ctypes
    from ctypes import wintypes

    from src.utils import fs

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    flags = 0x02000000 if Path(path).is_dir() else 0x80  # BACKUP_SEMANTICS / NORMAL
    handle = kernel32.CreateFileW(fs.native_path(path), 0x80000000, 0x3, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    guard = threading.Lock()
    closed = False

    def release():
        nonlocal closed
        with guard:
            if not closed:
                if not kernel32.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                closed = True

    try:
        yield release
    finally:
        release()


def _retry_deployment(tmp_path):
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "Mods")
    target = tmp_path / "Mods" / "Deployment"
    target.mkdir()
    mod_id = uuid4().hex
    # 先创建标记再锁定载荷文件，以保持实际删除顺序。
    write_deployment_identity(target, mod_id, "original-content")
    (target / "locked.buf").write_bytes(b"locked payload")
    return storage, target, mod_id


def _remove_retry_deployment(storage, target, mod_id):
    return storage.undeploy_mod(
        target.parent / "missing-source.zip",
        deploy_path=target,
        mod_id=mod_id,
        capture_changes=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_delete_retry_succeeds_after_brief_windows_payload_lock(tmp_path, monkeypatch):
    storage, target, mod_id = _retry_deployment(tmp_path)
    original_unlink = os.unlink
    release_timer = None
    with _block_windows_delete(target / "locked.buf") as release:

        def observe_unlink(path, *args, **kwargs):
            nonlocal release_timer
            try:
                return original_unlink(path, *args, **kwargs)
            except PermissionError:
                if Path(path).name == "locked.buf" and release_timer is None:
                    # 确认发生删除拒绝后，在重试间隔内释放句柄。
                    release_timer = threading.Timer(0.01, release)
                    release_timer.start()
                raise

        monkeypatch.setattr(os, "unlink", observe_unlink)
        removed = _remove_retry_deployment(storage, target, mod_id)
        if release_timer is not None:
            release_timer.join(1)

    assert release_timer is not None, "the test must exercise an actual Windows sharing violation"
    assert not release_timer.is_alive()
    assert removed is True
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
@pytest.mark.parametrize("locked_part", ["payload", "marker", "metadata", "root"])
def test_delete_retry_keeps_identity_after_persistent_windows_lock(tmp_path, locked_part):
    storage, target, mod_id = _retry_deployment(tmp_path)
    (target / ".d3dxmeta").mkdir()
    paths = {
        "payload": target / "locked.buf",
        "marker": target / ".d3dxmod",
        "metadata": target / ".d3dxmeta",
        "root": target,
    }
    original_identity = read_deployment_identity(target)
    with _block_windows_delete(paths[locked_part]):
        assert _remove_retry_deployment(storage, target, mod_id) is False
        assert read_deployment_identity(target) == original_identity
        assert storage.scan_deployments(force=True)[mod_id].path == target

    assert _remove_retry_deployment(storage, target, mod_id) is True
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
@pytest.mark.parametrize("release_before_retry", [False, True])
def test_delete_retry_still_rejects_reassignment_after_sharing_violation(tmp_path, monkeypatch, release_before_retry):
    storage, target, mod_id = _retry_deployment(tmp_path)
    other_identity = uuid4().hex
    original_unlink = os.unlink
    reassigned = False

    def reassign_after_denied_unlink(path, *args, **kwargs):
        nonlocal reassigned
        try:
            return original_unlink(path, *args, **kwargs)
        except PermissionError:
            if Path(path).name == "locked.buf" and not reassigned:
                write_deployment_identity(target, other_identity, "other-content")
                reassigned = True
                if release_before_retry:
                    release()
            raise

    monkeypatch.setattr(os, "unlink", reassign_after_denied_unlink)
    with _block_windows_delete(target / "locked.buf") as release:
        assert _remove_retry_deployment(storage, target, mod_id) is False

    assert reassigned
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert (target / "locked.buf").read_bytes() == b"locked payload"
    assert _remove_retry_deployment(storage, target, mod_id) is False
    assert target.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_delete_retry_never_restores_marker_over_new_owner(tmp_path, monkeypatch):
    storage, target, mod_id = _retry_deployment(tmp_path)
    other_identity = uuid4().hex
    original_rmdir = os.rmdir
    reassigned = False

    def reassign_after_denied_rmdir(path, *args, **kwargs):
        nonlocal reassigned
        try:
            return original_rmdir(path, *args, **kwargs)
        except OSError:
            if Path(path).name == target.name and not reassigned:
                write_deployment_identity(target, other_identity, "other-content")
                reassigned = True
            raise

    monkeypatch.setattr(os, "rmdir", reassign_after_denied_rmdir)
    with _block_windows_delete(target):
        assert _remove_retry_deployment(storage, target, mod_id) is False

    assert reassigned
    assert read_deployment_identity(target)["mod_id"] == other_identity
    assert _remove_retry_deployment(storage, target, mod_id) is False
    assert target.is_dir()
