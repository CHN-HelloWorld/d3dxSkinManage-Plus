"""文件监控服务"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from src.utils import fs
from src.utils.constants import (
    ARCHIVE_EXTENSIONS,
    CATEGORY_MARKER_FILE,
    META_DIR,
    MOD_CONTAINER_MARKER,
)
from src.utils.logger import logger

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog 未安装，文件监控功能不可用")


# 把归档扩展名转为小写集合，方便快速匹配
_ARCHIVE_EXT_SET = {e.lower() for e in ARCHIVE_EXTENSIONS}


@dataclass(frozen=True)
class FileChange:
    """携带文件事件的类型、路径、监听代次、批次及可选原生身份依据。"""

    root: str
    generation: int
    kind: str
    path: str
    destination: str = ""
    is_directory: bool = False
    native_evidence: str = ""
    evidence_mod_id: str = ""
    batch_id: int = 0
    root_identity: tuple = ()


def _is_relevant_path(
    path: str,
    *,
    is_directory: bool,
    path_may_have_disappeared: bool = False,
) -> bool:
    """按路径判定是否值得通知 UI 刷新（``_is_relevant_event`` 的按路径版本）。"""
    src = (path or "").replace("\\", "/")
    if not src:
        return False
    parts = src.split("/")
    if MOD_CONTAINER_MARKER in parts or META_DIR in parts:
        return False
    if is_directory:
        return True
    # Windows ReadDirectoryChangesW 在快速递归删除/移动目录时，经常只能上报
    # FileDeletedEvent/FileMovedEvent，此时 watchdog 会把目录误标为 is_directory=False。
    # 路径消失后无法查询原始类型，因此这两类事件仍需触发防抖检查，
    # 避免遗漏目录资源的变化；元数据事件已单独过滤。
    if path_may_have_disappeared:
        return True
    return any(src.lower().endswith(ext) for ext in _ARCHIVE_EXT_SET)


def _is_relevant_event(
    event: FileSystemEvent,
    *,
    path_may_have_disappeared: bool = False,
) -> bool:
    """判断目录事件是否需要通知界面刷新。

    资源目录及受支持的归档变化参与判断，其他叶子文件忽略。
    容器标记和元数据写入不表示资源增减，须过滤以避免重复重建。
    """
    return _is_relevant_path(
        event.src_path or "",
        is_directory=bool(event.is_directory),
        path_may_have_disappeared=path_may_have_disappeared,
    )


class _FileEventHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
    """文件系统事件处理器"""

    def __init__(self, callback: Callable[[str, str], None], *, detailed_callback=None) -> None:
        """保存简要和详细事件回调，并在可用时初始化 watchdog 基类。"""
        if WATCHDOG_AVAILABLE:
            super().__init__()
        self._callback = callback
        self._detailed_callback = detailed_callback

    def _detail(self, kind, event):
        """向详细回调发送事件类型、来源、目标及目录标记。"""
        if self._detailed_callback is not None:
            self._detailed_callback(
                kind, event.src_path, getattr(event, "dest_path", "") or "", bool(event.is_directory)
            )

    def on_created(self, event: FileSystemEvent) -> None:
        """转发身份标记创建和相关文件创建事件，简要回调仅接收相关文件。"""
        if Path(event.src_path).name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE):
            self._detail("created", event)
        if not _is_relevant_event(event):
            return
        self._detail("created", event)
        self._callback("created", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """在路径可能已消失的条件下筛选删除事件，并转发标记及相关路径变化。"""
        if Path(event.src_path).name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE):
            self._detail("deleted", event)
        if not _is_relevant_event(event, path_may_have_disappeared=True):
            return
        self._detail("deleted", event)
        self._callback("deleted", event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """检查移动两端及身份标记，发送完整移动事件并分别通知相关来源与目标路径。"""
        dest = getattr(event, "dest_path", "") or ""
        if (
            _is_relevant_event(event, path_may_have_disappeared=True)
            or (dest and _is_relevant_path(dest, is_directory=bool(event.is_directory), path_may_have_disappeared=True))
            or Path(event.src_path).name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE)
            or (dest and Path(dest).name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE))
        ):
            self._detail("moved", event)
        if _is_relevant_event(event, path_may_have_disappeared=True):
            self._callback("moved", event.src_path)
        # 同时上报移动目标，使对账覆盖新位置，避免将已移动的容器误判为缺失。
        dest = getattr(event, "dest_path", "") or ""
        if dest and _is_relevant_path(
            dest,
            is_directory=bool(event.is_directory),
            path_may_have_disappeared=True,
        ):
            self._callback("moved", dest)

    def on_modified(self, event: FileSystemEvent) -> None:
        """仅将容器或分类身份标记的内容修改转发给详细回调。"""
        if Path(event.src_path).name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE):
            self._detail("modified", event)


class FileWatcher(QObject):
    """文件系统监控器

    监控 Mods 目录文件变化，发送 Qt 信号通知 UI 更新。
    """

    directory_created = Signal(str)  # 新文件夹路径
    directory_deleted = Signal(str)  # 删除的文件夹路径
    directory_moved = Signal(str)  # 移动的文件夹路径
    change_detected = Signal()  # 通用变更信号
    path_changed = Signal(object)  # 成对路径、监视根及代际

    def __init__(self, parent: QObject | None = None, *, track_containers: bool = True) -> None:
        """初始化 Qt 文件监听器、容器身份跟踪选项和监听代次状态。"""
        super().__init__(parent)
        self._track_containers = track_containers
        self._observer = None
        self._watch_path: Path | None = None
        self._running = False
        self.generation = 0

    def start(self, path: str | Path) -> bool:
        """开始监控目录

        参数：
            path: 要监控的目录路径
        """
        if not WATCHDOG_AVAILABLE:
            logger.error("watchdog 未安装，无法启动文件监控")
            return False

        self.stop()
        self._watch_path = Path(path)
        if not fs.exists(self._watch_path):
            logger.error("监控目录不存在: %s", self._watch_path)
            return False

        self.generation += 1
        generation, root = self.generation, str(self._watch_path)
        native = os.name == "nt"
        handler = _FileEventHandler(
            (lambda *args: None) if native else self._on_event,
            detailed_callback=None
            if native
            else lambda kind, path, dest, directory: self.path_changed.emit(
                FileChange(root, generation, kind, path, dest, directory)
            ),
        )
        if os.name == "nt":
            from src.core.watcher_windows import create_observer

            self._observer = create_observer(
                lambda kind, path: self.path_changed.emit(FileChange(root, generation, kind, path)),
                native_callback=lambda kind, path, dest, directory, evidence, identity, batch, root_id: (
                    self.path_changed.emit(
                        FileChange(root, generation, kind, path, dest, directory, evidence, identity, batch, root_id)
                    )
                ),
                track_containers=self._track_containers,
            )
        else:
            self._observer = Observer()
        try:
            self._observer.schedule(handler, str(self._watch_path), recursive=True)
            self._observer.daemon = True
            self._observer.start()
        except Exception:
            self.stop()
            raise
        self._running = True
        logger.info("文件监控已启动: %s", self._watch_path)
        return True

    def stop(self) -> None:
        """停止监控"""
        self.generation += 1
        observer, self._observer = self._observer, None
        self._running = False
        if observer is not None:
            observer.stop()
            # 启动失败时事件读取器可能已运行，即使分发线程尚不能等待退出，也须先停止读取器。
            if observer.ident is not None:
                observer.join(timeout=5)
            logger.info("文件监控已停止")

    def is_running(self) -> bool:
        """是否正在监控"""
        return self._running

    def _on_event(self, event_type: str, path: str) -> None:
        """处理文件事件"""
        logger.debug("文件事件: %s - %s", event_type, path)

        if event_type == "created":
            self.directory_created.emit(path)
        elif event_type == "deleted":
            self.directory_deleted.emit(path)
        elif event_type == "moved":
            self.directory_moved.emit(path)

        self.change_detected.emit()
