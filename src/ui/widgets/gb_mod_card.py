"""展示在线 Mod 的缩略图、日期、作者、统计及安装状态，支持内容分级和悬停反馈。"""

from __future__ import annotations

from datetime import datetime
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

        self.date_badge = QLabel(self)
        self.date_badge.setStyleSheet(
            "background: rgba(0, 0, 0, 0.55); color: white; padding: 2px 6px; border-radius: 4px; font-size: 8pt;"
        )
        self._update_date_badge()

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

    def cancel_thumbnail(self) -> None:
        """标签绑定新记录前撤销旧图片投递。"""
        cancel = getattr(self._image_loader, "cancel_for", None)
        if callable(cancel):
            cancel(self.thumbnail)
        self._thumb_loaded = False

    def scroll_render_key(self) -> tuple | None:
        """组合缩略图、文字和徽章状态生成绘制缓存键，预览尚不可缓存时返回 None。"""
        preview = preview_cache_key(self.thumbnail)
        if preview is None:
            return None
        return (
            preview,
            self.title_label.text(),
            self.author_label.text(),
            self.view_label.text(),
            self.like_label.text(),
            self.dl_label.text(),
            self.type_badge.text(),
            self.type_badge.isHidden(),
            self.nsfw_badge.isHidden(),
            self.installed_badge.isHidden(),
            self.date_badge.text(),
            self.date_badge.isHidden(),
            self._selected,
        )

    def set_record(
        self,
        record: GbModRecord,
        *,
        nsfw_policy: Literal["remove", "blur", "show"] | None = None,
        is_installed: bool = False,
        download_count_pending: bool = False,
        defer_image: bool = True,
    ) -> None:
        """更新已有控件树，信号始终读取当前记录。"""
        self.cancel_thumbnail()
        if self._scale_anim is not None:
            try:
                self._scale_anim.stop()
            except RuntimeError:
                pass
        self._animating = False
        if self._rest_geometry is not None:
            self.setGeometry(self._rest_geometry)
        self.record = record
        if nsfw_policy is not None:
            self._nsfw_policy = nsfw_policy
        self._defer_image = bool(defer_image)
        self._dl_pending = bool(download_count_pending) and not record.download_count
        self.set_selected(False)
        self.title_label.setText(record.name)
        self.author_label.setText(record.submitter.name)
        self.view_label.setText(str(record.view_count))
        self.like_label.setText(str(record.like_count))
        self.dl_label.setText("…" if self._dl_pending else str(record.download_count))
        self.type_badge.setText(localize_category_name(record.model.value))
        self.type_badge.adjustSize()
        self.type_badge.move(_CARD_W - self.type_badge.width() - 6, 6)
        self.type_badge.setVisible(record.model.value != "Mod")
        self.nsfw_badge.setVisible(record.is_nsfw)
        self.set_installed(is_installed)
        self._update_date_badge()
        # 显示新占位前清除旧的清晰和模糊像素。
        self.thumbnail.start_loading()
        self.thumbnail.set_blur(record.is_nsfw and self._nsfw_policy == "blur")
        if not record.thumbnail_url:
            self.thumbnail.setPixmap(_build_loading_placeholder())
        elif not self._restore_cached_thumbnail() and not self._defer_image:
            self._do_load_thumbnail()

    def _update_date_badge(self) -> None:
        """在预览图右下角显示本地日期，使用模型的发布回退值，异常时间不显示占位文字。"""
        try:
            timestamp = self.record.date_updated_ts
            date_time = datetime.fromtimestamp(timestamp) if timestamp > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            date_time = None
        self.date_badge.setText(date_time.strftime("%Y-%m-%d") if date_time is not None else "")
        self.date_badge.setToolTip(date_time.strftime("%Y-%m-%d %H:%M") if date_time is not None else "")
        self.date_badge.adjustSize()
        self.date_badge.move(_CARD_W - self.date_badge.width() - 6, _THUMB_H - self.date_badge.height() - 6)
        self.date_badge.setVisible(date_time is not None)

    def sizeHint(self) -> QSize:
        """为使用绝对定位子控件的卡片提供明确的固定布局尺寸。"""
        # 子控件采用绝对定位，需显式提供尺寸提示，避免流式布局将卡片按零宽度叠放。
        return QSize(_CARD_W, _CARD_H)

    def minimumSizeHint(self) -> QSize:
        """保持卡片的最小建议尺寸与固定缩略图及信息布局一致。"""
        return QSize(_CARD_W, _CARD_H)

    def mousePressEvent(self, event):
        """鼠标左键按下时向使用方发送当前资源记录。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.record)
        super().mousePressEvent(event)

    # 缩略图延迟加载
    def _restore_cached_thumbnail(self) -> bool:
        """尝试同步恢复加载器缓存，命中后标记图片就绪并直接收起加载占位。"""
        restore = getattr(self._image_loader, "try_cached", None)
        if not callable(restore) or not restore(
            self.record.thumbnail_url,
            self.thumbnail,
            size=QSize(_CARD_W, _THUMB_H),
            crop_to_size=True,
        ):
            return False
        self._thumb_loaded = True
        # 缓存命中时立即显示，复用可见卡片不再重新淡入。
        anim._cancel_fade(self.thumbnail._shimmer)
        self.thumbnail._shimmer.hide()
        return True

    def _do_load_thumbnail(self) -> None:
        """幂等提交缩略图加载请求。"""
        if self._thumb_loaded or not self.record.thumbnail_url:
            return
        if self._restore_cached_thumbnail():
            return
        self._thumb_loaded = True
        self._image_loader.load(
            self.record.thumbnail_url,
            self.thumbnail,
            size=QSize(_CARD_W, _THUMB_H),
            crop_to_size=True,
        )

    def load_thumbnail(self) -> None:
        """由 GbAdaptiveGrid 在滚动中分批调用，触发缩略图加载（幂等）。"""
        self._do_load_thumbnail()

    # 选中态
    def set_selected(self, selected: bool) -> None:
        """更新选中状态，并在最上层显示边框。"""
        if self._selected == bool(selected):
            return
        self._selected = bool(selected)
        if self._selected:
            # 显示前提升覆盖层，避免子控件遮挡选中边框。
            self._selection_overlay.setGeometry(0, 0, self.width(), self.height())
            self._selection_overlay.raise_()
            self._selection_overlay.show()
            self._selection_overlay.update()
        else:
            self._selection_overlay.hide()

    # 下载量回填
    def update_downloads(self, count: int) -> None:
        """更新卡片下载量并写回记录，供后续详情或卡片重建复用。"""
        try:
            self.record.download_count = int(count)
        except (TypeError, ValueError):
            return
        self._dl_pending = False
        self.dl_label.setText(str(self.record.download_count))

    # 安装状态实时刷新
    def set_installed(self, is_installed: bool) -> None:
        """同步安装徽章，使当前卡片立即反映本地安装状态。"""
        try:
            self.installed_badge.setVisible(bool(is_installed))
        except RuntimeError:
            # 卡片在派发期间被销毁（例如用户切了分类），静默丢弃
            pass

    # NSFW 切换
    def moveEvent(self, event):
        """非动画位移时记录新的静止几何，供后续悬停缩放使用。"""
        # 父布局调整位置后更新静止几何，仅在没有缩放动画时记录，
        # 避免将动画中间状态作为下一次缩放基准。
        if not self._animating:
            self._rest_geometry = QRect(self.pos(), self.size())
        super().moveEvent(event)

    def resizeEvent(self, event):
        """非动画缩放时更新静止几何，并使有效选中覆盖层始终匹配卡片尺寸。"""
        if not self._animating:
            self._rest_geometry = QRect(self.pos(), self.size())
        # 让选中描边 overlay 跟随卡片当前尺寸（hover 动画期间也保持铺满）
        try:
            self._selection_overlay.setGeometry(0, 0, self.width(), self.height())
        except (AttributeError, RuntimeError):
            pass
        super().resizeEvent(event)

    def enterEvent(self, event):
        """通知资源悬停并提升卡片层级，按配置启动轻微放大动画。"""
        self.hovered.emit(self.record)
        self.raise_()
        if self._hover_scale:
            self._animate_scale(1.02)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """按配置将悬停卡片恢复到静止尺寸。"""
        if self._hover_scale:
            self._animate_scale(1.0)
        super().leaveEvent(event)

    def _animate_scale(self, target: float) -> None:
        """始终以静止几何计算缩放目标，替换已有动画并防止动画中间值污染基准位置。"""
        # 缩放始终以静止几何为基准，进入时绕中心放大，离开时完整恢复，
        # 避免基于动画中间位置计算导致累计偏移。
        if self._rest_geometry is None:
            self._rest_geometry = QRect(self.pos(), self.size())
        rest = self._rest_geometry
        delta_w = int(_CARD_W * (target - 1.0))
        delta_h = int(_CARD_H * (target - 1.0))
        # 将扩展后的位置限制在父控件内，避免左侧及顶部卡片的边缘被裁切。
        nx = rest.x() - delta_w // 2
        ny = rest.y() - delta_h // 2
        if nx < 0:
            nx = 0
        if ny < 0:
            ny = 0
        new = QRect(nx, ny, _CARD_W + delta_w, _CARD_H + delta_h)
        if self._scale_anim is not None:
            # 动画结束后 Qt 对象可能已删除，Python 引用仍可能存在。
            try:
                self._scale_anim.stop()
            except RuntimeError:
                pass
            self._scale_anim = None
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.setStartValue(self.geometry())
        anim.setEndValue(new)
        # 动画期间屏蔽 moveEvent/resizeEvent 对 _rest_geometry 的写入
        self._animating = True
        anim.finished.connect(self._on_anim_finished)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._scale_anim = anim

    def _on_anim_finished(self) -> None:
        """结束动画状态并清除已由 Qt 回收的动画引用。"""
        self._animating = False
        # 动画结束时同时清除 Python 引用，避免再次访问已删除的 Qt 对象。
        self._scale_anim = None

    def _build_context_menu(self) -> QMenu:
        """为当前资源构建复制链接和浏览器打开入口，缺少页面地址时按资源标识生成。"""
        menu = QMenu(self)
        url = self.record.page_url or build_mod_page_url(self.record.model, self.record.mod_id)
        act_copy = QAction(icons.get_icon("actions/copy"), tr("复制 GameBanana 链接"), menu)
        act_copy.triggered.connect(lambda _checked=False: self._copy_link(url))
        menu.addAction(act_copy)
        act_open = QAction(icons.get_icon("actions/open"), tr("在浏览器中打开"), menu)
        act_open.triggered.connect(lambda _checked=False: self._open_in_browser(url))
        menu.addAction(act_open)
        return menu

    def _on_context_menu(self, pos: QPoint) -> None:
        """在卡片对应屏幕位置执行菜单，关闭后安排释放并容忍卡片已被刷新销毁。"""
        menu = self._build_context_menu()
        try:
            menu.exec(self.mapToGlobal(pos))
        finally:
            try:
                menu.deleteLater()
            except RuntimeError:
                # 全量刷新可能已经删除所属卡片。
                pass

    def _copy_link(self, url: str | None = None) -> None:
        """将指定地址或当前资源的页面地址复制到系统剪贴板。"""
        url = (
            url
            or self.record.page_url
            or build_mod_page_url(
                self.record.model,
                self.record.mod_id,
            )
        )
        QApplication.clipboard().setText(url)

    def _open_in_browser(self, url: str | None = None) -> None:
        """通过系统默认浏览器打开指定地址或当前资源页面。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = (
            url
            or self.record.page_url
            or build_mod_page_url(
                self.record.model,
                self.record.mod_id,
            )
        )
        QDesktopServices.openUrl(QUrl(url))
