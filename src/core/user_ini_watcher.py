"""监控 3DMigoto 用户 INI，通知上层采集运行变量。

以非递归方式监视所在目录，再按文件名筛选，以覆盖文件尚未创建、覆盖写入和删除。
库目录监控及部署 INI 同步具有不同的范围和职责，不共享该监听目标。

监控回调和防抖线程仅处理路径及通知，不访问 Qt 或数据库。
调用方须将需要主线程执行的操作通过信号转发。文件删除也会通知，但读取无内容时
保留已有偏好，不将缺失解释为变量归零。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from src.utils import fs
from src.utils.logger import logger

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


def _name_key(path: str | Path) -> str:
    """规范化用于比较的文件名，Windows 下忽略大小写。"""
    return os.path.normcase(os.path.basename(str(path)))


class UserIniWatcher:
    """监控当前游戏的用户 INI，并合并短时间内的变化通知。

    同时仅保留一个目标，切换时移除旧监控再注册新目录；timer_factory 可提供计时器实现。
    """

    def __init__(
        self,
        on_changed: Callable[[], None],
        *,
        debounce_seconds: float = 0.25,
        enable_watch: bool = True,
        timer_factory=threading.Timer,
    ) -> None:
        """绑定变化回调，初始化目标文件监听及可注入定时器的防抖状态。"""
        self.on_changed = on_changed
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._enable_watch = enable_watch
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._observer = None
        self._watch_handle = None
        self._target: Path | None = None
        self._watched_dir: Path | None = None
        self._timer = None
        self._deadline = 0.0
        self._stopped = False

    # 目标管理

    @property
    def target(self) -> Path | None:
        """在锁内返回当前监听目标，未设置时返回 None。"""
        with self._lock:
            return self._target

    def watch(self, path: str | Path | None) -> bool:
        """把监视目标切到 ``path``（None/空 → 停止监视）。

        目标文件**允许尚不存在**：盯的是它所在目录，等 3DMigoto 第一次保存时自然收到事件。
        目录不存在（未配 3DMigoto 路径 / 路径填错）→ 不注册，返回 False。
        """
        if not path:
            self._unwatch()
            return False
        target = Path(fs.normal_path(path))
        with self._lock:
            if self._stopped:
                return False
            if self._target == target and self._watch_handle is not None:
                return True  # 已在监视同一个文件 → 幂等
            self._target = target
        directory = target.parent
        if not fs.is_dir(directory):
            logger.debug("d3dx_user.ini 所在目录不存在，暂不监视: %s", directory)
            self._unwatch(keep_target=True)
            return False
        return self._install(directory)

    def _install(self, directory: Path) -> bool:
        """非递归监控指定目录，同一目录已有监控时复用。"""
        if not WATCHDOG_AVAILABLE or not self._enable_watch:
            return False
        with self._lock:
            if self._watch_handle is not None and self._watched_dir == directory:
                return True
            self._teardown_watch_locked()
            if self._observer is None:
                self._observer = Observer()
                self._observer.daemon = True
                self._observer.start()
            try:
                self._watch_handle = self._observer.schedule(
                    _UserIniEventHandler(self), str(directory), recursive=False
                )
            except Exception as e:
                logger.warning("监视 d3dx_user.ini 目录失败 (%s): %s", directory, e)
                self._watch_handle = None
                return False
            self._watched_dir = directory
        logger.info("已开始监视 d3dx_user.ini: %s", self._target)
        return True

    def _unwatch(self, *, keep_target: bool = False) -> None:
        """撤销现有监听，按参数决定是否保留目标文件位置。"""
        with self._lock:
            self._teardown_watch_locked()
            if not keep_target:
                self._target = None

    def _teardown_watch_locked(self) -> None:
        """清空监听目录和句柄并尝试取消观察任务，取消异常不影响状态释放。"""
        handle, self._watch_handle = self._watch_handle, None
        self._watched_dir = None
        if handle is None or self._observer is None:
            return
        try:
            self._observer.unschedule(handle)
        except Exception:
            pass

    # 事件 → 防抖

    def notify_path(self, path: str | Path) -> None:
        """watchdog 线程调：只认目标文件名，其它一律丢弃。"""
        with self._lock:
            target = self._target
            if self._stopped or target is None:
                return
            if _name_key(path) != _name_key(target):
                return
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """单一活跃 Timer 防抖 —— 整文件重写会连发 create/modify/moved 多个事件。

        照 ``IniSyncManager.schedule_reconcile_paths`` 的范式：新事件只延后 deadline，
        不为每个事件新建/取消线程。
        """
        timer_to_start = None
        with self._lock:
            if self._stopped:
                return
            self._deadline = time.monotonic() + self._debounce_seconds
            if self._timer is None:
                timer_to_start = self._timer = self._timer_factory(self._debounce_seconds, self._flush)
                timer_to_start.daemon = True
        if timer_to_start is not None:
            timer_to_start.start()

    def _flush(self) -> None:
        """防抖到点：静默窗口未满则单线程续期，满了才回调一次。"""
        timer_to_start = None
        with self._lock:
            if self._stopped:
                return
            remaining = self._deadline - time.monotonic()
            if remaining > 0:
                timer_to_start = self._timer = self._timer_factory(remaining, self._flush)
                timer_to_start.daemon = True
            else:
                self._timer = None
                self._deadline = 0.0
        if timer_to_start is not None:
            timer_to_start.start()
            return
        cb = self.on_changed
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            # 隔离回调异常，保持后续定时通知可用。
            logger.warning("d3dx_user.ini 变更回调抛错: %s", e)

    # 生命周期

    def stop(self) -> None:
        """退出时调：取消待执行防抖 + 停 observer（幂等）。"""
        with self._lock:
            self._stopped = True
            timer, self._timer = self._timer, None
            self._deadline = 0.0
            self._teardown_watch_locked()
            observer, self._observer = self._observer, None
            self._target = None
        if timer is not None:
            timer.cancel()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=3)
            except Exception:
                pass


if WATCHDOG_AVAILABLE:

    class _UserIniEventHandler(FileSystemEventHandler):
        """目录内任意事件 → 交 manager 按文件名过滤（含 moved 的目标路径）。"""

        def __init__(self, watcher: UserIniWatcher) -> None:
            """绑定用户 INI 监听器，供文件系统事件转发目标变化。"""
            super().__init__()
            self._w = watcher

        def on_any_event(self, event) -> None:
            """将非目录事件的来源和目标路径交给监听器判断是否需要通知。"""
            if getattr(event, "is_directory", False):
                return
            for attr in ("src_path", "dest_path"):
                path = getattr(event, attr, "") or ""
                if path:
                    self._w.notify_path(path)
else:

    class _UserIniEventHandler:  # type: ignore[no-redef]
        """在监听依赖缺失时保留事件处理器构造接口。"""

        def __init__(self, watcher) -> None:
            """保存监听器引用，不建立外部文件系统事件订阅。"""
            self._w = watcher
