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
