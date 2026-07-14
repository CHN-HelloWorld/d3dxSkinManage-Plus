"""展示在线 Mod 的缩略图、作者、统计及安装状态，支持内容分级和悬停反馈。"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.models import GbModRecord
from src.services.gamebanana.url_helper import build_mod_page_url
from src.ui import anim, icons, theme
from src.ui.widgets.card_render_cache import preview_cache_key
from src.ui.widgets.spinner import ShimmerPlaceholder
from src.utils.i18n import localize_category_name
from src.utils.i18n import t as tr

_CARD_W = 210
_CARD_H = 270
_THUMB_H = 160


def _round_top_corners(pm: QPixmap, radius: int) -> QPixmap:
    """将图像顶部两角裁为圆角，底部与文字区连接的两角保持方形，半径按设备像素比缩放。"""
    if pm.isNull() or radius <= 0:
        return pm
    dpr = pm.devicePixelRatio() or 1.0
    w, h = pm.width(), pm.height()  # 设备像素
    r = int(min(radius * dpr, w / 2, h))
    if r <= 0:
        return pm
    out = QPixmap(w, h)
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.moveTo(0, h)
        path.lineTo(0, r)
        path.quadTo(0, 0, r, 0)
        path.lineTo(w - r, 0)
        path.quadTo(w, 0, w, r)
        path.lineTo(w, h)
        path.closeSubpath()
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
    finally:
        p.end()
    return out


def _blur_pixmap(pm: QPixmap, radius: float) -> QPixmap:
    """对 pixmap 执行一次高斯模糊并返回静态结果。

    预先计算模糊像素，避免将 QGraphicsBlurEffect 挂在标签上而随每次重绘执行离屏模糊。
    滚动时可直接绘制处理后的 pixmap。
    """
    if pm.isNull() or radius <= 0:
        return pm
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pm)
    eff = QGraphicsBlurEffect()
    eff.setBlurRadius(radius)
    item.setGraphicsEffect(eff)
    scene.addItem(item)
    out = QImage(pm.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(0)
    p = QPainter(out)
    r = QRectF(0, 0, pm.width(), pm.height())
    scene.render(p, r, r)
    p.end()
    res = QPixmap.fromImage(out)
    res.setDevicePixelRatio(pm.devicePixelRatio())
    return res


def _build_loading_placeholder() -> QPixmap:
    """生成带有中文加载提示的预览占位图。"""
    pm = QPixmap(_CARD_W, _THUMB_H)
    pm.fill(QColor(theme.color("bg.subtle")))
    p = QPainter(pm)
    try:
        p.setPen(QColor(theme.color("text.secondary")))
        font = QFont()
        font.setPointSize(10)
        p.setFont(font)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, tr("加载中…"))
    finally:
        p.end()
    return pm


class _FillCropLabel(QLabel):
    """按比例填充并居中裁切缩略图。

    使用平滑缩放后裁切到固定区域，避免直接拉伸造成形变或使用快速缩放降低清晰度。
    """

    def __init__(self, target_size: QSize, parent: QWidget | None = None) -> None:
        """建立固定尺寸的缩略图区域，初始化清晰图、模糊图和加载占位状态。"""
        super().__init__(parent)
        self._target = QSize(target_size)
        self.setFixedSize(target_size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(False)
        # 预先缓存敏感内容的模糊图，避免滚动绘制时重复处理。
        self._blur_on = False
        self._sharp: QPixmap | None = None  # 当前清晰终图（裁切+圆角后）
        self._blurred: QPixmap | None = None  # 对应的模糊缓存（懒算）
        # 图片未到达时显示加载占位，设置图片后自动收起。
        self._shimmer = ShimmerPlaceholder(self, radius=5)
        self._shimmer.hide()

    def start_loading(self) -> None:
        """清空上一张图的缓存并启动覆盖整个缩略图区域的加载占位。"""
        self._sharp = None
        self._blurred = None
        super().clear()
        self._shimmer.setGeometry(self.rect())
        self._shimmer.start()

    def set_blur(self, on: bool) -> None:
        """设置敏感内容的模糊状态，并在图像到达后应用。"""
        on = bool(on)
        if on == self._blur_on:
            return
        self._blur_on = on
        self._refresh_display()

    def _refresh_display(self) -> None:
        """按分级显示开关选择清晰图或懒生成的模糊缓存。"""
        if self._sharp is None or self._sharp.isNull():
            return
        if self._blur_on:
            if self._blurred is None:
                self._blurred = _blur_pixmap(self._sharp, 20.0)
            super().setPixmap(self._blurred)
        else:
            super().setPixmap(self._sharp)

    def setPixmap(self, pm: QPixmap) -> None:
        """将有效图片等比裁剪到目标尺寸并处理顶部圆角，更新缓存后结束加载占位。"""
        if pm is None or pm.isNull():
            super().setPixmap(pm)
            return
        if pm.size() == self._target:
            final = pm
        else:
            scaled = pm.scaled(
                self._target,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width() - self._target.width()) // 2)
            y = max(0, (scaled.height() - self._target.height()) // 2)
            final = scaled.copy(x, y, self._target.width(), self._target.height())
        # 缩略图上沿与卡片使用相同圆角，避免覆盖卡片边缘。
        final = _round_top_corners(final, theme.radius("card"))
        self._sharp = final
        self._blurred = None  # 新图 → 旧模糊缓存失效
        self._refresh_display()
        # 图片准备完成后结束加载占位，不依赖控件当前可见性。
        self._shimmer.reveal()


class _SelectionOverlay(QWidget):
    """通过独立覆盖层绘制完整选中边框。

    覆盖层位于缩略图和徽章上方，避免子控件遮住边框；忽略鼠标事件以保持卡片交互。
    """

    def __init__(self, parent: QWidget) -> None:
        """创建不接收鼠标事件的透明覆盖层，初始隐藏选中边框。"""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # 覆盖层自绘边框并跳过系统背景填充。
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def paintEvent(self, event):
        """在覆盖层内绘制完整强调色边框及浅色填充，避免子控件遮挡选中状态。"""
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            accent = QColor(theme.color("accent"))
            pen = p.pen()
            pen.setColor(accent)
            pen.setWidth(2)
            p.setPen(pen)
            # 使用半透明强调色表示选中状态。
            fill = QColor(accent)
            fill.setAlpha(26)  # ≈ 0.10 透明度
            p.setBrush(fill)
            # 向内缩进一像素，使两像素描边完整位于控件矩形内。
            rect = self.rect().adjusted(1, 1, -1, -1)
            p.drawRoundedRect(rect, 5, 5)
        finally:
            p.end()


class GbModCard(QFrame):
    """展示一条在线资源记录，通过信号交付选择和悬停意图，并管理缩略图与局部交互外观。"""

    clicked = Signal(GbModRecord)
    hovered = Signal(GbModRecord)

    # 固定尺寸（对外暴露）——GbAdaptiveGrid 虚拟滚动按格计算列数/总高，
    # 无需实例化一张卡来探测尺寸。值同模块级 _CARD_W/_CARD_H。
    CARD_WIDTH = _CARD_W
    CARD_HEIGHT = _CARD_H
    THUMBNAIL_HEIGHT = _THUMB_H

    def __init__(
        self,
        record: GbModRecord,
        *,
        image_loader,
        nsfw_policy: Literal["remove", "blur", "show"] = "blur",
        is_installed: bool = False,
        hover_scale: bool = True,
        download_count_pending: bool = False,
        defer_image: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        """绑定资源记录和图片加载器，建立徽章、统计及选中覆盖层，并按策略恢复或请求缩略图。"""
        super().__init__(parent)
        self.record = record
        self._image_loader = image_loader
        self._nsfw_policy = nsfw_policy
        self._hover_scale = hover_scale
        # 缓存命中立即恢复像素；尚未加载的图由 grid 在滚动中分小批提交。
        self._defer_image = bool(defer_image)
        self._thumb_loaded = False
        # 列表接口缺少下载量，补全前使用待加载占位。
        self._dl_pending = bool(download_count_pending) and not record.download_count
        # 选中状态由商店页面管理，蓝色描边通过顶层覆盖控件绘制，
        # 避免缩略图等子控件遮挡边框。
        self._selected = False
        self.setObjectName("gbModCard")
        # 保留默认框架边缘，选中状态由独立覆盖层绘制。
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # 加载原图后在缩略图控件中平滑缩放并居中裁切。
        self.thumbnail = _FillCropLabel(QSize(_CARD_W, _THUMB_H), self)
        self.thumbnail.setGeometry(0, 0, _CARD_W, _THUMB_H)
        if record.thumbnail_url:
            # 真实图片到达前保留加载占位。
            # 不传递图片占位参数，避免加载器提前调用 setPixmap 结束加载动画。
            if not self._restore_cached_thumbnail():
                self.thumbnail.start_loading()
                if not self._defer_image:
                    self._do_load_thumbnail()
        else:
            self.thumbnail.setPixmap(_build_loading_placeholder())

        if record.is_nsfw and nsfw_policy == "blur":
            # 预先计算模糊图，滚动时直接绘制缓存像素。
            self.thumbnail.set_blur(True)

        # 仅对非 Mod 类型显示类型徽章。
        self.type_badge = QLabel(localize_category_name(record.model.value), self)
        self.type_badge.setStyleSheet(
            "background: rgba(0, 0, 0, 0.55); color: white; padding: 2px 6px; border-radius: 4px;"
        )
        self.type_badge.adjustSize()
        self.type_badge.move(_CARD_W - self.type_badge.width() - 6, 6)
        self.type_badge.setVisible(record.model.value != "Mod")

        self.nsfw_badge = QLabel("NSFW", self)
        self.nsfw_badge.setStyleSheet(
            f"background: {theme.color('danger')}; color: white; padding: 2px 6px; border-radius: 4px;"
        )
        self.nsfw_badge.adjustSize()
        self.nsfw_badge.move(6, 6)
        self.nsfw_badge.setVisible(record.is_nsfw)

        # 已安装徽章
        self.installed_badge = QLabel(tr("已安装"), self)
        self.installed_badge.setStyleSheet(
            f"background: {theme.color('success')}; color: white; padding: 2px 6px; border-radius: 4px;"
        )
        self.installed_badge.adjustSize()
        self.installed_badge.move(6, _THUMB_H - self.installed_badge.height() - 6)
        self.installed_badge.setVisible(bool(is_installed))

        # 文字区域保持透明，使用整张卡片统一绘制的背景。
        # 祖先控件的透明样式须限定作用范围，避免覆盖卡片底色。
        info_widget = QWidget(self)
        info_widget.setGeometry(6, _THUMB_H + 4, _CARD_W - 12, _CARD_H - _THUMB_H - 8)
        v = QVBoxLayout(info_widget)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        self.title_label = QLabel(record.name)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: 600; font-size: 12pt;")
        v.addWidget(self.title_label)

        author_label = QLabel(record.submitter.name)
        self.author_label = author_label
        author_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 9pt;")
        v.addWidget(author_label)

        stats = QHBoxLayout()
        stats.setSpacing(3)
        self.view_label = QLabel(str(record.view_count))
        self.like_label = QLabel(str(record.like_count))
        self.dl_label = QLabel("…" if self._dl_pending else str(record.download_count))
        for icon_name, lbl in (
            ("meta/views", self.view_label),
            ("meta/likes", self.like_label),
            ("meta/downloads", self.dl_label),
        ):
            ico = QLabel()
            ico.setPixmap(icons.pixmap(icon_name, 13, "text.tertiary"))
            ico.setStyleSheet("background: transparent;")
            lbl.setStyleSheet(f"font-size: 8pt; color: {theme.color('text.tertiary')};")
            stats.addWidget(ico)
            stats.addWidget(lbl)
            stats.addSpacing(6)
        stats.addStretch(1)
        v.addLayout(stats)

        self._scale_anim: QPropertyAnimation | None = None
        # 保存悬停缩放的基准几何，避免反复进入和离开时累积位置偏移。
        self._rest_geometry: QRect | None = None
        self._animating: bool = False

        # 最后创建选中覆盖层，使其位于缩略图和徽章之上，并忽略鼠标事件。
        self._selection_overlay = _SelectionOverlay(self)
        self._selection_overlay.setGeometry(0, 0, _CARD_W, _CARD_H)
        self._selection_overlay.raise_()
