"""通过 Windows 图形捕获读取本应用创建的场景窗口。

进程操作、窗口发现和捕获清理均在界面线程之外执行，仅传递最新独立图像。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import threading
import time
import uuid
import weakref
from ctypes import wintypes
from pathlib import Path

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QImage

from src.utils.i18n import t as tr


def validate_engine_path(path):
    # 延迟导入原生后端，避免普通启动加载可选捕获模块。
    """延迟导入原生后端并返回引擎可执行文件的验证结果及错误说明。"""
    from .engine import validate_engine_path as validate

    return validate(path)


def validate_scene_project(path: str) -> Path:
    """验证场景项目类型、素材路径边界及文件存在性，返回规范化后的项目路径。

    允许场景配置存放在 scene.pkg 中；无效项目或读取失败均转换为可展示的 ValueError。
    """
    project = Path(path).expanduser().resolve()
    if project.name.lower() != "project.json" or not project.is_file():
        raise ValueError(tr("请选择场景素材的 project.json 文件"))
    try:
        data = json.loads(project.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("type"), str) or data["type"].casefold() != "scene":
            raise ValueError(tr("素材不是 Wallpaper Engine 场景"))
        filename = data.get("file")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(tr("场景素材未指定文件"))
        relative = Path(filename)
        target = (project.parent / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(project.parent):
            raise ValueError(tr("场景文件不能超出素材目录"))
        # 场景配置可位于场景包内，由 Wallpaper Engine 打开项目时解析。
        packed_scene = project.parent / "scene.pkg"
        packed = (
            relative.as_posix().lower() == "scene.json"
            and packed_scene.is_file()
            and packed_scene.resolve().is_relative_to(project.parent)
        )
        if not target.is_file() and not packed:
            raise ValueError(tr("场景素材文件不存在"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(tr("无法读取场景素材：{exc}", exc=exc)) from exc
    return project


class _Windows:
    """封装场景窗口查找、屏幕外布局及隐藏所有者窗口的 Win32 资源操作。"""

    def __init__(self):
        """加载 user32 接口并声明指针宽度正确的参数类型，初始化隐藏所有者句柄。"""
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.api.FindWindowW.restype = wintypes.HWND
        self.api.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.GetWindowLongW.restype = wintypes.LONG
        self.api.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        self.api.SetWindowLongW.restype = wintypes.LONG
        self.api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.api.CreateWindowExW.restype = wintypes.HWND
        self.api.DestroyWindow.argtypes = [wintypes.HWND]
        self.api.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetWindow.restype = wintypes.HWND
        self._set_owner = self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.api.SetWindowLongW
        self._set_owner.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_owner.restype = ctypes.c_ssize_t
        self._owner = None

    def find(self, name):
        """按唯一窗口标题查找场景窗口，未发现时返回空句柄。"""
        return self.api.FindWindowW(None, name)

    def offscreen_x(self):
        """返回虚拟桌面右侧之外的横坐标，供场景窗口离屏显示。"""
        return self.api.GetSystemMetrics(76) + self.api.GetSystemMetrics(78) + 100

    def resize(self, hwnd, size):
        # 隐藏所有者窗口可移除任务栏入口，无需使用会影响图形捕获资格的工具窗口标志。
        """创建隐藏所有者并调整场景窗口样式、位置和尺寸，使其可捕获且不显示任务栏入口。

        原生接口失败时抛出系统错误，由捕获线程统一处理和回收。
        """
        if self._owner is None:
            self._owner = self.api.CreateWindowExW(0, "STATIC", "", 0x80000000, 0, 0, 0, 0, None, None, None, None)
            if not self._owner:
                raise ctypes.WinError(ctypes.get_last_error())
        if self.api.GetWindow(hwnd, 4) != self._owner:  # GW_OWNER
            self.api.ShowWindow(hwnd, 0)
            ctypes.set_last_error(0)
            previous = self._set_owner(hwnd, -8, self._owner)  # GWLP_HWNDPARENT
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        style = self.api.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        hidden_style = style & ~0x40000
        if style != hidden_style:
            ctypes.set_last_error(0)
            previous = self.api.SetWindowLongW(hwnd, -20, hidden_style)
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.SetWindowPos(hwnd, 1, self.offscreen_x(), 0, *size, 0x0010 | 0x0040 | 0x0020):
            raise OSError(tr("无法调整场景窗口大小"))

    def dispose(self):
        """销毁本对象创建的隐藏所有者窗口并清空句柄。"""
        if self._owner:
            self.api.DestroyWindow(self._owner)
            self._owner = None

    def close(self, name):
        # 每次按会话身份重新查找，避免使用已被复用的窗口句柄。
        """按唯一标题重新查找场景窗口并投递关闭消息，避免误用已复用的旧句柄。"""
        hwnd = self.find(name)
        if hwnd:
            self.api.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


class _Session:
    """保存单个场景捕获任务的线程共享状态，并负责引擎进程通信和完整资源清理。"""

    OPEN_TIMEOUT = 20.0
    RETRY_AFTER = 5.0
    CLOSE_GRACE = 2.0

    def __init__(self, engine_path, scene_path, notify):
        """记录引擎和项目路径、通知回调及唯一窗口身份，创建取消事件和最新帧互斥锁。"""
        self.engine_path = engine_path
        self.scene_path = scene_path
        self.notify = notify
        self.name = f"d3dxSkinManage-background-{uuid.uuid4().hex}"
        self.cancel = threading.Event()
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.size = (1, 1)
        self.fps = 30
        self.volume = 0
        self.last_copy = 0.0
        self.last_frame = 0.0
        self.failure = None

    def command(self, *args, timeout=5, properties=None):
        """重新验证引擎后以隐藏进程执行控制命令，并等待指定超时内完成。

        属性参数使用引擎要求的原始 JSON 分隔格式；非零退出码与超时向调用方传播。
        """
        valid, message = validate_engine_path(self.engine_path)
        if not valid:
            raise ValueError(message)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        command = [self.engine_path, "-control", *args]
        if properties is not None:
            # 引擎从原始命令行分隔符读取 JSON，不能使用会额外转义引号的参数编码。
            command = (
                subprocess.list2cmdline(command)
                + " -properties RAW~("
                + json.dumps(properties, separators=(",", ":"))
                + ")~END"
            )
        subprocess.run(
            command,
            cwd=str(Path(self.engine_path).parent),
            timeout=timeout,
            startupinfo=startup,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def apply_volume(self, volume):
        """向本任务的场景窗口发送音量属性，避免影响其他引擎窗口。"""
        self.command("applyProperties", "-location", self.name, properties={"volume": volume})

    def on_capture_frame(self, frame, control):
        """限制同步捕获回调频率，避免已完成 GPU 读回后才丢帧。

        单帧池不会在等待时积压；事件等待释放 GIL，并可在停止时立即唤醒。
        """
        self.on_frame(frame, control)
        if not self.cancel.is_set():
            remaining = self.last_copy + 1 / self.fps - time.perf_counter()
            if remaining > 0:
                self.cancel.wait(remaining)

    def on_frame(self, frame, control):
        """在捕获回调中按帧率筛选图像，复制像素到独立 QImage 并替换最新帧。

        兼容带填充的行跨度；复制失败记录错误并唤醒取消事件，供工作线程统一清理。
        """
        if self.cancel.is_set():
            return
        # 使用高精度计时，避免部分系统的单调时钟精度不足以调度视频帧。
        now = time.perf_counter()
        self.last_frame = time.monotonic()
        # 显示定时器负责最终帧率，此处预留调度余量，避免接近周期边界的帧被交替丢弃。
        if now - self.last_copy < 0.9 / self.fps:
            return
        try:
            image = QImage(frame.width, frame.height, QImage.Format.Format_RGB32)
            if image.isNull():
                raise ValueError(tr("场景捕获返回空画面"))
            pixels = frame.frame_buffer
            if pixels.strides[0] == image.bytesPerLine():
                # 原生复制期间释放 GIL，捕获回调保留源像素，目标图像独立拥有存储。
                destination = image.bits()
                ctypes.memmove(int(shiboken6.VoidPtr(destination)), pixels.ctypes.data, image.sizeInBytes())
            else:
                # GPU 行跨度可能包含填充，使用支持跨度且释放 GIL 的复制，避免逐行 Python 循环。
                import numpy as np

                destination = np.ndarray(
                    (frame.height, frame.width, 4),
                    dtype=np.uint8,
                    buffer=image.bits(),
                    strides=(image.bytesPerLine(), 4, 1),
                )
                np.copyto(destination, pixels)
            with self.lock:
                if not self.cancel.is_set():
                    self.latest = image
                    self.last_copy = now
        except Exception as exc:
            self.failure = tr("场景捕获失败：{exc}", exc=exc)
            self.cancel.set()

    def on_closed(self):
        """将非主动关闭的捕获通道记为错误，并通知工作线程停止。"""
        if not self.cancel.is_set():
            self.failure = tr("场景捕获已意外关闭")
            self.cancel.set()

    def run(self):
        """在工作线程中验证项目、启动并发现场景窗口，然后维持捕获与尺寸音量更新。

        启动和断帧均有超时限制；退出时停止捕获、关闭本任务窗口、释放图像并通知完成。
        """
        windows = capture = control = None
        opened = False
        found = False
        deadline = 0.0
        try:
            project = validate_scene_project(self.scene_path)
            if self.cancel.is_set():
                return
            windows = _Windows()
            # 打开窗口前加载依赖，避免缺少模块时遗留原生来源窗口。
            from windows_capture import WindowsCapture

            if self.cancel.is_set():
                return
            size = self.size
            args = (
                "openWallpaper",
                "-file",
                str(project),
                "-playInWindow",
                self.name,
                "-width",
                str(size[0]),
                "-height",
                str(size[1]),
                "-x",
                str(windows.offscreen_x()),
                "-y",
                "0",
                "-borderless",
            )
            started = time.monotonic()
            deadline = started + self.OPEN_TIMEOUT
            opened = True  # 通信超时后仍可能创建窗口。
            self.command(*args)
            retried = False
            hwnd = None
            while not self.cancel.is_set():
                hwnd = windows.find(self.name)
                if hwnd:
                    found = True
                    break
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError(tr("Wallpaper Engine 未在 20 秒内创建场景窗口"))
                if not retried and now - started >= self.RETRY_AFTER:
                    retried = True
                    self.command(*args, timeout=max(0.1, min(5, deadline - now)))
                self.cancel.wait(0.1)
            if self.cancel.is_set():
                return
            windows.resize(hwnd, self.size)
            applied_volume = self.volume
            self.apply_volume(applied_volume)
            capture = WindowsCapture(window_hwnd=int(hwnd), cursor_capture=False, draw_border=False)
            capture.frame_handler = self.on_capture_frame
            capture.closed_handler = self.on_closed
            control = capture.start_free_threaded()
            self.last_frame = time.monotonic()
            applied_size = self.size
            while not self.cancel.wait(0.1):
                if not windows.find(self.name):
                    raise RuntimeError(tr("Wallpaper Engine 场景窗口已关闭"))
                if time.monotonic() - self.last_frame > 20:
                    raise TimeoutError(tr("场景长时间未返回画面，请重试"))
                if self.size != applied_size:
                    applied_size = self.size
                    windows.resize(hwnd, applied_size)
                if self.volume != applied_volume:
                    applied_volume = self.volume
                    self.apply_volume(applied_volume)
        except Exception as exc:
            if not self.cancel.is_set():
                self.failure = str(exc)
        finally:
            self.cancel.set()
            if self.failure:
                self.notify("error", self.failure)
            if control is not None:
                try:
                    control.stop()
                except Exception:
                    pass
            if opened and windows is not None:
                self.cleanup(windows, deadline if not found else 0)
            if windows is not None:
                windows.dispose()
            with self.lock:
                self.latest = None
            self.closed.set()
            self.notify("finished", None)

    def cleanup(self, windows, pending_deadline):
        # 启动中的请求不能撤回，取消后仍跟踪创建过程并仅关闭当前会话窗口，包括界面已销毁的情况。
        """关闭本任务的场景窗口，并在启动宽限期内持续处理可能迟到的窗口创建。

        只按唯一窗口身份操作；引擎校验或控制失败时仍尝试发送原生关闭消息。
        """
        until = max(pending_deadline, time.monotonic() + self.CLOSE_GRACE)
        windows.close(self.name)
        try:
            self.command("closeWallpaper", "-location", self.name)
        except Exception:
            # 不得执行磁盘内容已发生变化的引擎。
            pass
        while time.monotonic() < until:
            windows.close(self.name)
            time.sleep(0.1)
        windows.close(self.name)
        if windows.find(self.name):
            try:
                self.command("closeWallpaper", "-location", self.name)
            except Exception:
                pass
