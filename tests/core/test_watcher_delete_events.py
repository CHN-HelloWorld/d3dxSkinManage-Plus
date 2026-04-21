from __future__ import annotations

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from src.core.watcher import _FileEventHandler


def test_misclassified_deleted_directory_still_triggers_refresh(tmp_path):
    callbacks: list[tuple[str, str]] = []
    handler = _FileEventHandler(lambda event_type, path: callbacks.append((event_type, path)))
    deleted_container = tmp_path / "Character" / "FolderMod"

    event = FileDeletedEvent(str(deleted_container))
    assert event.is_directory is False
    handler.on_deleted(event)

    assert callbacks == [("deleted", str(deleted_container))]


def test_deleted_sidecar_noise_remains_filtered(tmp_path):
    callbacks: list[tuple[str, str]] = []
    handler = _FileEventHandler(lambda event_type, path: callbacks.append((event_type, path)))

    handler.on_deleted(FileDeletedEvent(str(tmp_path / "FolderMod" / ".d3dxmeta" / "info.json")))

    assert callbacks == []


def test_moved_events_report_both_old_and_new_path(tmp_path):
    """移动事件须同时报告源路径和目标路径，使局部对账能够认领新位置。"""
    callbacks: list[tuple[str, str]] = []
    handler = _FileEventHandler(lambda event_type, path: callbacks.append((event_type, path)))
    old_container = tmp_path / "Character" / "OldName"
    new_container = tmp_path / "Character" / "NewName"

    handler.on_moved(DirMovedEvent(str(old_container), str(new_container)))

    assert callbacks == [
        ("moved", str(old_container)),
        ("moved", str(new_container)),
    ]


def test_moved_metadata_noise_stays_filtered_on_both_sides(tmp_path):
    """元数据子树内的原子写改名（tmp → info.json）两端都不上报。

    这是本程序自己写 sidecar 的副产物，若放行会形成刷新风暴。
    """
    callbacks: list[tuple[str, str]] = []
    handler = _FileEventHandler(lambda event_type, path: callbacks.append((event_type, path)))
    meta = tmp_path / "FolderMod" / ".d3dxmeta"

    handler.on_moved(FileMovedEvent(str(meta / "info.json.tmp"), str(meta / "info.json")))

    assert callbacks == []


def test_created_events_report_path_for_scoped_reconcile(tmp_path):
    """新增事件须包含具体路径，使合并后的变化范围可用于局部对账。"""
    callbacks: list[tuple[str, str]] = []
    handler = _FileEventHandler(lambda event_type, path: callbacks.append((event_type, path)))
    dropped_dir = tmp_path / "Character" / "DroppedMod"
    dropped_archive = tmp_path / "Character" / "DroppedMod.7z"

    handler.on_created(DirCreatedEvent(str(dropped_dir)))
    handler.on_created(FileCreatedEvent(str(dropped_archive)))
    # 元数据副产物仍然不算 Mod 增减，绝不能进入 changed_paths
    handler.on_created(FileCreatedEvent(str(tmp_path / "Character" / "DroppedMod" / ".d3dxmod")))

    assert callbacks == [
        ("created", str(dropped_dir)),
        ("created", str(dropped_archive)),
    ]
