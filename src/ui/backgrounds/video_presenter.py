"""视频显示独立于业务界面事件循环和 GIL。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from weakref import WeakSet

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from src.ui import theme
from src.ui.backgrounds.foreground import ForegroundCaptureEffect
from src.ui.backgrounds.process_protocol import (
    LatestImageSender,
    PacketReader,
    decode_image,
    decode_json,
    encode_json,
)
from src.ui.backgrounds.repaint_coalescer import WindowRepaintCoalescer
from src.ui.backgrounds.shared_image import SharedImageWriter
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import is_frozen
from src.utils.qt_timers import call_later

_live_presenters = set()
_connected_apps = WeakSet()


def _renderer_command(pipe: str) -> list[str]:
    """根据源码或打包运行环境生成渲染子进程命令，传入管道名称和父进程标识。"""
    args = ["--background-renderer", pipe, str(os.getpid())]
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve().parents[3] / "run_app.py"), *args]


def _finish_presenters():
    """应用退出时停止全部呈现器，在统一宽限期后终止未退出的子进程并回收资源。"""
    presenters = list(_live_presenters)
    for presenter in presenters:
        presenter.stop()
    deadline = time.monotonic() + 1.5
    for presenter in presenters:
        process = presenter._process
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        presenter._finish()


def create_presenter(overlay, parent):
    """仅在 Windows 原生 Qt 平台且具备父控件时创建独立呈现器，否则返回 None。"""
    app = QApplication.instance()
    if (
        sys.platform != "win32"
        or app is None
        or app.platformName() != "windows"
        or app.property("backgroundRenderProcess")
        or overlay.parentWidget() is None
    ):
        return None
    return NativeVideoPresenter(overlay, parent)


class NativeVideoPresenter(QObject):
    """管理独立视频渲染进程，通过本地管道和共享图像传递配置、前景像素及播放状态。"""

    state_changed = Signal(str, str)
    preview_ready = Signal(QImage)

    def __init__(self, overlay, owner):
        # 页面销毁后仍保留控制对象，直到子进程退出。
        """创建本地服务、隐藏渲染进程和前景捕获效果，并绑定窗口变化与退出清理。

        子进程连接和退出由定时器监控；启动失败会关闭已创建的服务及日志资源。
        """
        super().__init__()
        self.overlay = overlay
        self.window = overlay.window()
        self.root = overlay.parentWidget()
        # 对话框创建可能使中心控件成为原生窗口，合成器应放在稳定前景根内部。
        self.root.winId()
        self._socket = None
        self._process = None
        self._log = None
        self._stopped = False
        self._last_modal_event = None
        self._pending_error = None
        self._suspended = False
        self._paused = False
        self._options = {}
        self._deadline = time.monotonic() + 15
        self._reader = PacketReader()
        self._images = SharedImageWriter()
        self._sender = LatestImageSender(self._write, image_writer=self._images)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._connected)
        pipe = f"d3dx-background-{os.getpid()}-{uuid.uuid4().hex}"
        if not self._server.listen(pipe):
            raise OSError(self._server.errorString())
        try:
            self._log = tempfile.TemporaryFile()
            # 光栅合成需要 CPU 图像，避免循环或跳转时重复等待 GPU 读回；显式选择的解码后端保持不变。
            environment = os.environ.copy()
            environment.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", ",")
            self._process = subprocess.Popen(
                _renderer_command(pipe),
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=self._log,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            self._server.close()
            if self._log is not None:
                self._log.close()
            self.deleteLater()
            raise
        overlay._native_presenter = self
        # 真实输入树只绘制一次，辅助进程接收透明像素，不触发第二次渲染。
        self._capture_effect = ForegroundCaptureEffect(self.root)
        self._capture_effect.frame_ready.connect(self._frame_ready)
        self.root.setGraphicsEffect(self._capture_effect)
        for widget in {self.root, self.window, self.overlay}:
            widget.installEventFilter(self)
        self._repaint_coalescer = WindowRepaintCoalescer(self.window, self.root, self._capture_effect)
        owner.destroyed.connect(self.stop)
        _live_presenters.add(self)
        app = QCoreApplication.instance()
        if app not in _connected_apps:
            _connected_apps.add(app)
            app.aboutToQuit.connect(_finish_presenters)
        self._poll_timer.start(100)

    def configure(self, options):
        """保存背景配置副本，并请求前景重绘以同步最新几何尺寸与呈现参数。"""
        self._options = dict(options)
        self.refresh()

    def playback(self, action):
        """更新暂停状态、丢弃过期前景图像，并向渲染进程发送播放操作。"""
        self._paused = action == "pause"
        self._sender.discard_pending()
        self._send({"action": action})
        self.refresh()

    def set_suspended(self, suspended):
        """更新窗口挂起标志并请求向渲染进程同步可见性。"""
        self._suspended = suspended
        self.refresh()

    def refresh(self):
        """丢弃待发送旧帧并请求前景刷新；挂起时立即发送配置而不等待隐藏窗口绘制。"""
        if self._stopped:
            return
        self._sender.discard_pending()
        if self._socket is not None:
            configuration = self._configuration()
            if configuration["suspended"]:
                # 最小化时立即处理，隐藏窗口不再等待绘制。
                self._send(configuration)
                return
        self.root.update()

    def eventFilter(self, watched, event):
        """随窗口尺寸、可见性和原生句柄变化刷新呈现配置，并合并重复的模态阻塞通知。"""
        if self._stopped:
            return False
        kind = event.type()
        if watched is self.root and kind in (QEvent.Type.WindowBlocked, QEvent.Type.WindowUnblocked):
            # 保留首次完整子树通知，避免原生窗口重复转发同一变化；不拦截 Qt 清除按下状态所需的事件。
            if kind == self._last_modal_event:
                return True
            self._last_modal_event = kind
        if kind in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.WindowStateChange,
            QEvent.Type.DevicePixelRatioChange,
            QEvent.Type.WinIdChange,
        ):
            self.refresh()
        return False

    def _connected(self):
        """接受唯一渲染进程连接，绑定读取事件并停止接收其他客户端。"""
        socket = self._server.nextPendingConnection()
        if self._stopped or self._socket is not None:
            socket.abort()
            socket.deleteLater()
            return
        self._socket = socket
        self._deadline = None
        socket.readyRead.connect(self._read)
        self._server.close()
        self.refresh()

    def _write(self, packet):
        """在连接可用且呈现器未停止时将协议数据包写入本地套接字。"""
        if self._socket is not None and not self._stopped:
            self._socket.write(packet)

    def _send(self, message):
        """将控制消息编码为 JSON 协议数据包后写入渲染进程连接。"""
        self._write(encode_json(message))

    def _configuration(self):
        """收集背景参数、主题语言、原生父窗口和物理像素区域，返回可发送的配置消息。"""
        from src.utils.i18n import get_i18n

        overlay = self.overlay
        ratio = overlay.devicePixelRatioF()
        position = overlay.mapTo(self.root, QPoint(0, 0))
        options = {
            **self._options,
            "scrim_light": round(overlay._scrim * 100),
            "scrim_dark": round(overlay._scrim * 100),
            "blur": overlay._blur,
            "fit": overlay._fit,
        }
        return dict(
            action="configure",
            options=options,
            theme=theme.current_theme(),
            language=get_i18n().current_language,
            parent=int(self.root.winId()),
            size=[overlay.width(), overlay.height()],
            rect=[
                round(position.x() * ratio),
                round(position.y() * ratio),
                max(1, round(overlay.width() * ratio)),
                max(1, round(overlay.height() * ratio)),
            ],
            paused=self._paused,
            suspended=self._suspended or not self.window.isVisible(),
        )

    def _frame_ready(self, image):
        """将可见窗口的非空前景图像与对应配置提交给限流发送器，捕获共享图像写入错误。"""
        if self._stopped or self._socket is None:
            return
        try:
            configuration = self._configuration()
            if configuration["suspended"]:
                return
            if not image.isNull():
                self._sender.submit(image, configuration=configuration)
        except (OSError, ValueError, RuntimeError) as exc:
            self._report_error(tr("背景呈现失败：{exc}", exc=exc))

    def _report_error(self, message):
        """合并首个待发布错误，并延迟到绘制调用结束后通知控制器以便安全移除图形效果。"""
        if self._stopped or self._pending_error is not None:
            return
        # 错误处理会移除当前图形效果，因此退出绘制栈后再通知控制器。
        self._pending_error = message
        call_later(0, self, self._publish_error)

    def _publish_error(self):
        """取出待发布错误，在呈现器仍活动时发出错误状态信号。"""
        message, self._pending_error = self._pending_error, None
        if not self._stopped and message is not None:
            self.state_changed.emit("error", message)

    def _read(self):
        """解析管道中的预览、确认及状态消息，推进图像发送并转发结果信号。

        协议读取失败交由延迟错误通知处理，避免在当前回调内破坏呈现资源。
        """
        try:
            for kind, payload in self._reader.feed(bytes(self._socket.readAll())):
                if self._stopped:
                    break
                if kind == b"P":
                    _, image = decode_image(payload)
                    self.preview_ready.emit(image)
                elif kind == b"J":
                    message = decode_json(payload)
                    if "ack" in message:
                        self._sender.acknowledge(message["ack"])
                    elif "state" in message:
                        self.state_changed.emit(message["state"], message.get("message", ""))
        except (OSError, ValueError, RuntimeError) as exc:
            self._report_error(tr("背景进程通信失败：{exc}", exc=exc))

    def stop(self):
        """发送停止命令，卸载前景捕获和事件过滤器，并给予子进程有限退出时间。

        该操作可重复调用；共享图像、套接字和日志在子进程确认结束后统一释放。
        """
        if self._stopped:
            return
        self._send({"action": "stop"})
        if self._socket is not None:
            self._socket.flush()
        self._stopped = True
        self._repaint_coalescer.stop()
        self._sender.discard_pending()
        self._server.close()
        self._deadline = time.monotonic() + 1.5
        try:
            if self.overlay._native_presenter is self:
                self.overlay._native_presenter = None
                self.overlay.update()
            for widget in {self.root, self.window, self.overlay}:
                widget.removeEventFilter(self)
            if self.root.graphicsEffect() is self._capture_effect:
                self.root.setGraphicsEffect(None)
        except RuntimeError:
            pass

    def _poll(self):
        """监控渲染进程退出及连接超时，发布异常日志并终止超过宽限期的进程。"""
        if self._process is None:
            return
        code = self._process.poll()
        if code is not None:
            if not self._stopped:
                self._log.seek(0)
                detail = self._log.read().decode("utf8", errors="replace")[-4000:]
                logger.warning("背景呈现进程退出 (%s): %s", code, detail)
                self.state_changed.emit("error", tr("背景播放进程已退出，请重试。"))
                self.stop()
            self._finish()
        elif self._deadline is not None and time.monotonic() >= self._deadline:
            if not self._stopped:
                self.state_changed.emit("error", tr("背景播放进程启动超时，请重试。"))
                self.stop()
            self._process.kill()

    def _finish(self):
        """停止进程轮询，关闭共享图像、套接字和日志，再移除全局引用并延迟销毁。"""
        self._poll_timer.stop()
        self._images.close()
        if self._socket is not None:
            self._socket.abort()
        if self._log is not None:
            self._log.close()
            self._log = None
        _live_presenters.discard(self)
        self.deleteLater()
