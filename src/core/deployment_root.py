"""跟踪部署根目录的文件系统身份，区分目录移动、对象缺失和未配置状态。"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from typing import Literal

from src.utils import fs


@dataclass(frozen=True)
class DeploymentRootResolution:
    """保存部署根解析状态、当前有效路径以及用于比较的原配置路径。"""

    status: Literal["current", "moved", "missing", "unset"]
    path: str
    previous_path: str


@dataclass
class _TrackedRoot:
    """保存单个游戏的配置路径、目录身份、可选原生句柄及创建时间。"""

    configured_path: str
    identity: tuple[int, int] | None = None
    handle: int | None = None
    birth_time: int | None = None


def _normal_path(path: str) -> str:
    """将非空路径转换为规范的绝对路径，保留空字符串表示未配置。"""
    return os.path.abspath(fs.normal_path(path)) if path else ""


def _identity(info: os.stat_result) -> tuple[int, int]:
    """从文件状态中提取设备号和文件编号，用于比较目录对象身份。"""
    return info.st_dev, info.st_ino


class DeploymentRootTracker:
    """在当前进程中跟踪目录对象及其父目录移动。

    修改配置时显式替换跟踪对象；重复应用相同配置不会绑定原路径上的新目录。
    跨卷复制后删除会产生新对象，因此视为原对象缺失。所有者关闭时须释放句柄。
    """

    def __init__(self) -> None:
        """建立受重入锁保护的跟踪表，并在 Windows 上初始化句柄查询接口。"""
        self._roots: dict[str, _TrackedRoot] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._api = None
        if os.name == "nt":
            from src.core.watcher_windows import _NativeAPI

            self._api = _NativeAPI()
            c, w = self._api.c, self._api.w

            class StandardInformation(c.Structure):
                """映射 Windows 文件标准信息结构，用于识别目录类型和待删除状态。"""

                _fields_ = [
                    ("allocation", c.c_longlong),
                    ("end", c.c_longlong),
                    ("links", w.DWORD),
                    ("delete_pending", c.c_ubyte),
                    ("directory", c.c_ubyte),
                ]

            self._standard_information = StandardInformation

    def _release(self, record: _TrackedRoot) -> None:
        """关闭记录持有的原生或文件描述符句柄，并清空句柄避免重复释放。"""
        if record.handle is not None:
            if self._api is not None:
                self._api.k.CloseHandle(record.handle)
            else:
                os.close(record.handle)
            record.handle = None

    def track(self, game_id: str, configured_path: str) -> None:
        """配置改变时重新绑定目录身份；配置相同时保留原对象，打开失败时标记不可用。"""
        configured_path = _normal_path(configured_path)
        with self._lock:
            if self._closed:
                return
            old = self._roots.get(game_id)
            if old is not None and old.configured_path == configured_path:
                return
            if old is not None:
                self._release(old)
            record = self._roots[game_id] = _TrackedRoot(configured_path)
            if not configured_path:
                return
            try:
                info = os.stat(fs.native_path(configured_path))
                if not stat.S_ISDIR(info.st_mode):
                    return
                record.identity = _identity(info)
                record.birth_time = getattr(info, "st_birthtime_ns", None)
                if self._api is not None:
                    try:
                        # 按路径打开子目录可能阻止父目录改名，即使启用了共享删除；
                        # 因此仅保留按文件标识打开的句柄。
                        record.handle = self._api.open_by_id(configured_path, info.st_ino)
                    except OSError as error:
                        if getattr(error, "winerror", None) not in (1, 50, 87, 120):
                            raise
                        # 不支持按标识打开的存储提供者，在配置路径上核对身份，不持有路径句柄。
                    if record.handle is not None and self._api.identity(record.handle) != record.identity:
                        raise OSError("deployment directory changed while opening")
                elif os.path.isdir("/proc/self/fd"):
                    record.handle = os.open(configured_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                    if _identity(os.fstat(record.handle)) != record.identity:
                        raise OSError("deployment directory changed while opening")
            except (OSError, ValueError):
                self._release(record)
                record.identity = None

    def _current_path(self, record: _TrackedRoot) -> str:
        """通过保留句柄解析目录当前位置；对象已删除时释放句柄并返回空路径。"""
        if record.handle is None:
            return record.configured_path
        if self._api is None:
            if os.fstat(record.handle).st_nlink == 0:
                self._release(record)
                record.identity = None
                return ""
            return _normal_path(os.readlink(f"/proc/self/fd/{record.handle}"))
        api = self._api
        info = self._standard_information()
        if not api.k.GetFileInformationByHandleEx(
            record.handle,
            1,
            api.c.byref(info),
            api.c.sizeof(info),
        ):
            raise api.c.WinError(api.c.get_last_error())
        if info.delete_pending or not info.directory:
            self._release(record)
            record.identity = None
            return ""
        buffer = api.c.create_unicode_buffer(32768)
        length = api.k.GetFinalPathNameByHandleW(record.handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("could not resolve deployment directory handle")
        return _normal_path(buffer.value)

    def resolve(self, game_id: str, configured_path: str) -> DeploymentRootResolution:
        """核对目录身份并返回原位、已移动、缺失或未配置结果；访问错误视为缺失。"""
        configured_path = _normal_path(configured_path)
        with self._lock:
            self.track(game_id, configured_path)
            if not configured_path:
                return DeploymentRootResolution("unset", "", "")
            missing = DeploymentRootResolution("missing", "", configured_path)
            record = self._roots.get(game_id)
            if record is None or record.identity is None:
                return missing
            try:
                current = self._current_path(record)
                if not current:
                    return missing
                info = os.stat(fs.native_path(current))
                if not stat.S_ISDIR(info.st_mode) or _identity(info) != record.identity:
                    return missing
                if record.handle is None and record.birth_time is not None:
                    if getattr(info, "st_birthtime_ns", None) != record.birth_time:
                        return missing
            except (OSError, ValueError):
                return missing
            status = "current" if os.path.normcase(current) == os.path.normcase(configured_path) else "moved"
            return DeploymentRootResolution(status, current, configured_path)

    def forget(self, game_id: str) -> None:
        """释放已删除游戏的目录，或允许显式选择新的目录对象。"""
        with self._lock:
            record = self._roots.pop(game_id, None)
            if record is not None:
                self._release(record)

    def close(self) -> None:
        """在锁内关闭所有目录句柄并清空记录，阻止后续建立新的跟踪。"""
        with self._lock:
            self._closed = True
            for record in self._roots.values():
                self._release(record)
            self._roots.clear()
