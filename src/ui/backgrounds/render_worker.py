"""仅负责视频与前景合成的 Qt 进程，不处理业务操作。"""

from __future__ import annotations

import sys

import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from src.ui import theme
from src.ui.backgrounds.controller import BackgroundController
from src.ui.backgrounds.process_native import attach_child, parent_alive, position_child, raise_child
from src.ui.backgrounds.process_protocol import PacketReader, decode_image, decode_json, encode_image, encode_json
from src.ui.backgrounds.shared_image import SharedImageReader
from src.ui.widgets.background_overlay import BackgroundOverlay
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


class ForegroundLayer(QWidget):
    """在视频上方以原有控件坐标绘制透明前景图像。"""

    def __init__(self, parent):
        """创建不接收鼠标事件的透明图层，并初始化空前景图像。"""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.image = QImage()

    def paintEvent(self, event):
        """绘制有效前景图像，尺寸切换期间保持其原始坐标和比例。"""
        if not self.image.isNull():
            painter = QPainter(self)
            # 新尺寸可能先于对应前景到达，期间保持旧控件坐标，避免拉伸导致文字和卡片移动。
            painter.drawImage(QPoint(), self.image)


class VideoCanvas(QWidget):
    """承载渲染进程的视频背景与前景，按父窗口状态管理原生子窗口。"""

    def __init__(self, process_id):
        """创建不抢焦点的画布及背景控制器，等待背景和前景均就绪后显示。"""
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._process_id = process_id
        self._parent_hwnd = None
        self._native_rect = None
        self._suspended = False
        self._video_ready = False
        self._foreground_ready = False
        self.overlay = BackgroundOverlay(self)
        self.foreground = ForegroundLayer(self)
        self.controller = BackgroundController(self.overlay, self)
        self.controller.active_changed.connect(self._active_changed)

    def configure(self, message):
        """验证原生窗口尺寸并应用父窗口、主题及播放配置；非法配置或嵌入失败时抛出异常。"""
        from src.utils.i18n import get_i18n, set_language

        language = message.get("language")
        if language and language != get_i18n().current_language:
            set_language(language)
        parent = int(message["parent"])
        rect = tuple(int(value) for value in message["rect"])
        if len(rect) != 4 or not (0 < rect[2] <= 16384 and 0 < rect[3] <= 16384):
            raise ValueError("Invalid background window size")
        if self._parent_hwnd != parent:
            attach_child(int(self.winId()), parent, self._process_id)
            self._parent_hwnd = parent
        theme.set_theme(message["theme"])
        self.resize(*message["size"])
        if self._native_rect != rect:
            self._native_rect = rect
            position_child(int(self.winId()), rect)
        self.overlay.setGeometry(self.rect())
        self.foreground.setGeometry(self.rect())
        self.set_suspended(bool(message.get("suspended")))
        self.controller.apply(message["options"])
        paused = bool(message.get("paused"))
        if paused != self.controller._paused:
            self.controller.playback("pause" if paused else "resume")
        self._update_visibility()
        self.update()

    def set_suspended(self, suspended):
        """同步暂停渲染状态，并根据两层画面的就绪情况更新可见性。"""
        self._suspended = bool(suspended)
        self.controller.set_suspended(self._suspended)
        self._update_visibility()

    def present_foreground(self, image, configuration=None):
        """在同一次禁止重绘的更新中应用可选配置和前景，最后恢复原有重绘状态。"""
        if configuration is None:
            self.set_foreground(image)
            return
        enabled = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            self.configure(configuration)
            self.set_foreground(image)
        finally:
            self.setUpdatesEnabled(enabled)

    def set_foreground(self, image):
        """替换前景图像并标记就绪，显示时调整原生窗口层次以覆盖前景区域。"""
        self.foreground.image = image
        self.foreground.update()
        self._foreground_ready = True
        self._update_visibility()
        if self.isVisible():
            raise_child(int(self.winId()))

    def _active_changed(self, active):
        """记录背景画面是否就绪，并重新计算画布是否可见。"""
        self._video_ready = active
        self._update_visibility()

    def _update_visibility(self):
        """仅在背景和前景均就绪且未挂起时显示窗口，并恢复原生位置。"""
        visible = self._video_ready and self._foreground_ready and not self._suspended
        if visible != self.isVisible():
            self.setVisible(visible)
            if visible and self._native_rect:
                position_child(int(self.winId()), self._native_rect)

    def resizeEvent(self, event):
        """随画布尺寸调整两层控件，并通知背景控制器更新输出尺寸。"""
        super().resizeEvent(event)
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.rect())
            self.foreground.setGeometry(self.rect())
            self.controller.resize()

    def paintEvent(self, event):
        """使用当前主题的窗口颜色填充画布底色。"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.color("window")))

    def dispose(self):
        """隐藏画布并释放背景控制器和叠加层资源。"""
        self.hide()
        self.controller.dispose()
        self.overlay.dispose()


class RenderSession:
    """通过本地套接字接收配置和前景，监督主窗口存活并回传渲染状态。"""

    def __init__(self, app, pipe, process_id):
        """连接本地通信、图像读取与退出清理，并启动父窗口和连接状态检查。"""
        self.app = app
        self.canvas = VideoCanvas(process_id)
        self.reader = PacketReader()
        self._shared_images = SharedImageReader()
        self._pending_configuration = None
        self.socket = QLocalSocket()
        self.socket.readyRead.connect(self.read)
        self.socket.disconnected.connect(app.quit)
        self.canvas.controller.state_changed.connect(self.status)
        self.canvas.controller.preview_ready.connect(lambda _path, image: self.socket.write(encode_image(image, b"P")))
        self.socket.connectToServer(pipe)
        self.parent_timer = QTimer()
        self.parent_timer.timeout.connect(self.check_parent)
        self.parent_timer.start(500)
        app.aboutToQuit.connect(self.canvas.dispose)
        app.aboutToQuit.connect(self._shared_images.close)
        call_later(15000, self.app, self.check_connection)

    def status(self, state, message):
        """将背景状态及提示文字编码为 JSON 回传主进程。"""
        self.socket.write(encode_json(dict(state=state, message=message)))

    def check_connection(self):
        """在连接期限到达时检查通信是否建立，未连接则退出渲染应用。"""
        if self.socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            self.app.quit()

    def check_parent(self):
        """父窗口已绑定时验证其进程归属，失效则退出渲染应用。"""
        canvas = self.canvas
        if canvas._parent_hwnd is not None and not parent_alive(canvas._parent_hwnd, canvas._process_id):
            self.app.quit()

    def read(self):
        """消费完整命令和图像，应用配套配置后确认帧序号；处理异常时回传错误状态。"""
        try:
            for kind, payload in self.reader.feed(bytes(self.socket.readAll())):
                if kind == b"F":
                    sequence, image = decode_image(payload)
                    configuration, self._pending_configuration = self._pending_configuration, None
                    self.canvas.present_foreground(image, configuration)
                    self.socket.write(encode_json(dict(ack=sequence)))
                elif kind == b"J":
                    message = decode_json(payload)
                    action = message.get("action")
                    if action == "foreground":
                        image = self._shared_images.read(message["image"])
                        configuration, self._pending_configuration = self._pending_configuration, None
                        self.canvas.present_foreground(image, message.get("configuration", configuration))
                        self.socket.write(encode_json(dict(ack=message["sequence"])))
                    elif action == "configure":
                        # 配置与帧沿同一有序通道传输，完整收到新帧前保留原有显示几何。
                        self._pending_configuration = message
                        if message.get("suspended"):
                            self.canvas.set_suspended(True)
                    elif action in ("pause", "resume"):
                        self.canvas.controller.playback(action)
                    elif action == "stop":
                        self.app.quit()
                        return
        except Exception as exc:
            self.status("error", tr("背景呈现失败：{exc}", exc=exc))


def main(pipe: str, process_id: int) -> int:
    """运行独立渲染应用，退出事件循环后删除画布并返回 Qt 的退出码。"""
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setProperty("backgroundRenderProcess", True)
    session = RenderSession(app, pipe, process_id)
    result = app.exec()
    # Qt 退出前由退出钩子停止并等待媒体工作线程。
    shiboken6.delete(session.canvas)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], int(sys.argv[2])))
