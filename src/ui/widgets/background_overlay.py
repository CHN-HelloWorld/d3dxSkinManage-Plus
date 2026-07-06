"""在中央内容区底层绘制自定义背景及主题遮罩。

支持填充、适应、平铺和模糊。控件不参与布局并忽略鼠标事件，
由主窗口同步几何，上层透明区域显示此背景。
"""

from __future__ import annotations

import logging
import weakref
from collections import deque
from collections.abc import Callable
from math import ceil

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QWidget,
)

from src.ui import theme
from src.utils import fs

logger = logging.getLogger(__name__)


class _ReadyCallbackCleanup(QObject):
    """让 Qt 跟踪销毁槽的接收者，避免循环 GC 回调已被清理的 Python 闭包。"""

    def __init__(self, callbacks: deque) -> None:
        """弱引用就绪回调队列，使清理接收者不延长窗口及回调的生命周期。"""
        super().__init__()
        self._callbacks = weakref.ref(callbacks)

    @Slot()
    def release(self) -> None:
        """将回调队列的释放延后到子控件析构之后，并安排销毁清理接收者。"""
        callbacks = self._callbacks()
        if callbacks is not None:
            # 回调可能是父窗口的最后一个引用；等子控件析构结束再释放。
            QTimer.singleShot(0, callbacks.clear)
        self.deleteLater()


class _ImageReadSignals(QObject):
    """在线程池读图完成后携带请求代次和图像通知界面线程。"""

    finished = Signal(int, QImage)


class _ImageReadTask(QRunnable):
    """只在线程池中读 QImage；信号对象不归控件所有，销毁窗口不会中断读盘。"""

    def __init__(self, generation: int, path: str) -> None:
        """保存读图路径及请求代次，创建独立于背景控件的完成信号对象。"""
        super().__init__()
        self.generation = generation
        self.path = path
        self.signals = _ImageReadSignals()

    def run(self) -> None:
        """读取并自动校正图像方向，释放文件句柄后发送结果；读取异常时发送空图像。"""
        image = QImage()
        try:
            reader = QImageReader(fs.native_path(self.path))
            reader.setAutoTransform(True)
            try:
                image = reader.read()
            finally:
                # QImageReader 持有文件句柄；先释放，再让 GUI 安装图片/通知完成。
                del reader
        except Exception:
            logger.exception("Unable to read background image: %s", self.path)
        self.signals.finished.emit(self.generation, image)


class BackgroundOverlay(QWidget):
    """在内容区底层绘制静态或动态背景，并叠加与主题相适应的遮罩。"""

    _MAX_BLUR_RADIUS = 40  # blur=100 对应的高斯半径

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化静态读图、动态帧和缩放缓存状态，建立不会保活窗口的就绪回调清理机制。"""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._orig: QPixmap | None = None  # 原图
        self._render: QPixmap | None = None  # 模糊后缓存（无模糊时 == 原图）
        self._path = ""
        self._scrim = 0.45
        self._blur = 0
        self._fit = "cover"
        self._enabled = False
        # 缓存平滑缩放结果，仅在视口尺寸、适配方式或源图变化时重算，降低连续重绘开销。
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple | None = None
        self._generation = 0
        self._active_read: _ImageReadTask | None = None
        self._pending_read: tuple[int, str] | None = None
        self._loading = False
        self._disposed = False
        self._dynamic = False
        self._capturing_foreground = False
        self._native_presenter = None
        self._dynamic_image = QImage()
        self._blur_task = None
        self._pending_frame = None
        self.frames_drawn = 0
        self._frame_sequence = 0
        self._painted_sequence = 0
        self._ready_callbacks: deque[Callable[[], None]] = deque()
        self._notifying_ready = False
        # 独立接收者不以本控件为 QObject 父对象，仅弱引用队列：既不保活父窗口，
        # 又让 Qt 在接收者被循环 GC 回收时自动断开槽，避免 lambda/partial 代理悬挂。
        self._ready_cleanup = _ReadyCallbackCleanup(self._ready_callbacks)
        self.destroyed.connect(self._ready_cleanup.release)
        self.hide()

    # 公开接口。
    def set_enabled(self, on: bool) -> None:
        """在控件仍有效且启用状态变化时同步可见性并请求重绘。"""
        if self._disposed or self._enabled == bool(on):
            return
        self._enabled = bool(on)
        self.setVisible(self._enabled)
        self.update()

    def configure(self, path: str, scrim_pct: int, blur_pct: int, fit: str) -> None:
        """更新参数；路径变化异步读图，读完前保留已显示的背景。"""
        if self._disposed:
            return
        if self._dynamic:
            self._dynamic = False
            self._generation += 1
            self._dynamic_image = QImage()
            self._pending_frame = None
            self._path = ""
            self._orig = self._render = None
        self._scrim = max(0.0, min(1.0, (scrim_pct or 0) / 100.0))
        new_fit = fit or "cover"
        refit = new_fit != self._fit
        self._fit = new_fit
        new_blur = max(0, min(100, int(blur_pct or 0)))
        path = path or ""
        reload_img = path != self._path
        reblur = new_blur != self._blur
        self._blur = new_blur
        self._path = path
        if reload_img:
            self._generation += 1
            self._loading = bool(path)
            self._pending_read = (self._generation, path) if path else None
            if not path:
                self._orig = None
                self._render = None
        if reblur:
            self._render = self._make_blurred(self._orig, self._blur)
        if reload_img or reblur or refit:
            self._invalidate_scaled()
        self._start_pending_read()
        self.update()
        self._notify_ready()

    def configure_dynamic(self, scrim_pct: int, blur_pct: int, fit: str) -> None:
        """切换为动态背景，作废静态读图请求，并应用遮罩、模糊和适配参数。"""
        if self._disposed:
            return
        if not self._dynamic:
            self._generation += 1
            self._dynamic = True
            self._path = ""
            self._pending_read = None
            self._loading = False
            self._orig = self._render = None
            self._notify_ready()
        old_fit = self._fit
        self._fit = fit if fit in ("cover", "contain") else "cover"
        self.set_scrim_live(scrim_pct)
        self.set_blur_live(blur_pct)
        if old_fit != self._fit:
            self._refresh_dynamic_frame()
