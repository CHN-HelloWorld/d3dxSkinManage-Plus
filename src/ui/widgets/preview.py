"""管理本地预览图片、拖放和剪贴板输入，以及可缩放的图片查看窗口。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui.widgets.image_utils import (
    fit_contain_rounded,
    load_pixmap_file,
    trim_uniform_border,
)
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


def clipboard_has_image() -> bool:
    """剪贴板里是否有可用作预览图的东西（本地图片文件 或 位图数据）。

    只做判断、不写入磁盘，供菜单项决定灰显。
    """
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return False
    mime = clipboard.mimeData()
    if mime is not None and mime.hasUrls():
        from src.utils.paths import is_image

        for url in mime.urls():
            if url.isLocalFile():
                p = url.toLocalFile()
                if is_image(p) and fs.exists(p):
                    return True
    if mime is not None and mime.hasImage():
        return True
    image = clipboard.image()
    return image is not None and not image.isNull()


def clipboard_image_path() -> str:
    """把剪贴板里的图片解析成一个本地路径；无图返回 ""。

    优先本地文件 URL（复制的图片文件，直接用原路径）；否则把位图数据存成缓存目录里的
    临时 PNG（不写 C 盘临时区）。预览栏右键粘贴、卡片右键快捷上图共用此入口。
    """
    clipboard = QApplication.clipboard()
    if clipboard is None:
        return ""
    # 1. 优先检查剪切板中的文件URL（复制的图片文件）
    mime = clipboard.mimeData()
    if mime is not None and mime.hasUrls():
        from src.utils.paths import is_image

        for url in mime.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if is_image(path) and fs.exists(path):
                    return path
    # 2. 检查剪切板中的图片数据（截图、复制的图片内容）
    image = clipboard.image()
    if image is not None and not image.isNull():
        from src.utils.paths import get_cache_dir

        clip_dir = get_cache_dir() / "clipboard"
        clip_dir.mkdir(parents=True, exist_ok=True)
        temp_path = tempfile.mktemp(suffix=".png", prefix="mod_preview_clip_", dir=str(clip_dir))
        if image.save(temp_path, "PNG"):
            return temp_path
    return ""


class _ZoomableImageWidget(QWidget):
    """支持鼠标滚轮缩放和左键拖拽平移的图片控件"""

    def __init__(self, pixmap: QPixmap, parent=None):
        """保存原始位图并初始化缩放及拖动偏移，启用鼠标跟踪和键盘焦点。"""
        super().__init__(parent)
        self._pixmap = pixmap
        self._scale = 1.0
        self._min_scale = 0.1
        self._max_scale = 10.0
        self._offset = QPointF(0, 0)  # 平移偏移量（逻辑坐标）
        self._dragging = False
        self._drag_start = QPointF()
        self._drag_offset_start = QPointF()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 150)

    def fit_to_window(self):
        """根据当前窗口尺寸自动适配图片缩放比例"""
        if self._pixmap.isNull():
            return
        pw, ph = self._pixmap.width(), self._pixmap.height()
        ww, wh = self.width(), self.height()
        sx = ww / pw if pw > 0 else 1.0
        sy = wh / ph if ph > 0 else 1.0
        self._scale = min(sx, sy, 1.0)  # 不超过原始尺寸
        self._offset = QPointF(0, 0)
        self.update()

    def paintEvent(self, event):
        """在黑色底色上按缩放比例及平移偏移绘制居中的原始图片。"""
        if self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)

        # 计算居中偏移量
        pw = self._pixmap.width() * self._scale
        ph = self._pixmap.height() * self._scale
        cx = (self.width() - pw) / 2 + self._offset.x()
        cy = (self.height() - ph) / 2 + self._offset.y()

        painter.translate(cx, cy)
        painter.scale(self._scale, self._scale)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()

    def wheelEvent(self, event: QWheelEvent):
        """鼠标滚轮缩放 — 以光标位置为缩放中心"""
        old_scale = self._scale
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else (1.0 / 1.15)
        new_scale = max(self._min_scale, min(self._max_scale, self._scale * factor))
        if new_scale == old_scale:
            return

        # 以鼠标光标位置为缩放中心
        mouse_pos = event.position()
        # 当前图片中心在widget中的位置
        img_center_x = self.width() / 2 + self._offset.x()
        img_center_y = self.height() / 2 + self._offset.y()
        # 鼠标相对图片中心的偏移
        dx = mouse_pos.x() - img_center_x
        dy = mouse_pos.y() - img_center_y
        # 缩放后调整偏移
        ratio = new_scale / old_scale
        self._offset = QPointF(
            self._offset.x() - dx * (ratio - 1),
            self._offset.y() - dy * (ratio - 1),
        )
        self._scale = new_scale
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent):
        """左键按下时记录拖动起点和原偏移，切换为抓取光标。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.position()
            self._drag_offset_start = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        """拖动期间依据鼠标相对起点的位移更新图片平移量。"""
        if self._dragging:
            delta = event.position() - self._drag_start
            self._offset = QPointF(
                self._drag_offset_start.x() + delta.x(),
                self._drag_offset_start.y() + delta.y(),
            )
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """左键释放时结束图片平移，并恢复可拖动光标。"""
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)
