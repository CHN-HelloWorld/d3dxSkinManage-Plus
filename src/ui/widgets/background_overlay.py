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

    def _refresh_dynamic_frame(self) -> None:
        """作废旧的帧处理结果，以当前尺寸和参数重新提交最近的动态帧。"""
        if self._disposed or not self._dynamic or self._dynamic_image.isNull():
            return
        self._generation += 1
        self._pending_frame = None
        self.set_dynamic_frame(self._dynamic_image)

    def resizeEvent(self, event) -> None:
        """在控件尺寸变化后重新处理当前动态帧，使其匹配新的显示区域。"""
        super().resizeEvent(event)
        self._refresh_dynamic_frame()

    def set_dynamic_frame(self, image: QImage) -> None:
        """接收有效动态帧；需要缩放或模糊时合并到待处理帧，否则直接安装。"""
        if self._disposed or not self._dynamic or image.isNull():
            return
        self._dynamic_image = image
        ratio = self.devicePixelRatioF()
        aspect = (
            Qt.AspectRatioMode.KeepAspectRatio
            if self._fit == "contain"
            else Qt.AspectRatioMode.KeepAspectRatioByExpanding
        )
        target = self.size() * ratio
        if self._blur or image.size() != image.size().scaled(target, aspect):
            self._pending_frame = image
            self._start_frame_blur()
        else:
            self._install_frame(image)

    def reset_dynamic_frames(self) -> None:
        """递增代次使异步结果失效，清空动态帧及绘制缓存并请求重绘。"""
        self._generation += 1
        self._pending_frame = None
        self._dynamic_image = QImage()
        self._orig = self._render = None
        self._invalidate_scaled()
        self.update()

    def _start_frame_blur(self) -> None:
        """仅在没有帧任务运行时，按当前像素尺寸向全局线程池提交最新待处理帧。"""
        if self._blur_task is not None or self._pending_frame is None:
            return
        from src.ui.backgrounds.blur import BlurFrame

        image, self._pending_frame = self._pending_frame, None
        ratio = self.devicePixelRatioF()
        task = BlurFrame(
            self._generation,
            image,
            self._blur,
            (max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio))),
            self._fit,
        )
        task.signals.ready.connect(self._frame_blurred)
        self._blur_task = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, QImage)
    def _frame_blurred(self, generation: int, image: QImage) -> None:
        """仅安装当前代次的处理结果，释放任务槽位后继续处理最新排队帧。"""
        self._blur_task = None
        if self._disposed or not self._dynamic:
            return
        if generation == self._generation:
            self._install_frame(image)
        self._start_frame_blur()

    def _install_frame(self, image: QImage) -> None:
        """在界面线程把处理后的图像转为带像素比的位图，更新帧序号并作废缩放缓存。"""
        self._orig = QPixmap.fromImage(image)
        self._orig.setDevicePixelRatio(self.devicePixelRatioF())
        self._render = self._orig
        self._invalidate_scaled()
        self._frame_sequence += 1
        self.update()

    def is_loading(self) -> bool:
        """最新请求是否仍在准备；已经清空背景时不等待旧请求的读盘。"""
        return self._loading

    def when_ready(self, callback: Callable[[], None]) -> None:
        """在 GUI 线程调用一次 callback；已就绪时立即调用，销毁后不再调用。"""
        if self._disposed:
            return
        self._ready_callbacks.append(callback)
        self._notify_ready()

    def dispose(self) -> None:
        """丢弃待办和回调；线程池持有正在读取的任务，无须在 GUI 等待 IO。"""
        self._disposed = True
        self._loading = False
        self._pending_read = None
        self._active_read = None
        self._ready_callbacks.clear()
        self._generation += 1
        self._pending_frame = None
        self._dynamic_image = QImage()
        self._orig = None
        self._render = None
        self._invalidate_scaled()

    def set_scrim_live(self, scrim_pct: int) -> None:
        """将遮罩比例限制在有效范围内，刷新原生呈现器及控件以实时显示变化。"""
        if self._disposed:
            return
        self._scrim = max(0.0, min(1.0, (scrim_pct or 0) / 100.0))
        if self._native_presenter is not None:
            self._native_presenter.refresh()
        self.update()

    def set_blur_live(self, blur_pct: int) -> None:
        """应用模糊比例变化，按原生呈现、动态帧或静态图像路径刷新结果；调用方负责节流。"""
        if self._disposed:
            return
        new_blur = max(0, min(100, int(blur_pct or 0)))
        if new_blur == self._blur:
            return
        self._blur = new_blur
        if self._native_presenter is not None:
            self._native_presenter.refresh()
            return
        if self._dynamic:
            self._generation += 1
            self._pending_frame = None
            self.set_dynamic_frame(self._dynamic_image)
            return
        self._render = self._make_blurred(self._orig, self._blur)
        self._invalidate_scaled()
        self.update()

    # 内部绘制。
    def _start_pending_read(self) -> None:
        """串行提交最新静态读图请求，使用全局线程池避免窗口析构等待文件读取。"""
        if self._disposed or self._active_read is not None or self._pending_read is None:
            return
        generation, path = self._pending_read
        self._pending_read = None
        task = _ImageReadTask(generation, path)
        task.signals.finished.connect(self._image_read, Qt.ConnectionType.QueuedConnection)
        self._active_read = task
        # 不创建归控件所有的 QThreadPool：它的析构会等待仍在读盘的任务。
        QThreadPool.globalInstance().start(task)

    @Slot(int, QImage)
    def _image_read(self, generation: int, image: QImage) -> None:
        """接收读图结果，仅更新当前代次的背景，然后继续排队读取并检查就绪回调。"""
        if self._disposed:
            return
        self._active_read = None
        if generation == self._generation:
            pm = QPixmap.fromImage(image) if not image.isNull() else None
            self._orig = pm if pm is not None and not pm.isNull() else None
            self._render = self._make_blurred(self._orig, self._blur)
            self._invalidate_scaled()
            self._loading = False
            self.update()
        self._start_pending_read()
        self._notify_ready()

    def _notify_ready(self) -> None:
        """在最新静态背景已就绪时依次执行回调，防止重入并隔离单个回调的异常。"""
        if self._notifying_ready:
            return
        self._notifying_ready = True
        try:
            while self._ready_callbacks and not self._loading and not self._disposed:
                callback = self._ready_callbacks.popleft()
                try:
                    callback()
                except Exception:
                    logger.exception("Background readiness callback failed")
        finally:
            self._notifying_ready = False

    def _invalidate_scaled(self) -> None:
        """释放缩放后的位图及其缓存键，使下次绘制按当前参数重建缓存。"""
        self._scaled = None
        self._scaled_key = None

    @classmethod
    def _make_blurred(cls, pm: QPixmap | None, blur_pct: int) -> QPixmap | None:
        """先降采样并延伸边缘像素，再模糊及裁剪；保留不透明图像的覆盖范围，返回处理后的位图。"""
        if pm is None or pm.isNull() or blur_pct <= 0:
            return pm
        # 先降采样再模糊，减少 QGraphicsScene 处理的像素数和主线程耗时。
        # 绘制时将模糊结果缩放至背景区域。
        max_side = 1280
        src = pm
        longest = max(pm.width(), pm.height())
        scale = 1.0
        if longest > max_side:
            scale = max_side / float(longest)
            src = pm.scaled(
                max(1, int(pm.width() * scale)),
                max(1, int(pm.height() * scale)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        radius = blur_pct / 100.0 * cls._MAX_BLUR_RADIUS * (scale if scale < 1.0 else 1.0)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(radius)
        # 延伸边缘像素后再模糊、裁回原尺寸，避免把图外透明黑色混进边缘。
        # 仍在降采样尺寸处理；只多出与模糊半径有关的一圈，不影响缩放缓存。
        rect = QRectF(0, 0, src.width(), src.height())
        bounds = effect.boundingRectFor(rect)
        padding = ceil(
            max(-bounds.left(), -bounds.top(), bounds.right() - rect.right(), bounds.bottom() - rect.bottom())
        )
        padded = QPixmap(src.width() + padding * 2, src.height() + padding * 2)
        padded.fill(Qt.GlobalColor.transparent)
        edge_painter = QPainter(padded)
        xs = (
            (0, padding, 0, 1),
            (padding, src.width(), 0, src.width()),
            (padding + src.width(), padding, src.width() - 1, 1),
        )
        ys = (
            (0, padding, 0, 1),
            (padding, src.height(), 0, src.height()),
            (padding + src.height(), padding, src.height() - 1, 1),
        )
        for dx, dw, sx, sw in xs:
            for dy, dh, sy, sh in ys:
                edge_painter.drawPixmap(QRectF(dx, dy, dw, dh), src, QRectF(sx, sy, sw, sh))
        edge_painter.end()
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(padded)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        out = QImage(src.size(), QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(0)
        painter = QPainter(out)
        scene.render(painter, rect, rect.translated(padding, padding))
        painter.end()
        if not src.hasAlphaChannel():
            # Qt 的快速模糊会在不透明区域也留下少量 alpha 舍入误差。
            # 先解除预乘再丢弃 alpha，保持原本不透明的图片颜色与覆盖范围。
            out = out.convertToFormat(QImage.Format.Format_ARGB32).convertToFormat(QImage.Format.Format_RGB32)
        return QPixmap.fromImage(out)

    def _scaled_for(self, pm: QPixmap, w: int, h: int) -> QPixmap | None:
        """按适应或填充模式缩放位图，并按视口尺寸、模式及源图缓存结果。"""
        key = (w, h, self._fit, pm.cacheKey())
        if self._scaled_key == key and self._scaled is not None:
            return self._scaled
        if self._fit == "contain":
            scaled = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        else:  # 按比例填充。
            scaled = pm.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
            )
        self._scaled = scaled
        self._scaled_key = key
        return scaled

    def paintEvent(self, _event) -> None:
        """绘制背景及主题遮罩并统计动态帧；前景捕获或原生呈现期间由对应流程负责显示。"""
        if self._capturing_foreground or self._native_presenter is not None:
            return
        if not self._enabled or self._orig is None or self._orig.isNull():
            return
        pm = self._render if (self._render and not self._render.isNull()) else self._orig
        painter = QPainter(self)
        if self._dynamic and self._painted_sequence != self._frame_sequence:
            self.frames_drawn += 1
            self._painted_sequence = self._frame_sequence
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return
        if self._dynamic:
            # 动态帧已匹配显示尺寸，绘制时不重复分配和缩放全窗图像。
            pw = pm.width() / pm.devicePixelRatioF()
            ph = pm.height() / pm.devicePixelRatioF()
            painter.drawPixmap(QRectF((w - pw) / 2, (h - ph) / 2, pw, ph), pm, QRectF(0, 0, pm.width(), pm.height()))
        elif self._fit == "tile":
            painter.drawTiledPixmap(self.rect(), pm)
        else:
            scaled = self._scaled_for(pm, w, h)
            if scaled is not None and not scaled.isNull():
                if self._fit == "contain":
                    painter.drawPixmap((w - scaled.width()) // 2, (h - scaled.height()) // 2, scaled)
                else:  # cover：等比放大铺满后居中裁剪
                    sx = max(0, (scaled.width() - w) // 2)
                    sy = max(0, (scaled.height() - h) // 2)
                    painter.drawPixmap(0, 0, scaled, sx, sy, w, h)
        if self._scrim > 0:
            # 浅色界面以白色柔化图片，保持深色文字可读；深色界面保留原压暗效果。
            tint = 255 if theme.current_theme() == "light" else 0
            painter.fillRect(self.rect(), QColor(tint, tint, tint, int(self._scrim * 255)))
        painter.end()
