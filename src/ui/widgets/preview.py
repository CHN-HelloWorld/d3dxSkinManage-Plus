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

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """双击恢复适配窗口"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_to_window()
            event.accept()


class ImageViewerDialog(QDialog):
    """预览图放大查看窗口 — 支持鼠标滚轮缩放和左键拖拽平移"""

    def __init__(self, image_path: str, parent=None):
        """读取图片并按屏幕范围恢复查看窗口尺寸，布局就绪后适配图像，读取失败时显示提示。"""
        super().__init__(parent)
        self.setWindowTitle(Path(image_path).name)
        self.setMinimumSize(400, 300)
        pixmap = load_pixmap_file(image_path)

        # 优先沿用已保存的窗口尺寸并限制在当前屏幕内，否则按屏幕可用区域设置默认尺寸。
        # 关闭或隐藏时保存尺寸到 ui.image_viewer，图片本身的大小不决定窗口大小。
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            avail_w, avail_h = geom.width(), geom.height()
        else:
            avail_w, avail_h = 1200, 900

        from src.core.config import Config

        cfg = Config.instance()
        saved_w = cfg.get("ui.image_viewer.width", 0)
        saved_h = cfg.get("ui.image_viewer.height", 0)
        if isinstance(saved_w, int) and isinstance(saved_h, int) and saved_w >= 400 and saved_h >= 300:
            self.resize(min(saved_w, avail_w), min(saved_h, avail_h))
        else:
            def_w = max(760, min(int(avail_w * 0.72), avail_w))
            def_h = max(560, min(int(avail_h * 0.72), avail_h))
            self.resize(def_w, def_h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not pixmap.isNull():
            self._image_widget = _ZoomableImageWidget(pixmap)
            self._image_widget.setCursor(Qt.CursorShape.OpenHandCursor)
            layout.addWidget(self._image_widget)
            # 延迟一帧做 fit_to_window 以获取正确的 widget 尺寸
            call_later(0, self._image_widget, self._image_widget.fit_to_window)
        else:
            label = QLabel(tr("无法加载图片"))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(
                f"color: {theme.color('text.tertiary')}; font-size: 16px; background-color: {theme.color('bg.base')};"
            )
            layout.addWidget(label)

    def _persist_size(self) -> None:
        """把当前窗口尺寸落库（下次打开沿用，见 __init__ 的尺寸策略）。"""
        try:
            w, h = int(self.width()), int(self.height())
            if w < 400 or h < 300:
                return  # 最小化/异常尺寸不记忆，避免污染记忆值
            from src.core.config import Config

            Config.instance().set("ui.image_viewer", {"width": w, "height": h})
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭查看窗口前保存当前尺寸，供下次打开时恢复。"""
        self._persist_size()
        super().closeEvent(event)

    def hideEvent(self, event):
        """隐藏时保存窗口尺寸，覆盖 Esc 或拒绝操作未触发关闭事件的情况。"""
        # Esc 或 reject 可能只触发隐藏而不经过 closeEvent，因此隐藏时也保存窗口尺寸。
        self._persist_size()
        super().hideEvent(event)


