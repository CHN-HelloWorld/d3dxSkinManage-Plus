"""只读识别 Wallpaper Engine，并异步校验路径。"""

from __future__ import annotations

import ctypes
import json
import os
import re
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from src.utils.i18n import t as tr


def _version_string(path: Path, name: str) -> str:
    """读取 Windows 文件版本资源中的指定字符串字段，无对应资源时返回空字符串。"""
    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return ""
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        return ""
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
    for index in range(0, length.value // 2, 2):
        key = f"\\StringFileInfo\\{words[index]:04x}{words[index + 1]:04x}\\{name}"
        if version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)):
            return ctypes.wstring_at(pointer, max(0, length.value - 1))
    return ""


def validate_engine_path(path: str) -> tuple[bool, str]:
    """根据可执行资源和安装文件识别程序，不执行目标。"""
    if not path.strip():
        return False, tr("请选择 Wallpaper Engine 程序路径。")
    if os.name != "nt":
        return False, tr("WE 场景目前仅支持 Windows。")
    exe = Path(path)
    try:
        if not exe.is_file() or exe.name.lower() not in ("wallpaper64.exe", "wallpaper32.exe"):
            return False, tr("请选择安装目录中的 wallpaper64.exe 或 wallpaper32.exe。")
        with exe.open("rb") as stream:
            if stream.read(2) != b"MZ":
                return False, tr("所选文件不是有效的 Windows 程序。")
        if _version_string(exe, "ProductName").strip() != "Wallpaper Engine":
            return False, tr("未识别到 Wallpaper Engine 程序信息，请选择官方安装目录。")
        if _version_string(exe, "OriginalFilename").lower() != exe.name.lower():
            return False, tr("程序文件信息不匹配，请修复 Wallpaper Engine 安装。")
        metadata = json.loads((exe.parent / "version.json").read_text(encoding="utf-8-sig"))
        if not metadata.get("version") or not (exe.parent / "assets").is_dir() or not (exe.parent / "bin").is_dir():
            return False, tr("安装资源不完整，请通过 Steam 修复 Wallpaper Engine。")
    except (OSError, ValueError, AttributeError) as exc:
        return False, tr("无法读取 Wallpaper Engine 安装：{exc}", exc=exc)
    return True, tr("已识别 Wallpaper Engine，可选择场景素材。")


def discover_engine_path() -> str:
    """读取 Steam 注册表及库配置，返回首个通过安装校验的 Wallpaper Engine 路径。"""
    if os.name != "nt":
        return ""
    import winreg

    roots: set[Path] = set()
    for hive, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                roots.add(Path(winreg.QueryValueEx(handle, value)[0]))
        except OSError:
            pass
    for root in tuple(roots):
        try:
            content = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8")
            roots.update(Path(value.replace("\\\\", "\\")) for value in re.findall(r'"path"\s+"([^"]+)"', content))
        except OSError:
            pass
    for root in sorted(roots):
        for name in ("wallpaper64.exe", "wallpaper32.exe"):
            path = str(root / "steamapps/common/wallpaper_engine" / name)
            if validate_engine_path(path)[0]:
                return path
    return ""


class _Signals(QObject):
    """传递程序路径检查的代次、路径、有效性与提示消息。"""

    done = Signal(int, str, bool, str)


class _Check(QRunnable):
    """在线程池中发现或校验 Wallpaper Engine 安装路径。"""

    def __init__(self, generation: int, path: str, discover: bool = False):
        """保存任务代次和待检查路径，并设置是否先执行自动发现。"""
        super().__init__()
        self.generation, self.path, self.discover = generation, path, discover
        self.signals = _Signals()

    def run(self):
        """执行路径发现和校验，将异常转成失败消息后发出完成信号。"""
        path = self.path
        try:
            if self.discover:
                path = discover_engine_path()
            valid, message = validate_engine_path(path)
        except Exception as exc:
            valid, message = False, tr("路径检测失败：{exc}", exc=exc)
        self.signals.done.emit(self.generation, path, valid, message)


class EnginePathValidator(QObject):
    """对输入路径防抖检查，并定期重新校验有效安装，忽略过期任务结果。"""

    status_changed = Signal(str, str)
    path_found = Signal(str)

    def __init__(self, parent=None):
        """创建状态、任务保留表及输入防抖和定期复查计时器。"""
        super().__init__(parent)
        self.path, self.valid, self.status, self.message = "", False, "empty", tr("请选择 Wallpaper Engine 程序路径。")
        self._generation = 0
        self._tasks: dict[int, _Check] = {}
        self._discover_generation = -1
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.check_now)
        self._watch = QTimer(self)
        self._watch.setInterval(5000)
        self._watch.timeout.connect(self._recheck)

    def _publish(self, status, message):
        """同步状态、提示与有效标志，仅在展示状态变化时发出信号。"""
        changed = (self.status, self.message) != (status, message)
        self.status, self.message, self.valid = status, message, status == "valid"
        if changed:
            self.status_changed.emit(status, message)

    def set_path(self, path: str):
        """规范化输入路径、使旧任务失效，并为非空路径安排防抖检查。"""
        path = path.strip().strip('"')
        if path == self.path:
            return
        self.path = path
        self._generation += 1
        self._timer.stop()
        self._watch.stop()
        message = tr("正在检测程序路径…") if path else tr("请选择 Wallpaper Engine 程序路径。")
        self._publish("checking" if path else "empty", message)
        if path:
            self._timer.start(350)

    def check_now(self):
        """停止防抖计时器并立即提交当前非空路径的校验任务。"""
        self._timer.stop()
        if not self.path:
            return
        self._submit(False)

    def discover(self):
        """使旧任务失效并启动自动发现，同时发布正在查找的状态。"""
        self._generation += 1
        self._timer.stop()
        self._watch.stop()
        self._discover_generation = self._generation
        self._publish("checking", tr("正在查找 Wallpaper Engine…"))
        self._submit(True)

    def _recheck(self):
        """当前安装有效时提交复查，以检测路径或安装资源后续失效。"""
        if self.valid:
            self._submit(False)

    def _submit(self, discover):
        """每个代次最多保留一个检查任务，并将结果连接回对象所属线程。"""
        if self._generation in self._tasks:
            return
        task = _Check(self._generation, self.path, discover)
        task.signals.done.connect(self._done)
        self._tasks[self._generation] = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, str, bool, str)
    def _done(self, generation, path, valid, message):
        """释放任务引用并仅接收当前代次结果，发布发现路径及校验状态并调整复查计时。"""
        self._tasks.pop(generation, None)
        if generation != self._generation:
            return
        if generation == self._discover_generation:
            self._discover_generation = -1
            self.path = path
            if path:
                self.path_found.emit(path)
        elif path != self.path:
            return
        self._publish("valid" if valid else ("invalid" if path else "empty"), message)
        if valid:
            self._watch.start()
        else:
            self._watch.stop()
