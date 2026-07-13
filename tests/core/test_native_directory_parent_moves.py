import os
import time

import pytest

from src.core.ini_sync import IniSyncManager
from src.core.mod_package import override_path_for
from tests.core.test_ini_sync import _INI, _archive_container, _deploy_dir, _register

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows directory handle behavior")


def test_deployment_native_stream_allows_parent_rename(tmp_path):
    from src.core.watcher import FileWatcher

    parent = tmp_path / "game"
    root = parent / "Mods"
    root.mkdir(parents=True)
    watcher = FileWatcher(track_containers=False)
    try:
        assert watcher.start(root)
        moved = tmp_path / "renamed game"
        parent.rename(moved)
        assert (moved / "Mods").is_dir()
    finally:
        watcher.stop()


@pytest.mark.parametrize("side", ["deployment", "source"])
def test_ini_sync_allows_parent_rename_and_retains_unfiltered_ini_events(tmp_path, side):
    source_parent = tmp_path / "library home"
    deploy_parent = tmp_path / "game home"
    body = _archive_container(source_parent, "M")
    deploy = _deploy_dir(deploy_parent, "M", _INI)
    manager = IniSyncManager(enable_watch=True)
    try:
        assert _register(manager, body, deploy) == 1
        parent = deploy.parent if side == "deployment" else source_parent
        moved = parent.with_name(parent.name + " moved")
        parent.rename(moved)
        if side == "deployment":
            deploy = moved / "M"
        else:
            body = moved / "M" / "M.zip"
    finally:
        manager.stop()

    # 移动后由原生事件驱动根目录恢复和重新登记，不主动扫描或直接调用对账。
    manager = IniSyncManager(enable_watch=True, sweep_seconds=0)
    try:
        assert _register(manager, body, deploy) == 1
        changed = "[KeySwap]\nkey = F9\n"
        (deploy / "merged.ini").write_text(changed, encoding="utf-8")
        override = override_path_for(body, "merged.ini")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if override.exists() and override.read_text(encoding="utf-8") == changed:
                break
            time.sleep(0.02)
        assert override.read_text(encoding="utf-8") == changed
    finally:
        manager.stop()


def test_parent_filter_preserves_moves_across_logical_boundary_and_parent_removal(tmp_path):
    from watchdog.observers import winapi

    from src.core.watcher_windows import _ParentWatchEvents

    filtered = _ParentWatchEvents(str(tmp_path / "Mods"))
    event = winapi.WinAPINativeEvent
    records = filtered.filter(
        [
            event(winapi.FILE_ACTION_MODIFIED, r"ModsSibling\ignore.ini"),
            event(winapi.FILE_ACTION_MODIFIED, r"Mods\a.ini"),
            event(winapi.FILE_ACTION_RENAMED_OLD_NAME, r"Mods\a.ini"),
            event(winapi.FILE_ACTION_RENAMED_NEW_NAME, r"Mods\b.ini"),
            event(winapi.FILE_ACTION_RENAMED_OLD_NAME, r"Mods\b.ini"),
            event(winapi.FILE_ACTION_RENAMED_NEW_NAME, "outside.ini"),
            event(winapi.FILE_ACTION_RENAMED_OLD_NAME, "outside.ini"),
        ]
    )
    records += filtered.filter([event(winapi.FILE_ACTION_RENAMED_NEW_NAME, r"Mods\c.ini")])
    assert [(row.action, row.src_path) for row in records] == [
        (winapi.FILE_ACTION_MODIFIED, "a.ini"),
        (winapi.FILE_ACTION_RENAMED_OLD_NAME, "a.ini"),
        (winapi.FILE_ACTION_RENAMED_NEW_NAME, "b.ini"),
        (winapi.FILE_ACTION_REMOVED, "b.ini"),
        (winapi.FILE_ACTION_CREATED, "c.ini"),
    ]
    removed = filtered.filter([event(winapi.FILE_ACTION_DELETED_SELF, ".")])
    assert len(removed) == 1 and removed[0].is_removed_self


def test_shared_physical_parent_keeps_other_ini_watch_after_unregistration(tmp_path):
    body1 = _archive_container(tmp_path / "library", "One")
    body2 = _archive_container(tmp_path / "library", "Two")
    deploy1 = _deploy_dir(tmp_path / "game", "One", _INI)
    deploy2 = _deploy_dir(tmp_path / "game", "Two", _INI)
    manager = IniSyncManager(enable_watch=True, sweep_seconds=0)
    try:
        assert manager.register_mod("one", str(body1), str(deploy1)) == 1
        assert manager.register_mod("two", str(body2), str(deploy2)) == 1
        manager.unregister_mod("one")
        changed = "[KeySwap]\nkey = F8\n"
        # 编辑器原子替换文件时须保留目标 INI 的变化事件。
        temporary = deploy2 / "replacement.tmp"
        temporary.write_text(changed, encoding="utf-8")
        temporary.replace(deploy2 / "merged.ini")
        override = override_path_for(body2, "merged.ini")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if override.exists() and override.read_text(encoding="utf-8") == changed:
                break
            time.sleep(0.02)
        assert override.read_text(encoding="utf-8") == changed
        assert not override_path_for(body1, "merged.ini").exists()
    finally:
        manager.stop()