class PreviewWidget(QWidget):
    """预览图显示控件

    支持：
    - 显示 Mod 预览图（适应窗口大小）
    - 拖拽图片文件替换预览图
    - 多预览图切换
    - 点击放大查看
    """

    image_dropped = Signal(str)  # 拖入的图片路径
    image_clicked = Signal(str)  # 点击图片
    preview_changed = Signal(int)  # 当前预览图索引变更
    delete_current_requested = Signal(int)  # 删除当前预览图（删除文件）
    delete_all_requested = Signal()  # 删除所有预览图（删除文件）

    # 面板预览先限制工作分辨率，减少边框检测、缩放及像素缓存开销。
    # 独立图片查看器仍从源文件读取完整分辨率。
    _WORK_MAX_EDGE = 1600

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化图片列表、当前图缓存和缩放防抖定时器，创建支持拖放的预览界面。"""
        super().__init__(parent)
        # 预览控件保持透明，避免全局 QWidget 背景覆盖面板颜色。
        self.setObjectName("panelPreviewArea")
        self._images: list[str] = []
        self._current_index = 0
        self._single_image_mode = False  # 类别模式：仅一张预览图
        self._empty_text = tr("拖拽图片到此处\n或点击添加预览图\n或右键粘贴剪切板图片")  # 默认提示文本
        self._cached_pixmap: QPixmap | None = None  # 缓存已加载的原始图片
        self._cached_path: str = ""  # 缓存对应的路径
        # 防抖定时器：调整大小结束后用高质量重绘
        self._resize_smooth_timer = QTimer(self)
        self._resize_smooth_timer.setSingleShot(True)
        self._resize_smooth_timer.setInterval(150)
        self._resize_smooth_timer.timeout.connect(self._apply_smooth_scaled)
        self._setup_ui()
        self.setAcceptDrops(True)

    def _setup_ui(self) -> None:
        """初始化 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 图片显示区
        self._image_label = QLabel(tr("拖拽图片到此处\n或点击添加预览图\n或右键粘贴剪切板图片"))
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(200, 200)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._style_placeholder()
        # 仅在实际图像区域显示可点击光标，完整适配产生的留白保持默认光标。
        self._image_label.setMouseTracking(True)
        self._image_label.installEventFilter(self)
        layout.addWidget(self._image_label)

        # 导航按钮区
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(8)

        nav_layout.addStretch()

        self._prev_btn = QPushButton()
        icons.apply_icon(self._prev_btn, "actions/chevron-left")
        self._prev_btn.setIconSize(QSize(16, 16))
        self._prev_btn.setFixedSize(32, 32)
        self._prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._prev_btn.setToolTip(tr("上一张预览图"))
        self._prev_btn.setAccessibleName(tr("上一张预览图"))
        self._prev_btn.clicked.connect(self._show_prev)
        nav_layout.addWidget(self._prev_btn)

        self._page_label = QLabel("0 / 0")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav_layout.addWidget(self._page_label)

        self._next_btn = QPushButton()
        icons.apply_icon(self._next_btn, "actions/chevron-right")
        self._next_btn.setIconSize(QSize(16, 16))
        self._next_btn.setFixedSize(32, 32)
        self._next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next_btn.setToolTip(tr("下一张预览图"))
        self._next_btn.setAccessibleName(tr("下一张预览图"))
        self._next_btn.clicked.connect(self._show_next)
        nav_layout.addWidget(self._next_btn)

        nav_layout.addStretch()

        layout.addLayout(nav_layout)

        # 保留正常按钮尺寸，窄预览面板按需换行。
        mgmt_layout = TagFlowLayout(spacing=6)
        mgmt_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        mgmt_layout.setContentsMargins(0, 8, 0, 8)  # 上下增加间距，与分割线保持距离

        self._add_btn = QPushButton(tr("添加预览图"))
        icons.apply_icon(self._add_btn, "actions/add")
        self._add_btn.setIconSize(QSize(16, 16))
        self._add_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._add_btn.clicked.connect(self._browse_images)
        mgmt_layout.addWidget(self._add_btn)

        self._delete_btn = QPushButton(tr("删除"))
        icons.apply_icon(self._delete_btn, "actions/remove")
        self._delete_btn.setIconSize(QSize(16, 16))
        self._delete_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._delete_btn.setToolTip(tr("删除当前预览图（删除文件）"))
        self._delete_btn.clicked.connect(self._on_delete_current)
        mgmt_layout.addWidget(self._delete_btn)

        self._delete_all_btn = QPushButton(tr("删除所有"))
        icons.apply_icon(self._delete_all_btn, "actions/remove")
        self._delete_all_btn.setIconSize(QSize(16, 16))
        self._delete_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._delete_all_btn.setToolTip(tr("删除所有预览图（删除文件）"))
        self._delete_all_btn.clicked.connect(self._on_delete_all)
        mgmt_layout.addWidget(self._delete_all_btn)

        layout.addLayout(mgmt_layout)

        self._update_nav()

    # 预览图区域底色
    def _style_placeholder(self) -> None:
        """无图态：透明底 + 虚线框，沿用所在面板的背景。"""
        style = (
            "QLabel { background-color: transparent; "
            f"border: 2px dashed {theme.color('stroke.control')}; "
            f"border-radius: {theme.radius('card')}px; "
            f"color: {theme.color('text.tertiary')}; font-size: 14px; }}"
        )
        if self._image_label.styleSheet() != style:
            self._image_label.setStyleSheet(style)

    def _style_image(self) -> None:
        """显示图片时移除占位背景和虚线边框，使圆角区域透出面板本身。"""
        style = "QLabel { background: transparent; border: none; }"
        if self._image_label.styleSheet() != style:
            self._image_label.setStyleSheet(style)

    def _restore_label_style(self) -> None:
        """据当前是否有图恢复底色（拖拽结束 / 取消时调用）。"""
        if self._images:
            self._style_image()
        else:
            self._style_placeholder()

    def set_images(self, images: list[str], initial_index: int = 0) -> None:
        """设置预览图列表"""
        self._images = [img for img in images if fs.exists(img)]
        self._current_index = min(initial_index, max(0, len(self._images) - 1))
        self._show_current()
        self._update_nav()

    @staticmethod
    def _same_image_path(left: str, right: str) -> bool:
        """按规范化后的绝对路径比较非空图片路径，避免路径写法差异导致重复加载。"""
        if not left or not right:
            return False
        return Path(fs.normal_path(left)).absolute() == Path(fs.normal_path(right)).absolute()

    def set_images_deferred(
        self,
        images: list[str],
        initial_index: int = 0,
        *,
        cached_path: str = "",
        cached_pixmap: QPixmap | None = None,
    ) -> bool:
        """先更新预览状态，图片由调用方后台解码后再回填。

        可见卡片已有的低分辨率缓存足够覆盖侧栏尺寸时会立即复用，从而让 Mod
        选中反馈不再等待磁盘读取。返回本次是否成功复用了缓存。
        """
        self._images = [img for img in images if img]
        self._current_index = min(initial_index, max(0, len(self._images) - 1))
        if not self._images:
            self.clear()
            return False

        current_path = self._images[self._current_index]
        existing_matches = (
            self._cached_pixmap is not None
            and not self._cached_pixmap.isNull()
            and self._same_image_path(self._cached_path, current_path)
        )
        card_cache_matches = (
            cached_pixmap is not None
            and not cached_pixmap.isNull()
            and self._same_image_path(cached_path, current_path)
        )
        can_reuse = False
        if existing_matches:
            # 右栏自己的缓存通常已按面板尺寸解码，优先级高于卡片缩略图。
            self._show_current()
            can_reuse = True
        elif card_cache_matches:
            self._cached_pixmap = trim_uniform_border(cached_pixmap)
            self._cached_path = current_path
            self._show_current()
            can_reuse = True
        elif not self._same_image_path(self._cached_path, current_path):
            self._cached_pixmap = None
            self._cached_path = ""
            self._image_label.clear()
            self._image_label.setText(tr("正在加载预览..."))
            self._image_label.setStyleSheet(
                f"QLabel {{ background-color: {theme.surface_color('bg.subtle', 'placeholder')}; "
                f"border: none; border-radius: {theme.radius('card')}px; "
                f"color: {theme.color('text.tertiary')}; font-size: 12px; }}"
            )
        else:
            self._show_current()
            can_reuse = self._cached_pixmap is not None and not self._cached_pixmap.isNull()
        self._update_nav()
        return can_reuse

    def cached_pixmap_needs_upgrade(
        self,
        image_path: str,
        target_width: int,
        target_height: int,
    ) -> bool:
        """当前缓存若需明显放大才能装入目标边界，就应加载更高清的版本。"""
        pixmap = self._cached_pixmap
        if pixmap is None or pixmap.isNull() or not self._same_image_path(self._cached_path, image_path):
            return True
        if pixmap.width() <= 0 or pixmap.height() <= 0:
            return True
        contain_scale = min(
            max(1, target_width) / pixmap.width(),
            max(1, target_height) / pixmap.height(),
        )
        # 给黑边裁切和格式解码尺寸取整留少量余量，避免重复升级同一张图。
        return contain_scale > 1.12

    def apply_deferred_pixmap(self, image_path: str, pixmap: QPixmap | None) -> bool:
        """仅当后台结果仍对应当前图片时应用，防止快速切换 Mod 后串图。"""
        if not self._images or self._current_index >= len(self._images):
            return False
        current_path = self._images[self._current_index]
        if not self._same_image_path(current_path, image_path):
            return False
        if pixmap is None or pixmap.isNull():
            self._cached_pixmap = None
            self._cached_path = ""
            self._image_label.clear()
            self._image_label.setText(tr("预览图无法加载"))
            self._style_placeholder()
            return False

        self._cached_pixmap = trim_uniform_border(pixmap)
        self._cached_path = current_path
        self._show_current()
        return True

    def add_image(self, path: str) -> None:
        """添加预览图"""
        if fs.exists(path):
            self._images.append(path)
            self._current_index = len(self._images) - 1
            self._show_current()
            self._update_nav()

    def clear(self) -> None:
        """清空预览图"""
        self._images.clear()
        self._current_index = 0
        self._cached_pixmap = None
        self._cached_path = ""
        self._image_label.clear()
        self._image_label.setText(self._empty_text)
        self._style_placeholder()
        self._update_nav()

    def set_empty_text(self, text: str) -> None:
        """设置无预览图时的提示文本"""
        self._empty_text = text
        if not self._images:
            self._image_label.setText(text)

    def _show_current(self, smooth: bool = True) -> None:
        """显示当前预览图"""
        if not self._images or self._current_index >= len(self._images):
            return

        path = self._images[self._current_index]
        # 仅在路径变化时重新加载图片，避免重复磁盘读取
        if path != self._cached_path or self._cached_pixmap is None:
            # 从完整路径读字节再解码：同一 Mod 内常有多个不同目录下同名 preview.png；
            # 不让 Qt 的文件名缓存参与，确保每个索引对应自己的真实图片内容。
            pm = load_pixmap_file(path)
            if not pm.isNull():
                # 先缩小超大图片，限制边框检测和后续缩放的时间及内存开销。
                if max(pm.width(), pm.height()) > self._WORK_MAX_EDGE:
                    pm = pm.scaled(
                        self._WORK_MAX_EDGE,
                        self._WORK_MAX_EDGE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                # 部分预览图的像素包含近黑或透明边框，完整适配时会一并显示。
                # 裁除连续的纯色边框以保留有效画面；无此类边框的图像保持不变。
                pm = trim_uniform_border(pm)
            self._cached_pixmap = pm
            self._cached_path = path
        if self._cached_pixmap is not None and not self._cached_pixmap.isNull():
            # 等比适配并居中显示完整图像，留白透出面板背景。
            size = self._image_label.size()
            scaled = fit_contain_rounded(
                self._cached_pixmap,
                size.width(),
                size.height(),
                radius=8,
                smooth=smooth,
            )
            self._image_label.clear()
            self._image_label.setPixmap(scaled)
            self._style_image()
