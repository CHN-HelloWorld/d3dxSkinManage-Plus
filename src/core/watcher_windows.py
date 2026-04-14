"""Windows 文件监控适配层，保留原生通知溢出信息。

直接读取系统缓冲并发布父目录移动，后代处理交给索引中的子树对账，避免递归枚举移动目录。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from src.utils import fs


class _NativeAPI:
    """封装目录监听和文件身份查询所需的 Windows 原生接口及 ctypes 数据结构。"""

    def __init__(self):
        """声明内核函数签名并构造文件身份结构，确保原生调用使用正确参数与返回类型。"""
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.k = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]),
            "CloseHandle": (w.BOOL, [w.HANDLE]),
            "OpenFileById": (w.HANDLE, [w.HANDLE, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p, w.DWORD]),
            "ReadFile": (w.BOOL, [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]),
            "GetFileInformationByHandleEx": (w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
            "GetFileInformationByHandle": (w.BOOL, [w.HANDLE, c.c_void_p]),
            "GetFinalPathNameByHandleW": (w.DWORD, [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]),
            "CreateEventW": (w.HANDLE, [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]),
            "ReadDirectoryChangesW": (
                w.BOOL,
                [w.HANDLE, c.c_void_p, w.DWORD, w.BOOL, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p],
            ),
            "GetOverlappedResult": (w.BOOL, [w.HANDLE, c.c_void_p, c.POINTER(w.DWORD), w.BOOL]),
            "CancelIoEx": (w.BOOL, [w.HANDLE, c.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            method = getattr(self.k, name)
            method.restype, method.argtypes = result, arguments

        # 复用 ctypes 结构定义，减少重复描述符和垃圾回收环；每次读取使用独立缓冲。
        class Identifier(c.Union):
            """映射文件编号联合体，容纳传统及扩展文件身份。"""

            _fields_ = [("file_id", c.c_longlong), ("extended", c.c_byte * 16)]

        class Descriptor(c.Structure):
            """映射按身份打开文件时使用的编号描述结构。"""

            _fields_ = [("size", w.DWORD), ("kind", c.c_int), ("identifier", Identifier)]

        class Information(c.Structure):
            """映射句柄文件信息，读取卷序号及高低位文件编号。"""

            _fields_ = [
                ("attributes", w.DWORD),
                ("times", w.DWORD * 6),
                ("volume", w.DWORD),
                ("size_high", w.DWORD),
                ("size_low", w.DWORD),
                ("links", w.DWORD),
                ("index_high", w.DWORD),
                ("index_low", w.DWORD),
            ]

        self.Identifier, self.Descriptor, self.Information = Identifier, Descriptor, Information

        class FileIdInformation(c.Structure):
            """映射包含卷序号和 128 位文件编号的扩展身份信息。"""

            _fields_ = [("volume", c.c_ulonglong), ("identifier", c.c_ubyte * 16)]

        self.FileIdInformation = FileIdInformation

    def open(self, path, *, observe=False):
        """以共享访问模式打开路径，监听模式启用重叠读写；失败时抛出原生错误。"""
        handle = self.k.CreateFileW(
            fs.native_path(path), 1 if observe else 0x80, 7, None, 3, 0x02000000 | (0x40000000 if observe else 0), None
        )
        if handle == self.c.c_void_p(-1).value:
            raise self.c.WinError(self.c.get_last_error())
        return handle

    def open_by_id(self, path, file_id, volume_hint=None, *, access=0x80, share=7):
        """按文件标识持有句柄，不锁定祖先目录名称。

        Windows 上按路径打开后代可能阻止祖先改名，因此基线不保留这种句柄。
        """
        c = self.c
        descriptor = self.Descriptor(c.sizeof(self.Descriptor), 0, self.Identifier(file_id=file_id))
        temporary = volume_hint is None
        if temporary:
            volume_hint = self.open(Path(path).anchor)
        try:
            handle = self.k.OpenFileById(volume_hint, c.byref(descriptor), access, share, None, 0x02000000)
            if handle in (None, 0, c.c_void_p(-1).value):
                raise c.WinError(c.get_last_error())
            return handle
        finally:
            if temporary:
                self.k.CloseHandle(volume_hint)

    def read_marker_by_id(self, path, info, volume_hint=None):
        """按标识读取，避免子路径句柄阻止父目录改名。"""
        handle = self.open_by_id(path, info.st_ino, volume_hint, access=0x80000000)
        try:
            if self.identity(handle) != (info.st_dev, info.st_ino):
                raise OSError("marker identity changed before read")
            buffer = self.c.create_string_buffer(info.st_size + 1)
            size = self.w.DWORD()
            if not self.k.ReadFile(handle, buffer, len(buffer), self.c.byref(size), None):
                raise self.c.WinError(self.c.get_last_error())
            if size.value != info.st_size:
                raise OSError("marker size changed during read")
            return buffer.raw[: size.value].decode("utf-8")
        finally:
            self.k.CloseHandle(handle)

    def identity(self, handle):
        """读取句柄的卷和文件编号，优先使用扩展身份并在不可用时回退到传统接口。"""
        c = self.c
        # CPython 3.12 起使用 64 位卷序列号和 128 位文件标识，须采用相同结构比较身份。
        if sys.version_info >= (3, 12):
            extended = self.FileIdInformation()
            if self.k.GetFileInformationByHandleEx(handle, 18, c.byref(extended), c.sizeof(extended)):
                return extended.volume, int.from_bytes(bytes(extended.identifier), "little")
        info = self.Information()
        if not self.k.GetFileInformationByHandle(handle, c.byref(info)):
            raise c.WinError(c.get_last_error())
        return info.volume, (info.index_high << 32) | info.index_low


@lru_cache(maxsize=2)
def _metadata_api(api_type):
    """创建指定原生接口类型的实例，供缓存复用元数据读取接口。"""
    return api_type()


def read_marker_text(path):
    """短期读取标记，不阻止删除或祖先改名。"""
    api = _metadata_api(_NativeAPI)
    info = os.stat(fs.native_path(path))
    try:
        return api.read_marker_by_id(path, info)
    except OSError as error:
        # 不支持按标识打开的文件系统可使用路径读取，访问、共享和并发变化错误仍须向上传播。
        if getattr(error, "winerror", None) not in (1, 50, 87, 120):
            raise
        return fs.read_text(path, encoding="utf-8")


def open_marker(path):
    """返回以文件标识打开的二进制流，不锁定目录名称。"""
    import msvcrt

    api = _metadata_api(_NativeAPI)
    info = os.stat(fs.native_path(path))
    try:
        handle = api.open_by_id(path, info.st_ino, access=0x80000000)
    except OSError as error:
        if getattr(error, "winerror", None) not in (1, 50, 87, 120):
            raise
        return open(fs.native_path(path), "rb")
    try:
        if api.identity(handle) != (info.st_dev, info.st_ino):
            raise OSError("marker identity changed before opening")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.k.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class NativeContainerHandles:
    """后台保留目录句柄基线，允许改名和删除。"""

    def __init__(self, volume_hint=None):
        """初始化原生接口、卷提示、容器句柄表和受锁保护的路径树。"""
        self.api = _NativeAPI()
        self.volume_hint = volume_hint
        self._lock = threading.RLock()
        self._handles = {}
        self._paths = {}
        self.closed = False

    def _node(self, path, create=False):
        """按规范绝对路径逐级查找路径树节点，可按需创建缺失节点。"""
        node = self._paths
        for part in Path(os.path.abspath(fs.normal_path(path))).parts:
            key = os.path.normcase(part)
            children = node.setdefault("children", {}) if create else node.get("children", {})
            if key not in children:
                if not create:
                    return None
                children[key] = {}
            node = children[key]
        return node

    def relocate_prefix(self, source, destination):
        """移动路径树分支，后代继续使用原句柄记录。"""
        source, destination = Path(source), Path(destination)
        with self._lock:
            parent = self._node(source.parent)
            branch = parent.get("children", {}).pop(os.path.normcase(source.name), None) if parent else None
            if branch is None:
                return
            target = self._node(destination.parent, True).setdefault("children", {})
            displaced = target.get(os.path.normcase(destination.name))
            if displaced is not None and displaced is not branch:
                pending = [displaced]
                while pending:
                    node = pending.pop()
                    record = node.get("record")
                    if record:
                        self._handles.pop(record[0], None)
                        self.api.k.CloseHandle(record[0])
                    pending.extend(node.get("children", {}).values())
            target[os.path.normcase(destination.name)] = branch

    def containing_mod(self, path):
        """查询已登记的 Mod 祖先，不访问磁盘或遍历同级句柄。"""
        parts = Path(os.path.abspath(fs.normal_path(path))).parts
        with self._lock:
            node = self._paths
            for offset, part in enumerate(parts):
                node = node.get("children", {}).get(os.path.normcase(part))
                if node is None:
                    return None
                if node.get("kind") == "mod_id" and node.get("record"):
                    return str(Path(*parts[: offset + 1]))
        return None

    def track(self, path):
        """按文件编号打开容器并核对目录及标记版本，稳定后登记持久身份句柄。

        已登记或已关闭时跳过；读取失败或版本变化不产生身份凭据，并释放未采用的句柄。"""
        path = fs.normal_path(path)
        with self._lock:
            node = self._node(path)
            if self.closed or (node and node.get("record")):
                return
        handle = None
        try:
            before = os.stat(fs.native_path(path))
            identity_before = (before.st_dev, before.st_ino)
            handle = self.api.open_by_id(path, before.st_ino, self.volume_hint)
            if self.api.identity(handle) != identity_before:
                return
            marker_path = Path(path) / ".d3dxmod"
            identity_key = "mod_id"
            if not fs.is_file(marker_path):
                marker_path = Path(path) / ".d3dxcategory"
                identity_key = "id"
            marker_before = os.stat(fs.native_path(marker_path))
            marker = json.loads(self.api.read_marker_by_id(marker_path, marker_before, self.volume_hint))
            after = os.stat(fs.native_path(path))
            marker_after = os.stat(fs.native_path(marker_path))

            def version(info):
                """提取标记文件编号、大小与时间，核对标记读取期间是否变化。"""
                return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

            if (after.st_dev, after.st_ino) != identity_before or version(marker_before) != version(marker_after):
                return
            identity = marker.get(identity_key)
            if not identity:
                return
            with self._lock:
                node = self._node(path, True)
                if not self.closed and not node.get("record"):
                    record = (handle, identity)
                    self._handles[handle] = record
                    node["record"] = record
                    node["kind"] = identity_key
                    handle = None
        except (OSError, ValueError, AttributeError):
            return
        finally:
            if handle is not None:
                self.api.k.CloseHandle(handle)

    def seed(self, root, stop):
        """遍历分类树建立容器句柄基线，跳过受管正文内部并响应停止事件。"""
        pending = [Path(root)]
        while pending and not stop.is_set():
            directory = pending.pop()
            if fs.is_file(directory / ".d3dxmod"):
                self.track(directory)
                continue  # 不枚举受管 Mod 本体内部内容。
            if fs.is_file(directory / ".d3dxcategory"):
                self.track(directory)
            try:
                with os.scandir(fs.native_path(directory)) as entries:
                    for entry in entries:
                        if stop.is_set():
                            return
                        if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                            pending.append(directory / entry.name)
            except OSError:
                continue  # 未登记目标不能产生确定身份的证据。

    def inspect(self, path):
        """仅报告明确的待删除状态或实际移动位置，不推断缺失。"""
        api = self.api
        c, w = api.c, api.w

        class Standard(c.Structure):
            """映射文件标准信息，用于从句柄判断对象是否待删除及是否为目录。"""

            _fields_ = [
                ("allocation", c.c_longlong),
                ("end", c.c_longlong),
                ("links", w.DWORD),
                ("delete_pending", c.c_ubyte),
                ("directory", c.c_ubyte),
            ]

        with self._lock:
            node = self._node(path)
            record = node.get("record") if node else None
            if record is None:
                return "", "", ""
            handle, identity = record
            info = Standard()
            if not api.k.GetFileInformationByHandleEx(handle, 1, c.byref(info), c.sizeof(info)):
                return "", "", ""
            if info.delete_pending:
                del self._handles[handle]
                node.pop("record", None)
                api.k.CloseHandle(handle)  # 清理前释放最后的本地引用。
                return "delete_pending", "", identity
            buffer = c.create_unicode_buffer(32768)
            length = api.k.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                return "", "", ""
            target = fs.normal_path(buffer.value)
            if os.path.normcase(target) != os.path.normcase(fs.normal_path(path)):
                self.relocate_prefix(path, target)
                return "relocated", target, identity
            return "", "", identity

    def close(self):
        """在锁内标记关闭、释放全部容器句柄并清空路径索引。"""
        with self._lock:
            self.closed = True
            for handle, _ in self._handles.values():
                self.api.k.CloseHandle(handle)
            self._handles.clear()
            self._paths.clear()


class _ParentWatchEvents:
    """监控直接父目录，并将事件转换回逻辑根路径。

    目录通知不能使用按标识打开的句柄；父目录路径句柄允许该父目录移动，
    更高祖先仍受 Windows 命名空间规则限制。
    """

    def __init__(self, path, *, recursive=True):
        """将监听定位到目标父目录，并记录子目录前缀和递归过滤策略。"""
        root = Path(os.path.abspath(fs.normal_path(path)))
        self.path = str(root.parent if root.name else root)
        self._prefix = root.name
        self._recursive = recursive
        self._old = None

    def _relative(self, path):
        """把父目录事件路径裁剪为目标内相对路径，范围外或超出递归策略时返回 None。"""
        if not self._prefix:
            relative = path
        elif os.path.normcase(path) == os.path.normcase(self._prefix):
            relative = ""
        elif os.path.normcase(path).startswith(os.path.normcase(self._prefix) + os.sep):
            relative = path[len(self._prefix) + 1 :]
        else:
            return None
        return relative if self._recursive or os.sep not in relative else None

    def filter(self, events):
        """过滤父目录事件并配对重命名两端，跨边界移动转为创建或删除事件。"""
        from watchdog.observers import winapi

        result = []

        def emit(action, path):
            """追加有效范围内的原生事件，将目标根删除转换为自身删除事件。"""
            if path is None:
                return
            if not path and action == winapi.FILE_ACTION_REMOVED:
                action = winapi.FILE_ACTION_DELETED_SELF
            result.append(winapi.WinAPINativeEvent(action, path))

        for event in events:
            if event.is_removed_self:
                self._old = None
                emit(winapi.FILE_ACTION_DELETED_SELF, "")
                continue
            relative = self._relative(event.src_path)
            if event.is_renamed_new and self._old is not None:
                old = self._relative(self._old)
                self._old = None
                if old is not None and relative is not None:
                    emit(winapi.FILE_ACTION_RENAMED_OLD_NAME, old)
                    emit(winapi.FILE_ACTION_RENAMED_NEW_NAME, relative)
                else:
                    emit(winapi.FILE_ACTION_REMOVED, old)
                    emit(winapi.FILE_ACTION_CREATED, relative)
                continue
            if self._old is not None:
                emit(winapi.FILE_ACTION_REMOVED, self._relative(self._old))
                self._old = None
            if event.is_renamed_old:
                self._old = event.src_path
            elif event.is_renamed_new:
                emit(winapi.FILE_ACTION_CREATED, relative)
            else:
                emit(event.action, relative)
        return result


def create_file_observer():
    """保留完整文件事件，同时允许逻辑根的父目录移动。"""
    from watchdog.observers import winapi
    from watchdog.observers.api import BaseObserver
    from watchdog.observers.read_directory_changes import WindowsApiEmitter

    class ParentDirectoryEmitter(WindowsApiEmitter):
        """从目标父目录接收事件并按目标范围过滤，避免监听句柄固定目标路径。"""

        def on_thread_start(self):
            """创建父目录事件过滤器并打开实际监听目录句柄。"""
            self._parent_events = _ParentWatchEvents(self.watch.path, recursive=self.watch.is_recursive)
            self._whandle = winapi.get_directory_handle(fs.native_path(self._parent_events.path))

        def _read_events(self):
            """递归读取父目录原生事件，再筛选目标目录范围；句柄缺失时返回空列表。"""
            if not self._whandle:
                return []
            events = winapi.read_events(self._whandle, self._parent_events.path, recursive=True)
            return self._parent_events.filter(events)

    return BaseObserver(ParentDirectoryEmitter)


class _ArmedDirectoryRead:
    """基线读取前启动原生通知，不写入探测标记。"""

    def __init__(self, root, recursive, flags):
        """建立重叠读取结构、事件句柄和目录缓冲区，并立即提交首次读取请求。"""
        self.api = _NativeAPI()
        c, w = self.api.c, self.api.w

        class Overlapped(c.Structure):
            """映射 Windows 重叠读写状态及完成事件句柄。"""

            _fields_ = [
                ("internal", c.c_size_t),
                ("high", c.c_size_t),
                ("offset", w.DWORD),
                ("offset_high", w.DWORD),
                ("event", w.HANDLE),
            ]

        self.overlapped = Overlapped()
        self.handle = None
        self.closed = False
        self.overlapped.event = self.api.k.CreateEventW(None, True, False, None)
        try:
            if not self.overlapped.event:
                raise c.WinError(c.get_last_error())
            self.handle = self.api.open(root, observe=True)
            self.buffer = c.create_string_buffer(65536)
            self.recursive, self.flags = recursive, flags
            self.arm()
        except Exception:
            self.close()
            raise

    def arm(self):
        """提交异步目录变化读取，接受已挂起状态，其余原生失败继续抛出。"""
        api = self.api
        if not api.k.ReadDirectoryChangesW(
            self.handle,
            self.buffer,
            len(self.buffer),
            self.recursive,
            self.flags,
            None,
            api.c.byref(self.overlapped),
            None,
        ):
            error = api.c.get_last_error()
            if error != 997:
                raise api.c.WinError(error)

    def read(self):
        """等待本次读取完成，复制缓冲区后立即重新挂起监听，并返回数据及有效长度。"""
        api, size = self.api, self.api.w.DWORD()
        if not api.k.GetOverlappedResult(self.handle, api.c.byref(self.overlapped), api.c.byref(size), True):
            raise api.c.WinError(api.c.get_last_error())
        data = self.buffer.raw
        if not self.closed:
            self.arm()
        return data, size.value

    def close(self):
        """标记读取器关闭，取消在途读写并释放目录和完成事件句柄。"""
        if not self.closed:
            self.closed = True
            if self.handle is not None:
                self.api.k.CancelIoEx(self.handle, None)
                self.api.k.CloseHandle(self.handle)
            if self.overlapped.event:
                self.api.k.CloseHandle(self.overlapped.event)


def create_observer(health_callback, *, native_callback=None, track_containers=True):
    """创建具有根身份核验、批次边界和监听健康通知的 Windows 文件观察器。"""
    from watchdog.events import (
        DirCreatedEvent,
        DirModifiedEvent,
        DirMovedEvent,
        FileCreatedEvent,
        FileDeletedEvent,
        FileModifiedEvent,
        FileMovedEvent,
    )
    from watchdog.observers import winapi
    from watchdog.observers.api import BaseObserver
    from watchdog.observers.read_directory_changes import WindowsApiEmitter

    class HealthyEmitter(WindowsApiEmitter):
        """将原生目录事件转换为带身份依据的变化通知，并报告监听失效状态。"""

        def _detail(self, kind, path, destination="", directory=False, evidence="", identity=""):
            """过滤无关变化，将正文内部事件合并为容器修改，并附加批次与根身份通知。"""
            if native_callback is not None:
                controls = {"overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline", "batch_end"}
                if kind not in controls:
                    from src.core.watcher import _is_relevant_path

                    handles = getattr(self, "_container_handles", None)
                    container = handles.containing_mod(path) if handles else None
                    target_container = handles.containing_mod(destination) if handles and destination else None
                    if (
                        target_container
                        and os.path.normcase(target_container) != os.path.normcase(destination)
                        and target_container != container
                    ):
                        self._detail("modified", os.path.join(target_container, ".d3dxmod"))
                    if container and os.path.normcase(container) != os.path.normcase(path):
                        if kind == "rename_unpaired":
                            return
                        # 每个通知缓冲内按所属 Mod 合并本体变化，不将原始子路径或子项改名交给界面。
                        path = os.path.join(container, ".d3dxmod")
                        kind, destination, directory, evidence, identity = "modified", "", False, "", ""
                        seen = getattr(self, "_body_changes", None)
                        if seen is None:
                            seen = self._body_changes = set()
                        key = os.path.normcase(container)
                        if key in seen:
                            return
                        seen.add(key)
                    elif Path(path).name not in {".d3dxmod", ".d3dxcategory"}:
                        if kind == "modified":
                            return
                        if kind != "rename_unpaired" and not any(
                            _is_relevant_path(
                                value, is_directory=directory, path_may_have_disappeared=kind in {"deleted", "moved"}
                            )
                            for value in (path, destination)
                            if value
                        ):
                            return
                native_callback(
                    kind,
                    path,
                    destination,
                    directory,
                    evidence,
                    identity,
                    getattr(self, "_batch", 0),
                    getattr(self, "_root_identity", ()),
                )

        def queue_event(self, event):
            # 原生回调负责投递，不向无操作处理器的第二队列重复发送事件。
            """未提供原生回调时投递 watchdog 事件，避免两套通知重复发送。"""
            if native_callback is None:
                super().queue_event(event)

        def _health(self, kind):
            """清除待配对重命名并发送根目录健康事件，按回调模式转发通知。"""
            self._old = ""
            self._detail(kind, self.watch.path)
            if native_callback is None:
                health_callback(kind, self.watch.path)

        def on_thread_start(self):
            """记录根身份并挂起目录读取，按选项启动容器身份基线线程；失败时清理资源。"""
            try:
                info = os.stat(fs.native_path(self.watch.path))
                self._root_identity = (info.st_dev, info.st_ino)
                self._batch, self._old = 0, ""
                actual_path = self.watch.path
                if not track_containers:
                    self._parent_events = _ParentWatchEvents(self.watch.path, recursive=self.watch.is_recursive)
                    actual_path = self._parent_events.path
                self._reader = _ArmedDirectoryRead(
                    actual_path, self.watch.is_recursive, winapi.WATCHDOG_FILE_NOTIFY_FLAGS
                )
                if track_containers:
                    self._seed_stop = threading.Event()
                    self._container_handles = NativeContainerHandles(self._reader.handle)
                    self._seed_thread = threading.Thread(
                        target=self._container_handles.seed,
                        args=(self.watch.path, self._seed_stop),
                        daemon=True,
                        name="source-handle-baseline",
                    )
                    self._seed_thread.start()
            except Exception:
                self._health("watch_failed")
                self.on_thread_stop()
                raise

        def on_thread_stop(self):
            """停止基线扫描，关闭读取器和容器句柄，并限时等待扫描线程退出。"""
            if hasattr(self, "_seed_stop"):
                self._seed_stop.set()
            if hasattr(self, "_reader"):
                self._reader.close()
            if hasattr(self, "_container_handles"):
                self._container_handles.close()
            thread = getattr(self, "_seed_thread", None)
            if thread is not None and thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

        def queue_events(self, timeout):
            """读取并校验一个原生事件批次，配对移动并利用容器句柄判断删除依据。

            根替换、离线或读取失败会报告健康异常并停止监听，正常批次最终发送完成边界。"""
            try:
                reader = getattr(self, "_reader", None)
                buf, size = (
                    reader.read()
                    if reader is not None
                    else winapi.read_directory_changes(
                        self._whandle, self.watch.path, recursive=self.watch.is_recursive
                    )
                )
                if not self.should_keep_running():
                    return
                expected = getattr(self, "_root_identity", None)
                if expected is not None:
                    info = os.stat(fs.native_path(self.watch.path))
                    if expected != (info.st_dev, info.st_ino):
                        self._health("root_replaced")
                        self.stop()
                        return
                if not size:
                    self._health("zero_bytes")
                    return
                events = [
                    winapi.WinAPINativeEvent(action, relative)
                    for action, relative in winapi._parse_event_buffer(buf, size)
                ]
                parent_events = getattr(self, "_parent_events", None)
                if parent_events is not None:
                    events = parent_events.filter(events)
                    if not events:
                        return
                self._batch = getattr(self, "_batch", 0) + 1
                self._body_changes = set()
                old = getattr(self, "_old", "")
                handles = getattr(self, "_container_handles", None)
                for event in events:
                    relative = event.src_path
                    path = os.path.join(self.watch.path, relative)
                    if event.is_renamed_old:
                        if old:
                            self._detail("rename_unpaired", old)
                        old = path
                    elif event.is_renamed_new:
                        isdir = fs.is_dir(path)
                        # 目录创建事件可能早于标记发布，在原子发布标记后登记已提交名称。
                        if handles and handles.containing_mod(path) is None:
                            handles.track(path if isdir else Path(path).parent)
                        if old:
                            if handles:
                                handles.inspect(old)
                                handles.relocate_prefix(old, path)
                            self._detail("moved", old, path, isdir)
                            self.queue_event((DirMovedEvent if isdir else FileMovedEvent)(old, path))
                            old = ""
                        else:
                            self._detail("created", path, directory=isdir)
                            self.queue_event((DirCreatedEvent if isdir else FileCreatedEvent)(path))
                    elif event.is_added:
                        if handles and handles.containing_mod(path) is None:
                            handles.track(path if fs.is_dir(path) else Path(path).parent)
                        self._detail("created", path, directory=fs.is_dir(path))
                        self.queue_event((DirCreatedEvent if fs.is_dir(path) else FileCreatedEvent)(path))
                    elif event.is_modified:
                        if (
                            handles
                            and handles.containing_mod(path) is None
                            and Path(path).name in {".d3dxmod", ".d3dxcategory"}
                        ):
                            handles.track(Path(path).parent)
                        self._detail("modified", path, directory=fs.is_dir(path))
                        self.queue_event((DirModifiedEvent if fs.is_dir(path) else FileModifiedEvent)(path))
                    elif event.is_removed:
                        evidence, destination, identity = handles.inspect(path) if handles else ("", "", "")
                        if evidence == "relocated":
                            prefix = os.path.normcase(self.watch.path).rstrip("\\") + "\\"
                            if os.path.normcase(destination).startswith(prefix):
                                self._detail("moved", path, destination, True, evidence, identity)
                                self.queue_event(DirMovedEvent(path, destination))
                            else:
                                self._detail("deleted", path, evidence="outside_root", identity=identity)
                                self.queue_event(FileDeletedEvent(path))
                        else:
                            self._detail("deleted", path, evidence=evidence, identity=identity)
                            self.queue_event(FileDeletedEvent(path))
                    elif event.is_removed_self:
                        self._health("root_offline")
                        self.stop()
                self._old = old
                if old:
                    self._detail("rename_unpaired", old)
                self._detail("batch_end", self.watch.path)
            except OSError:
                if self.should_keep_running():
                    self._health("watch_failed")
                    self.stop()

    return BaseObserver(HealthyEmitter)
