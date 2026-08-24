"""类别总览页 - 混合类别/Mod视图、双击导航、多选快捷键、嵌套目录、面包屑导航、i18n"""

from __future__ import annotations

import queue
from collections import OrderedDict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import (
    QCollator,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QLocale,
    QPoint,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QFontMetrics, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyledItemDelegate,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.ini_keys import IniWriteError, KeyBinding
from src.models.mod import AgeRating, Mod
from src.models.tag import TagDefinition, tag_key
from src.ui import acrylic, anim, icons, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.card_pool import CardPool
from src.ui.widgets.card_render_cache import preview_cache_key
from src.ui.widgets.card_reveal import CardRevealQueue
from src.ui.widgets.card_type_badge import CardTypeBadge
from src.ui.widgets.deploy_overlay import DeployOverlay
from src.ui.widgets.icon_combo import LeadingIconComboBox
from src.ui.widgets.image_utils import (
    fill_crop_rounded,
    load_pixmap_file,
    load_scaled_qimage_file,
)
from src.ui.widgets.key_capture import KeyCaptureField
from src.ui.widgets.local_preview_cache import (
    DecodedPreview,
    PreviewLayout,
    cached_preview,
    cached_rendered_preview,
    load_preview,
    prepare_preview,
    preview_cache_token,
    retain_viewed_preview,
)
from src.ui.widgets.mod_card import ModCard
from src.ui.widgets.mod_list import ModListTable
from src.ui.widgets.mod_variant_panel import ModVariantPanel
from src.ui.widgets.preview import PreviewWidget, clipboard_has_image, clipboard_image_path
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade
from src.ui.widgets.tag_chip import TagFlowLayout
from src.ui.widgets.tag_display import TagNameDelegate, TagStrip, tags_for_names
from src.ui.widgets.tag_filter import TagFilter
from src.ui.worker_lifecycle import manage_page_worker
from src.utils import fs
from src.utils.constants import ARCHIVE_EXTENSIONS
from src.utils.i18n import N_, t
from src.utils.i18n import t as tr
from src.utils.image_cache_budget import image_cache_budget
from src.utils.logger import logger
from src.utils.qt_timers import call_later

try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:
    _lazy_pinyin = None


@dataclass(frozen=True)
class _PresentedOverviewView:
    """保存已呈现概览的游戏、目录、布局和筛选快照，供判断视图是否可复用。"""

    key: tuple[str, str, str]  # 记录游戏、目录和当前布局。
    signature: tuple
    search_text: str
    tag_ids: tuple[str, ...]
    tag_mode: str


_AGE_RATING_LABELS = {
    AgeRating.G: N_("G - 全年龄"),
    AgeRating.P: N_("P - 轻微敏感"),
    AgeRating.R: N_("R - 限制级"),
    AgeRating.X: N_("X · 18+ 内容"),
}


def _age_rating_text(rating: AgeRating) -> str:
    """将 AgeRating 枚举转为可读描述文字"""
    return tr(_AGE_RATING_LABELS.get(rating, "G - 全年龄"))


class _ElidedLabel(QLabel):
    """保留完整 tooltip，并随可用宽度从右侧省略的单行标签。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        """保存完整文本与提示，允许标签宽度收缩并由后续尺寸事件计算省略显示。"""
        super().__init__(parent)
        self._full_text = text
        super().setText(text)
        self.setToolTip(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

    def setText(self, text: str) -> None:
        """替换完整文本和提示，再按当前内容宽度设置尾部省略文字。"""
        self._full_text = text
        self.setToolTip(text)
        super().setText(
            QFontMetrics(self.font()).elidedText(text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width()))
        )

    def resizeEvent(self, event) -> None:
        """标签尺寸变化后按完整文本重新计算省略显示。"""
        super().resizeEvent(event)
        available = max(0, self.contentsRect().width())
        text = QFontMetrics(self.font()).elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            available,
        )
        super().setText(text)


class CharacterCard(QFrame):
    """角色/类别卡片"""

    clicked = Signal(str)
    clipboard_menu_requested = Signal(object)
    double_clicked = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    export_requested = Signal(str)  # 导出该类别/角色子树下全部 Mod
    context_select_requested = Signal(str)  # 右键时若未选中则先选中
    open_folder_requested = Signal(str)  # 右键「打开源目录」
    online_search_requested = Signal(str)  # 右键「在线查找」→ 在线 Mod
    preview_dropped = Signal(str, str)  # char_id, image_path

    CARD_WIDTH = 160
    CARD_HEIGHT = 200

    @staticmethod
    def _preview_height_for(height: int) -> int:
        """尺寸调整和预览预取共用文字预留空间。"""
        compact = height < 160
        title_size = int(theme.font("size_card_title_compact" if compact else "size_card_title"))
        count_size = int(theme.font("size_caption" if compact else "size_secondary"))
        ratio = 0.64 if height >= 160 else max(0.52, 0.64 - (160 - height) * 0.003)
        # 为两行文字、边距和卡片间距预留空间。
        info_height = title_size + 8 + count_size + 6 + 16
        return max(1, min(int(height * ratio), height - info_height))

    def __init__(
        self,
        char_data: dict,
        parent: QWidget | None = None,
        card_width: int = 160,
        card_height: int = 200,
        defer_pixmap: bool = False,
    ):
        """保存分类数据与卡片尺寸，初始化选择和延后预览策略后构建卡片。"""
        super().__init__(parent)
        self._data = char_data
        self._selected = False
        self._card_width = card_width
        self._card_height = card_height
        self._defer_pixmap = defer_pixmap
        self._setup_ui()

    def _setup_ui(self):
        """构建分类预览、节点类型徽章、名称和启用数量，并连接菜单与数据绑定。"""
        self.setFixedSize(self._card_width, self._card_height)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("charCard")
        self._cut_state = False  # 剪切状态标记
        # 不在卡片上接受拖拽 —— 拖拽预览图到卡片改由 OverviewPage 统一处理
        # 这避免了大量文件拖拽时每个卡片触发 dragEnterEvent/setStyleSheet 导致的严重卡顿

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 预览图容器（用于叠加角标，与 ModCard 一致）
        compact = self._card_height < 160
        font_size = int(theme.font("size_card_title_compact" if compact else "size_card_title"))
        self._preview_h = self._preview_height_for(self._card_height)
        self._preview_container = QWidget()
        self._preview_container.setFixedHeight(self._preview_h)
        # 预览容器保持透明，使图片边缘和圆角显示卡片本身的背景。
        self._preview_container.setStyleSheet("background: transparent;")
        preview_container_layout = QVBoxLayout(self._preview_container)
        preview_container_layout.setContentsMargins(0, 0, 0, 0)
        preview_container_layout.setSpacing(0)

        from src.ui.widgets.spinner import ShimmerLabel

        self._thumb = ShimmerLabel()
        self._thumb.setFixedHeight(self._preview_h)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setObjectName("charThumb")
        self._thumb.setScaledContents(False)
        self._cached_pixmap: QPixmap | None = None

        self._thumb.setWordWrap(True)
        self._thumb.setStyleSheet("font-size: 14px; font-weight: bold;")
        preview_container_layout.addWidget(self._thumb)

        # 左上角类型标签使用圆角底色、SVG 图标和文本区分节点类型。
        node_type = self._data.get("node_type", "category")
        self._badge_kind = "character" if node_type == "character" else "category"
        self._type_badge = CardTypeBadge(self._badge_kind, self._card_width, self._preview_container)
        self._badge_icon = self._type_badge.icon_label
        self._badge_text_label = self._type_badge.text_label

        layout.addWidget(self._preview_container)

        # 名称按卡片宽度省略，完整名称保留在提示中。
        name_min_h = font_size + 8
        self._name_label = QLabel()
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setStyleSheet(f"font-weight: bold; font-size: {font_size}px;")
        self._name_label.setWordWrap(False)
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setMinimumHeight(name_min_h)
        self._name_label.setMaximumHeight(name_min_h)
        self._elide_name(self._data.get("name", tr("未知")))
        layout.addWidget(self._name_label)

        # Mod 数量
        count = self._data.get("mod_count", 0)
        enabled = self._data.get("enabled_count", 0)
        info_text = t("{enabled}/{total} 已启用", enabled=enabled, total=count)
        self._info_label = QLabel(info_text)
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_info_style()
        layout.addWidget(self._info_label)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.set_category(self._data, defer_pixmap=self._defer_pixmap)

    def set_category(self, data: dict, *, defer_pixmap: bool = True) -> None:
        """绑定新记录时复用卡片控件树。"""
        self._data = data
        self._selected = False
        self._cut_state = False
        self._cached_pixmap = None
        self._preview_requested = False
        thumbnail = data.get("thumbnail", "")
        self._preview_resolved = not bool(thumbnail)
        self._elide_name(data.get("name", tr("未知")))
        self._info_label.setText(
            t(
                "{enabled}/{total} 已启用",
                enabled=data.get("enabled_count", 0),
                total=data.get("mod_count", 0),
            )
        )
        self._badge_kind = "character" if data.get("node_type") == "character" else "category"
        self.retheme()
        if thumbnail and defer_pixmap:
            self._thumb.start_loading()
        elif thumbnail and fs.exists(thumbnail):
            self.set_preview_pixmap(load_pixmap_file(thumbnail))
        else:
            self.set_no_preview()

    def set_preview_pixmap(self, pixmap: QPixmap, *, rendered: QPixmap | None = None) -> None:
        """保存有效预览并结束加载状态，优先使用已合成图片，否则执行圆角裁剪填充。"""
        if pixmap.isNull():
            self.set_no_preview()
            return
        self._preview_requested = False
        self._preview_resolved = True
        self._cached_pixmap = pixmap
        self._thumb.setPixmap(
            rendered
            if rendered is not None
            else fill_crop_rounded(
                pixmap,
                self._card_width - 8,
                self._preview_h,
                radius=8,
            )
        )

    def update_counts(self, data: dict) -> None:
        """发布计数，不重置选择或预览加载。"""
        self._data = data
        self._info_label.setText(
            t(
                "{enabled}/{total} 已启用",
                enabled=data.get("enabled_count", 0),
                total=data.get("mod_count", 0),
            )
        )

    def scroll_render_key(self) -> tuple | None:
        """组合预览、文字、选择、剪切及节点类型状态，生成可复用的滚动绘制缓存键。"""
        preview = preview_cache_key(self._thumb)
        if preview is None:
            return None
        return (
            preview,
            self._name_label.text(),
            self._info_label.text(),
            self._selected,
            self._cut_state,
            self._badge_kind,
        )

    def set_no_preview(self) -> None:
        """结束预览等待并释放图片，以分类名称作为占位。"""
        self._preview_requested = False
        self._preview_resolved = True
        self._cached_pixmap = None
        self._thumb.setText(self._data.get("name", ""))

    def _show_context_menu(self, pos):
        """同步选择分类后展示菜单并转发操作；按住 Ctrl 或 Shift 时直接粘贴剪贴板预览。"""
        cat_id = self._data.get("id", "")
        global_pos = self.mapToGlobal(pos)
        # 右键时若该卡片尚未被选中，先让 OverviewPage 选中它（加强视觉表现）。
        # 直连槽是同步执行，菜单弹出前选中态/高亮已更新。
        self.context_select_requested.emit(cat_id)
        # 与 Mod 卡片同一套约定：Ctrl/Shift + 右键 = 直接贴剪贴板图当预览图、不弹菜单；
        # 裸右键仍然弹菜单，菜单里也留了粘贴/选择两项。类别是单图，贴上即替换。
        if QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            path = clipboard_image_path()
            if path:
                self.preview_dropped.emit(cat_id, path)
            return
        menu = QMenu(self)
        enter_act = menu.addAction(icons.get_icon("actions/folder-open"), tr("进入"))
        menu.addSeparator()
        self.clipboard_menu_requested.emit(menu)
        menu.addSeparator()
        open_dir_act = menu.addAction(icons.get_icon("actions/folder"), tr("打开源目录"))
        online_act = menu.addAction(icons.get_icon("actions/search", "success"), tr("在线查找"))
        menu.addSeparator()
        paste_act = menu.addAction(icons.get_icon("actions/image"), tr("粘贴预览图"))
        paste_act.setEnabled(clipboard_has_image())
        browse_act = menu.addAction(icons.get_icon("actions/add"), tr("选择预览图"))
        menu.addSeparator()
        edit_act = menu.addAction(icons.get_icon("actions/edit"), t("编辑"))
        export_act = menu.addAction(icons.get_icon("actions/export"), tr("导出"))
        delete_act = menu.addAction(icons.get_icon("actions/remove"), t("删除"))

        # menus：追加插件 character-context 贡献点
        from src.ui import plugin_menu

        plugin_actions = plugin_menu.menu_actions("character-context", cat_id)
        plugin_act_map = {}
        if plugin_actions:
            menu.addSeparator()
            for _title, _cb in plugin_actions:
                _a = menu.addAction(_title)
                plugin_act_map[_a] = _cb

        action = menu.exec(global_pos)
        try:
            menu.deleteLater()
        except RuntimeError:
            # 菜单事件循环内的刷新可能删除当前卡片和菜单。
            return
        if action in plugin_act_map:
            plugin_act_map[action]()
            return
        if action == enter_act:
            self.double_clicked.emit(cat_id)
        elif action == paste_act:
            path = clipboard_image_path()
            if path:
                self.preview_dropped.emit(cat_id, path)
        elif action == browse_act:
            path, _ = QFileDialog.getOpenFileName(
                self,
                tr("选择预览图"),
                "",
                tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)"),
            )
            if path and fs.exists(path):
                self.preview_dropped.emit(cat_id, path)
        elif action == open_dir_act:
            self.open_folder_requested.emit(cat_id)
        elif action == online_act:
            self.online_search_requested.emit(cat_id)
        elif action == edit_act:
            self.edit_requested.emit(cat_id)
        elif action == export_act:
            self.export_requested.emit(cat_id)
        elif action == delete_act:
            self.delete_requested.emit(cat_id)

    def mousePressEvent(self, event):
        """左键点击时发布分类标识，交由概览处理选择。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._data.get("id", ""))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """左键双击时请求进入分类并接收事件，避免默认双击处理再次选中已离开的卡片。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._data.get("id", ""))
            # QWidget 的默认双击处理会再次调用 mousePressEvent，导航后会重新选中旧卡片。
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_selected(self, selected: bool):
        """设置选中状态（批量模式）"""
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    def set_cut_state(self, cut: bool):
        """设置剪切状态视觉反馈"""
        self._cut_state = cut
        self._apply_style()

    def _apply_style(self):
        """根据选中和剪切状态应用样式"""
        if self._cut_state:
            style = (
                f"#charCard {{ border: 2px dashed {theme.color('warning')}; "
                "background-color: rgba(232,168,56,0.08); opacity: 0.6; }"
            )
        elif self._selected:
            # 选中状态仅增加边框，保留卡片基础背景及透明材质。
            style = f"#charCard {{ border: 2px solid {theme.color('accent')}; }}"
        else:
            style = ""
        if self.styleSheet() != style:
            self.setStyleSheet(style)

    def _apply_info_style(self) -> None:
        """按卡片尺寸选择数量说明字号，仅在样式变化时重设，并同步文字行高。"""
        size = int(theme.font("size_caption" if self._card_height < 160 else "size_secondary"))
        style = f"color: {theme.surface_text_color('text.secondary')}; font-size: {size}px;"
        if self._info_label.styleSheet() != style:
            self._info_label.setStyleSheet(style)
        self._info_label.setFixedHeight(size + 6)

    def retheme(self) -> None:
        """刷新活动或回收卡片，保留当前绑定状态。"""
        self._apply_info_style()
        self._type_badge.update_for_card(self._badge_kind, self._card_width)
        self._apply_style()

    def resize_card(self, new_w: int, new_h: int, *, defer_preview: bool = False) -> None:
        """调整卡片尺寸 — 完整更新内部控件"""
        if self._card_width == new_w and self._card_height == new_h:
            return
        self._card_width = new_w
        self._card_height = new_h
        self.setFixedSize(new_w, new_h)

        # 更新预览图容器和缩略图高度（动态比例）
        compact = new_h < 160
        font_size = int(theme.font("size_card_title_compact" if compact else "size_card_title"))
        self._preview_h = self._preview_height_for(new_h)
        self._preview_container.setFixedHeight(self._preview_h)
        self._thumb.setFixedHeight(self._preview_h)

        # 更新名称和信息的字体大小
        name_min_h = font_size + 8
        name_max_h = name_min_h
        self._name_label.setStyleSheet(f"font-weight: bold; font-size: {font_size}px;")
        self._name_label.setMinimumHeight(name_min_h)
        self._name_label.setMaximumHeight(name_max_h)
        self._elide_name(self._data.get("name", tr("未知")))
        self._apply_info_style()
        self._type_badge.update_for_card(self._badge_kind, new_w)

        # 重缩放缓存的预览图
        if not defer_preview and self._cached_pixmap and not self._cached_pixmap.isNull():
            scaled = fill_crop_rounded(
                self._cached_pixmap,
                new_w - 8,
                self._preview_h,
                radius=8,
                smooth=False,
            )
            self._thumb.setPixmap(scaled)
