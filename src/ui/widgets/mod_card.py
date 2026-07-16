"""展示本地 Mod 的预览、启用状态、分级及标签，并适配主题和卡片尺寸。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from src.models.mod import AgeRating, Mod
from src.models.tag import TagDefinition
from src.ui import icons, theme
from src.ui.widgets.card_render_cache import preview_cache_key
from src.ui.widgets.card_type_badge import CardTypeBadge
from src.ui.widgets.image_utils import fill_blur_rounded, load_pixmap_file
from src.ui.widgets.spinner import ShimmerLabel
from src.ui.widgets.tag_display import TagStrip, tags_for_names
from src.utils import fs
from src.utils.i18n import t as tr


class ModCard(QFrame):
    """复用本地 Mod 预览与状态控件，通过信号向外层交付选择、标签筛选和启停请求。"""

    clicked = Signal(str)
    double_clicked = Signal(str)
    toggle_requested = Signal(str)
    tag_clicked = Signal(str)

    CARD_WIDTH = 200
    CARD_HEIGHT = 280
    PREVIEW_HEIGHT = 180

    def __init__(
        self,
        mod: Mod | None = None,
        parent: QWidget | None = None,
        adult_mode: str = "show",
        card_width: int = 200,
        card_height: int = 280,
    ) -> None:
        """初始化卡片尺寸、分级显示和预览缓存，创建子控件后可立即绑定 Mod。"""
        super().__init__(parent)
        self._mod: Mod | None = None
        self._selected = False
        self._cut_state = False
        self._hover = False
        # 成人级(X)预览模式："show"=正常 | "hide"=隐藏占位 | "blur"=模糊
        self._adult_mode = adult_mode
        self._card_width = card_width
        self._card_height = card_height
        self._tag_catalog: dict[str, TagDefinition] = {}
        # 动态调整预览图占比：小卡片时降低预览图比例，为文字信息留更多空间
        preview_ratio = 0.64 if card_height >= 200 else max(0.52, 0.64 - (200 - card_height) * 0.002)
        self._preview_height = int(card_height * preview_ratio)
        self._cached_pixmap: QPixmap | None = None  # 缓存原始大图，避免重复读盘
        self._preview_requested = False
        self._preview_resolved = False
        # 预览栏选中其他图片后记录覆盖路径，防止延迟加载或视图切换恢复为首张预览。
        self._preview_override_path: str | None = None
        self._setup_ui()
        if mod:
            self.set_mod(mod)

    def _setup_ui(self) -> None:
        """按卡片高度分配预览与信息空间，建立透明内容层、状态标记和标签行。"""
        self.setFixedSize(self._card_width, self._card_height)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("modCard")
        # 添加子控件前设置固定透明样式，状态变化只重绘边框，不递归重设整张卡片样式。
        self.setStyleSheet("#modCard { background: transparent; border: 1px solid transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 预览图容器（用于叠加角标）
        self._preview_container = QWidget()
        self._preview_container.setFixedHeight(self._preview_height)
        # 预览容器保持透明，使图片边缘和圆角显示卡片本身的背景。
        self._preview_container.setStyleSheet("background: transparent;")
        preview_container_layout = QVBoxLayout(self._preview_container)
        preview_container_layout.setContentsMargins(0, 0, 0, 0)
        preview_container_layout.setSpacing(0)

        self._preview_label = ShimmerLabel()
        self._preview_label.setFixedHeight(self._preview_height)
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setObjectName("modPreview")
        self._preview_label.setScaledContents(False)
        preview_container_layout.addWidget(self._preview_label)

        # 类型角标采用共用的圆角、图标和文字布局。
        self._type_badge = CardTypeBadge("mod", self._card_width, self._preview_container)

        # 右上角"可更新"角标，默认隐藏，随卡片高度分级降级
        self._update_available = False
        self._update_badge = QLabel(tr("可更新"), self._preview_container)
        self._update_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_badge.hide()
        self._layout_update_badge()

        layout.addWidget(self._preview_container)

        # 计算信息区可用高度
        info_avail = self._card_height - self._preview_height - 8 - 4  # 内容边距与间距。
        scale_ratio = max(0.4, min(1.0, self._card_height / 280))
        indicator_h = max(10, int(22 * scale_ratio))
        font_size = int(theme.font("size_card_title" if self._card_height >= 170 else "size_card_title_compact"))
        name_min_h = font_size + 8
        name_max_h = name_min_h

        self._info_widget = QWidget()
        # 内容容器保持透明，避免全局 QWidget 背景规则填充近黑底色，
        # 遮挡卡片下半部并在圆角预览边缘露出黑色。
        self._info_widget.setStyleSheet("background: transparent;")
        self._info_widget.setMinimumHeight(max(20, info_avail))
        info_layout = QVBoxLayout(self._info_widget)
        info_layout.setContentsMargins(0, 2, 0, 0)
        info_layout.setSpacing(0)

        # 名称 + 状态指示条 + 分级角标
        name_row = QHBoxLayout()
        name_row.setSpacing(4)

        self._status_indicator = QLabel()
        self._status_indicator.setFixedSize(6, indicator_h)
        name_row.addWidget(self._status_indicator)

        self._name_label = QLabel(tr("未命名"))
        self._name_label.setWordWrap(False)
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setMinimumHeight(name_min_h)
        self._name_label.setMaximumHeight(name_max_h)
        self._name_label.setStyleSheet(f"font-weight: bold; font-size: {font_size}px;")
        name_row.addWidget(self._name_label, 1)

        # 年龄分级角标
        self._rating_badge = QLabel("")
        self._rating_badge.setFixedSize(22, 16)
        self._rating_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rating_badge.setStyleSheet("font-size: 9px; font-weight: bold; border-radius: 3px; color: white;")
        name_row.addWidget(self._rating_badge)
        info_layout.addLayout(name_row)

        self._tag_row = TagStrip(self._info_widget, limit=1, height=22, font_size=int(theme.font("size_caption")))
        self._tag_row.tag_clicked.connect(self.tag_clicked)
        info_layout.addWidget(self._tag_row)
        info_layout.addStretch()
        self._update_tag_scale()

        layout.addWidget(self._info_widget)

    def set_mod(self, mod: Mod) -> None:
        """绑定 Mod 并同步文字、预览加载和启用状态显示。"""
        self._mod = mod
        self._set_mod_text(mod)
        self._load_mod_pixmap(mod)
        self._update_status()

    def set_mod_deferred(self, mod: Mod) -> None:
        """仅设置文字/状态信息，跳过预览图加载（用于渐进式加载优化）"""
        self._cached_pixmap = None
        self._preview_override_path = None
        self._preview_requested = False
        self._preview_resolved = not bool(mod.primary_preview) or (
            self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X
        )
        self._selected = False
        self._cut_state = False
        self._mod = mod
        if self._preview_label.styleSheet():
            self._preview_label.setStyleSheet("")
        self._set_mod_text(mod)
        # 预览图先显示占位文字，稍后由 load_pixmap_deferred 加载
        if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
            self._preview_label.setText(tr("已隐藏"))
            self._preview_label.setStyleSheet(
                f"QLabel {{ background-color: {theme.surface_color('bg.card', 'placeholder')}; "
                f"color: {theme.color('danger')}; font-size: 12px; "
                f"border-radius: 4px; }}"
            )
        elif mod.primary_preview:
            self._preview_label.start_loading()
        else:
            self._preview_label.setText(tr("无预览"))
        self._apply_adult_blur()
        self._update_status()

    def effective_preview_path(self) -> str | None:
        """返回卡片应显示的预览路径，优先使用用户选择的图片，其次使用首张预览。

        列表切换、卡片重建和后台加载均采用此路径，保持卡片与预览栏一致。
        """
        if self._preview_override_path and fs.exists(self._preview_override_path):
            return self._preview_override_path
        if self._mod and self._mod.primary_preview:
            return self._mod.primary_preview
        return None

    def scroll_render_key(self) -> tuple | None:
        """组合预览、文本及交互外观状态生成滚动绘制缓存键，预览不可缓存时返回 None。"""
        preview = preview_cache_key(self._preview_label)
        if preview is None:
            return None
        return (
            preview,
            self._name_label.text(),
            self._rating_badge.text(),
            self._selected,
            bool(self._mod and self._mod.is_enabled),
            self._update_available,
            self._adult_mode,
            self._tag_row._render_signature,
            self._cut_state,
            self._hover,
            self._chrome_parameters(),
            theme.radius("card"),
        )

    def set_preview_pixmap(self, pixmap: QPixmap | None, *, rendered: QPixmap | None = None) -> bool:
        """应用已解码的预览图，并统一结束骨架态和更新成人内容模糊。"""
        if (
            pixmap is None
            or pixmap.isNull()
            or self._mod is None
            or (self._adult_mode == "hide" and self._mod.info.age_rating == AgeRating.X)
        ):
            return False
        self._cached_pixmap = pixmap
        self._preview_requested = False
        self._preview_resolved = True
        scaled = (
            rendered
            if rendered is not None
            else fill_blur_rounded(
                pixmap,
                self._card_width - 8,
                self._preview_height,
                radius=8,
            )
        )
        self._preview_label.setPixmap(scaled)
        if self._preview_label.styleSheet():
            self._preview_label.setStyleSheet("")
        self._apply_adult_blur()
        return True

    def set_no_preview(self) -> None:
        """结束当前预览等待状态并显示无预览占位，同时应用分级显示规则。"""
        self._cached_pixmap = None
        self._preview_requested = False
        self._preview_resolved = True
        self._preview_label.setText(tr("无预览"))
        self._apply_adult_blur()

    def load_pixmap_deferred(self) -> None:
        """延迟加载预览图（由外部定时器调用）"""
        if not self._mod:
            return
        if self._adult_mode == "hide" and self._mod.info.age_rating == AgeRating.X:
            return  # 已设置隐藏文字
        path = self.effective_preview_path()
        if path and fs.exists(path):
            pixmap = load_pixmap_file(path)
            if self.set_preview_pixmap(pixmap):
                return
        self._cached_pixmap = None
        self._preview_label.setText(tr("无预览"))

    def _set_mod_text(self, mod: Mod) -> None:
        """设置卡片的文字/角标信息（不包含预览图）"""
        self._elide_name(mod.display_name)
        self._tag_row.set_tags(tags_for_names(mod.info.tags, self._tag_catalog))

        # 年龄分级角标
        rating_colors = {
            AgeRating.G: theme.color("success"),
            AgeRating.P: theme.color("warning"),
            AgeRating.R: theme.color("warning"),
            AgeRating.X: theme.color("danger"),
        }
        r_color = rating_colors.get(mod.info.age_rating, theme.color("success"))
        self._rating_badge.setText(mod.info.age_rating.value)
        rating_style = (
            f"font-size: 9px; font-weight: bold; border-radius: 3px; color: white; background-color: {r_color};"
        )
        if self._rating_badge.styleSheet() != rating_style:
            self._rating_badge.setStyleSheet(rating_style)

    def set_tag_catalog(self, catalog: dict[str, TagDefinition]) -> None:
        """复用页面的规范化名称缓存，不逐卡查询数据库。"""
        self._tag_catalog = catalog
        if self._mod:
            self._tag_row.set_tags(tags_for_names(self._mod.info.tags, catalog))

    def _update_tag_scale(self) -> None:
        """依据卡片高度控制标签数量和可见性，避免小卡片信息区拥挤。"""
        self._tag_row.set_limit(2 if self._card_height >= 300 else 1)
        self._tag_row.setVisible(self._card_height >= 170)

    def _load_mod_pixmap(self, mod: Mod) -> None:
        """加载预览图到卡片"""
        path = self.effective_preview_path()
        if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
            self._cached_pixmap = None
            self._preview_label.setText(tr("已隐藏"))
            self._preview_label.setStyleSheet(
                f"QLabel {{ background-color: {theme.surface_color('bg.card', 'placeholder')}; "
                f"color: {theme.color('danger')}; font-size: 12px; "
                f"border-radius: 4px; }}"
            )
        elif path and fs.exists(path):
            pixmap = load_pixmap_file(path)
            if not self.set_preview_pixmap(pixmap):
                self._cached_pixmap = None
                self._preview_label.setText(tr("无预览"))
        else:
            self._cached_pixmap = None
            self._preview_label.setText(tr("无预览"))
        self._apply_adult_blur()

    def _apply_adult_blur(self) -> None:
        """blur 模式下给 X 级预览图加模糊（与 Mod 商店 NSFW 模糊一致）；其余模式清除模糊。

        只在确有预览图（_cached_pixmap）时模糊；占位文字/无预览不模糊。
        """
        want = (
            self._adult_mode == "blur"
            and self._mod is not None
            and self._mod.info.age_rating == AgeRating.X
            and self._cached_pixmap is not None
        )
        if want:
            if not isinstance(self._preview_label.graphicsEffect(), QGraphicsBlurEffect):
                effect = QGraphicsBlurEffect(self._preview_label)
                effect.setBlurRadius(20)
                self._preview_label.setGraphicsEffect(effect)
        elif self._preview_label.graphicsEffect() is not None:
            self._preview_label.setGraphicsEffect(None)

    def _update_status(self) -> None:
        """同步已绑定 Mod 的启用指示色并请求重绘卡片边框。"""
        if not self._mod:
            return
        indicator_color = theme.color("success" if self._mod.is_enabled else "stroke.divider")
        indicator_style = f"background-color: {indicator_color}; border-radius: 3px;"
        if self._status_indicator.styleSheet() != indicator_style:
            self._status_indicator.setStyleSheet(indicator_style)
        self.update()

    def _chrome_parameters(self) -> tuple[str, str, float | None, float, Qt.PenStyle]:
        """按主题、启用、选择、悬停和剪切状态返回可用于缓存比较的背景与边框参数。"""
        # 滚动时高频缓存检查仅使用标量，绘制新像素时才创建颜色对象。
        mode = self.property("_appearanceTheme") or theme.current_theme()
        background = self.property("_backgroundSurface")
        if background is None:
            background = theme.is_background_active()
        enabled = bool(self._mod and self._mod.is_enabled)
        border_token = "success" if enabled else "accent" if self._selected or self._hover else "stroke.card"
        width = 2.0 if enabled else 1.0
        fill_token, alpha = "bg.card", theme.ACRYLIC["card"]
        if self._selected:
            fill_token = "accent.subtle"
        elif self._hover:
            # 启用卡片仍保留悬停底色，背景模式使用较强透明层。
            fill_token = "bg.hover" if background else "bg.subtle"
            alpha = theme.ACRYLIC["strong"]
        if self._cut_state:
            return "#e8a838", theme.color("warning", mode), 0.08, 2.0, Qt.PenStyle.DashLine
        return (
            theme.color(fill_token, mode),
            theme.color(border_token, mode),
            alpha if background else None,
            width,
            Qt.PenStyle.SolidLine,
        )

    def _chrome_style(self) -> tuple[QColor, QColor, float, Qt.PenStyle]:
        """把标量外观参数转换为绘制所需的颜色、线宽和线型，应用背景透明度。"""
        fill_color, border_color, alpha, width, pen_style = self._chrome_parameters()
        fill = QColor(fill_color)
        if alpha is not None:
            fill.setAlphaF(alpha)
        return fill, QColor(border_color), width, pen_style

    def paintEvent(self, event) -> None:
        """按当前状态绘制圆角背景与边框，并将描边限制在卡片范围内。"""
        fill, border, width, pen_style = self._chrome_style()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(fill)
        painter.setPen(QPen(border, width, pen_style))
        inset = width / 2
        radius = max(0.0, theme.radius("card") - inset)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), radius, radius)

    def enterEvent(self, event) -> None:
        """记录鼠标悬停状态并刷新卡片外观。"""
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """清除鼠标悬停状态并刷新卡片外观。"""
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def set_cut_state(self, is_cut: bool) -> None:
        """仅在剪切标记变化时请求重绘，使用虚线外观表示待移动的条目。"""
        if self._cut_state == is_cut:
            return
        self._cut_state = is_cut
        self.update()

    def set_selected(self, selected: bool) -> None:
        """同步卡片选择标记，实际变化时更新与启用状态共同决定的外观。"""
        if self._selected == selected:
            return
        self._selected = selected
        self._update_status()

    def show_preview_by_path(
        self,
        image_path: str,
        pixmap: QPixmap | None = None,
    ) -> bool:
        """显示指定路径的预览图（用于与右侧预览栏同步切换）。

        记录为覆盖路径：卡片视图重建、延迟/后台重加载都会沿用同一张，
        避免列表↔卡片视图切换时卡片回退到首图（串图 bug）。
        """
        if not self._mod:
            return False
        if self._adult_mode == "hide" and self._mod.info.age_rating == AgeRating.X:
            return False
        # 空路径或首图清除覆盖设置，恢复主预览。
        self._preview_override_path = None if not image_path or image_path == self._mod.primary_preview else image_path
        if image_path and (pixmap is not None or fs.exists(image_path)):
            loaded = pixmap if pixmap is not None else load_pixmap_file(image_path)
            if self.set_preview_pixmap(loaded):
                return True
        else:
            self._cached_pixmap = None
            self._preview_label.setText(tr("无预览"))
        return False

    def _elide_name(self, text: str) -> None:
        """根据卡片宽度省略过长的名称"""
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(self._name_label.font())
        avail_w = self._card_width - 46  # 预留边距、状态点、分级标识和间距。
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail_w)
        self._name_label.setText(elided)
        if elided != text:
            self._name_label.setToolTip(text)
        else:
            self._name_label.setToolTip("")
