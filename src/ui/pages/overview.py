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
    logger.exception("拼音词典加载失败，中文名称排序回退为原文比较")
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

_ALPHABETICAL_SORTS = ("alpha_asc", "alpha_desc")


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

    def _elide_name(self, text: str) -> None:
        """根据卡片宽度省略过长的名称"""
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(self._name_label.font())
        avail_w = self._card_width - 16  # 内容边距。
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail_w)
        self._name_label.setText(elided)
        if elided != text:
            self._name_label.setToolTip(text)
        else:
            self._name_label.setToolTip("")

    # 预览图拖放由 OverviewPage 统一处理，避免拖动时逐卡调用 setStyleSheet。


class CharacterDialog(QDialog):
    """新建和编辑分类目录的对话框。"""

    def __init__(
        self,
        char_data: dict | None = None,
        existing_names: list[str] | None = None,
        all_character_names: list[str] | None = None,
        parent=None,
    ):
        """创建顶层所属的分类编辑窗口，准备重名检查集合，编辑时排除自身并回填名称和图片。"""
        # 原生对话框归顶层窗口所有，避免使总览子控件和祖先原生化而影响视频绘制。
        super().__init__(parent.window() if parent is not None else None)
        self._data = char_data
        self._result = None
        self._thumbnail_path = ""
        # 同级已有名称列表，用于类别重名检测。
        self._existing_names = [n.lower() for n in (existing_names or [])]
        # 全局角色/对象名称列表，用于角色重名检测。
        self._all_character_names = [n.lower() for n in (all_character_names or [])]
        # 编辑时排除自身
        if char_data and char_data.get("name"):
            own = char_data["name"].lower()
            self._existing_names = [n for n in self._existing_names if n != own]
            self._all_character_names = [n for n in self._all_character_names if n != own]
        is_edit = char_data is not None
        self._is_edit = is_edit
        self.setWindowTitle(t("编辑类别") if is_edit else t("新建类别"))
        self.setFixedSize(520, 480)
        self.setModal(True)
        self._setup_ui()
        acrylic.apply_dialog_acrylic(self)
        if is_edit and char_data:
            self._name_input.setText(char_data.get("name", ""))
            self._name_en_input.setText(char_data.get("name_en", ""))
            if char_data.get("thumbnail", ""):
                self._thumbnail_path = char_data["thumbnail"]
                self._update_thumbnail_preview()

    def _setup_ui(self):
        """构建分类名称、英文名和图片表单，新建时显示节点类型选择，并连接确认与取消操作。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # 标题区
        header = QLabel(tr("新建类别") if not self._data else tr("编辑类别"))
        header.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {theme.color('text.primary')}; padding-bottom: 4px;"
        )
        layout.addWidget(header)

        # 分割线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {theme.color('stroke.divider')};")
        layout.addWidget(line)

        # 主体内容 - 左右布局
        body = QHBoxLayout()
        body.setSpacing(20)

        # 左侧：角色图片
        thumb_section = QVBoxLayout()
        thumb_section.setSpacing(8)

        thumb_title = QLabel(tr("类别图片"))
        thumb_title.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {theme.color('text.secondary')};")
        thumb_section.addWidget(thumb_title)

        self._thumb_preview = QLabel()
        self._thumb_preview.setFixedSize(140, 180)
        self._thumb_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_preview.setAcceptDrops(True)
        self._thumb_preview.setStyleSheet(
            f"QLabel {{ border: 2px dashed {theme.color('stroke.control')}; border-radius: 8px; "
            f"background-color: {theme.color('bg.card')}; color: {theme.color('text.tertiary')}; font-size: 12px; }}"
        )
        self._thumb_preview.setText(tr("点击选择\n或拖拽图片"))
        self._thumb_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb_preview.mousePressEvent = lambda e: self._on_select_thumbnail()
        # 拖拽图片支持
        self._thumb_preview.dragEnterEvent = self._thumb_drag_enter
        self._thumb_preview.dropEvent = self._thumb_drop
        thumb_section.addWidget(self._thumb_preview, alignment=Qt.AlignmentFlag.AlignCenter)

        thumb_btn_row = QHBoxLayout()
        thumb_btn_row.setSpacing(6)
        select_img_btn = QPushButton(tr("选择"))
        select_img_btn.setFixedWidth(60)
        select_img_btn.clicked.connect(self._on_select_thumbnail)
        thumb_btn_row.addWidget(select_img_btn)

        clear_img_btn = QPushButton(tr("清除"))
        clear_img_btn.setFixedWidth(60)
        clear_img_btn.clicked.connect(self._on_clear_thumbnail)
        thumb_btn_row.addWidget(clear_img_btn)
        thumb_section.addLayout(thumb_btn_row)
        thumb_section.addStretch()
        body.addLayout(thumb_section)

        # 右侧：表单
        form_section = QVBoxLayout()
        form_section.setSpacing(12)

        # 类别名称（中文）
        name_group = QVBoxLayout()
        name_group.setSpacing(4)
        name_label = QLabel(tr("类别名称 *"))
        name_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {theme.color('text.secondary')};")
        name_group.addWidget(name_label)
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText(t("输入类别名称"))
        self._name_input.setMinimumHeight(36)
        name_group.addWidget(self._name_input)
        form_section.addLayout(name_group)

        # 英文名称
        name_en_group = QVBoxLayout()
        name_en_group.setSpacing(4)
        name_en_label = QLabel(tr("英文名称"))
        name_en_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {theme.color('text.secondary')};")
        name_en_group.addWidget(name_en_label)
        self._name_en_input = QLineEdit()
        self._name_en_input.setPlaceholderText(t("用于Mod匹配和搜索。"))
        self._name_en_input.setMinimumHeight(36)
        name_en_group.addWidget(self._name_en_input)
        form_section.addLayout(name_en_group)

        # 节点类型选择仅在新建时显示，编辑时隐藏。
        self._type_section = QWidget()
        type_group = QVBoxLayout(self._type_section)
        type_group.setContentsMargins(0, 0, 0, 0)
        type_group.setSpacing(4)
        type_label = QLabel(tr("节点类型"))
        type_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {theme.color('text.secondary')};")
        type_group.addWidget(type_label)

        self._type_combo = QComboBox()
        self._type_combo.addItem(icons.get_icon("meta/category"), tr("类别（可同时启用多个Mod）"), "category")
        self._type_combo.addItem(icons.get_icon("meta/character"), tr("角色/对象（同时只能启用一个Mod）"), "character")
        if self._data and self._data.get("node_type") == "character":
            self._type_combo.setCurrentIndex(1)
        type_group.addWidget(self._type_combo)

        type_hint = QLabel(tr("角色/对象类型会限制其下所有子类别中同时只能启用一个Mod。"))
        type_hint.setStyleSheet(f"color: {theme.color('warning')}; font-size: 10px;")
        type_hint.setWordWrap(True)
        type_group.addWidget(type_hint)
        form_section.addWidget(self._type_section)
        # 编辑模式下完全隐藏节点类型选择
        if self._is_edit:
            self._type_section.setVisible(False)

        form_section.addStretch()
        body.addLayout(form_section, 1)
        layout.addLayout(body, 1)

        # 底部按钮区
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet(f"color: {theme.color('stroke.divider')};")
        layout.addWidget(line2)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        cancel_btn = QPushButton(t("取消"))
        cancel_btn.setFixedSize(90, 36)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton(tr("确认") if not self._data else tr("保存"))
        ok_btn.setFixedSize(90, 36)
        ok_btn.setDefault(True)
        ok_btn.setProperty("accent", True)
        ok_btn.clicked.connect(self._on_ok)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _on_select_thumbnail(self):
        """选择本地类别图片并更新预览，取消文件选择时保留原值。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("选择类别图片"),
            "",
            tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)"),
        )
        if path:
            self._thumbnail_path = path
            self._update_thumbnail_preview()

    def _thumb_drag_enter(self, event):
        """缩略图拖拽进入 - 仅检查格式可用性，不读取数据避免 COM IPC 卡顿"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def _thumb_drop(self, event):
        """缩略图拖拽放下"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    p = Path(url.toLocalFile())
                    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                        self._thumbnail_path = str(p)
                        self._update_thumbnail_preview()
                        event.acceptProposedAction()
                        return
        event.ignore()

    def _on_clear_thumbnail(self):
        """清除待保存的类别图片路径，并恢复图片选择提示。"""
        self._thumbnail_path = ""
        self._thumb_preview.clear()
        self._thumb_preview.setText(tr("点击选择\n或拖拽图片"))

    def _update_thumbnail_preview(self):
        """读取仍存在的有效图片，按比例缩放到预览区域并显示已选择样式。"""
        if self._thumbnail_path and fs.exists(self._thumbnail_path):
            pixmap = load_pixmap_file(self._thumbnail_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self._thumb_preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._thumb_preview.setPixmap(scaled)
                self._thumb_preview.setStyleSheet(
                    f"QLabel {{ border: 2px solid {theme.color('accent')}; border-radius: 8px; "
                    f"background-color: {theme.color('bg.card')}; }}"
                )

    def _on_ok(self):
        """校验必填名称、同级重名和角色全局重名，成功后保存结果并接受对话框。"""
        name = self._name_input.text().strip()
        if not name:
            self._name_input.setStyleSheet(
                f"QLineEdit {{ padding: 8px 12px; border: 1px solid {theme.color('danger')}; border-radius: 6px; "
                f"background-color: {theme.color('bg.card')}; color: {theme.color('text.primary')}; font-size: 13px; }}"
            )
            return
        # 同级重名检测
        if name.lower() in self._existing_names:
            self._name_input.setStyleSheet(
                f"QLineEdit {{ padding: 8px 12px; border: 1px solid {theme.color('danger')}; border-radius: 6px; "
                f"background-color: {theme.color('bg.card')}; color: {theme.color('text.primary')}; font-size: 13px; }}"
            )
            QMessageBox.warning(
                self, tr("名称重复"), tr('同级目录下已存在名为 "{name}" 的类别或角色/对象，请使用其他名称。', name=name)
            )
            return
        # 角色或对象名称在当前游戏范围内检查重名。
        selected_type = self._type_combo.currentData()
        if selected_type == "character" and name.lower() in self._all_character_names:
            self._name_input.setStyleSheet(
                f"QLineEdit {{ padding: 8px 12px; border: 1px solid {theme.color('danger')}; border-radius: 6px; "
                f"background-color: {theme.color('bg.card')}; color: {theme.color('text.primary')}; font-size: 13px; }}"
            )
            QMessageBox.warning(
                self, tr("角色名称重复"), tr('当前游戏中已存在名为 "{name}" 的角色/对象，请使用其他名称。', name=name)
            )
            return
        self._result = {
            "name": name,
            "name_en": self._name_en_input.text().strip(),
            "thumbnail": self._thumbnail_path,
            "node_type": self._type_combo.currentData(),
        }
        self.accept()

    def get_result(self):
        """返回确认后的类别字段，尚未确认时返回空值。"""
        return self._result


class _DragDropImageLabel(QLabel):
    """支持拖拽图片的Label"""

    image_dropped = None  # 由外部设置回调

    def __init__(self, text="", parent=None):
        """创建允许接收文件拖放的图片标签，初始不绑定图片处理回调。"""
        super().__init__(text, parent)
        self.setAcceptDrops(True)
        self._on_image_dropped = None  # callback(path)

    def set_drop_callback(self, callback):
        """设置图片拖入后的回调"""
        self._on_image_dropped = callback

    def dragEnterEvent(self, event):
        """仅检查 URL 拖放格式并显示目标样式，避免进入阶段读取外部文件数据。"""
        # 仅检查格式可用性，不读取实际数据，避免 COM IPC 卡顿
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(
                f"QLabel {{ border: 2px dashed {theme.color('accent')}; border-radius: 4px; "
                f"background-color: {theme.color('bg.subtle')}; color: {theme.color('accent')}; font-size: 9px; }}"
            )
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        """拖放离开图片标签时恢复默认虚线边框和文字颜色。"""
        self.setStyleSheet(
            f"QLabel {{ border: 1px dashed {theme.color('stroke.control')}; border-radius: 4px; "
            f"background-color: {theme.color('bg.subtle')}; color: {theme.color('text.tertiary')}; font-size: 9px; }}"
        )

    def dropEvent(self, event):
        """选取首个具有支持扩展名的本地拖入路径，交给图片回调处理。"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    p = Path(url.toLocalFile())
                    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                        event.acceptProposedAction()
                        if self._on_image_dropped:
                            self._on_image_dropped(str(p))
                        return
        event.ignore()


class BatchAddCharacterDialog(QDialog):
    """批量添加类别对话框 - 新建类别的批量版，每个条目有完整表单"""

    def __init__(self, parent=None):
        """创建批量类别编辑窗口，建立条目与结果集合，并初始添加一条空记录。"""
        super().__init__(parent)
        self._entries: list[dict] = []
        self._results: list[dict] = []
        self._built_theme = theme.current_theme()
        self.setWindowTitle(tr("批量添加类别"))
        self.setFixedSize(600, 580)
        self.setModal(True)
        self._setup_ui()
        # 默认添加一个空条目
        self._add_entry()
        acrylic.apply_dialog_acrylic(self)

    def prepare_for_open(self) -> None:
        """保留已准备的窗口与表单，替换旧编辑草稿。"""
        self._results = []
        self.setResult(QDialog.DialogCode.Rejected)
        for entry in self._entries[1:]:
            self._entries_layout.removeWidget(entry["frame"])
            entry["frame"].hide()
            entry["frame"].deleteLater()
        self._entries = self._entries[:1]
        entry = self._entries[0]
        entry["name_input"].clear()
        entry["name_en_input"].clear()
        entry["type_combo"].setCurrentIndex(0)
        self._clear_thumbnail(entry)
        self._renumber()
        self._entries_scroll.verticalScrollBar().setValue(0)
        self._entries_scroll.horizontalScrollBar().setValue(0)
        entry["name_input"].setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _clear_thumbnail(entry: dict) -> None:
        """清空指定条目的图片路径，并恢复图片选择占位和默认边框。"""
        entry["thumbnail_path"] = ""
        preview = entry["thumb_preview"]
        preview.setText(tr("选择\n图片"))
        preview.setStyleSheet(
            f"QLabel {{ border: 1px dashed {theme.color('stroke.control')}; border-radius: 4px; "
            f"background-color: {theme.color('bg.subtle')}; color: {theme.color('text.tertiary')}; font-size: 9px; }}"
        )

    def _setup_ui(self):
        """构建可滚动的批量类别条目区、数量提示及添加、确认和取消操作。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # 标题
        header = QLabel(tr("批量添加类别"))
        header.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {theme.color('text.primary')};")
        layout.addWidget(header)

        subtitle = QLabel(tr("逐个填写类别信息，每个类别可设置名称、英文名和图片。点击「+」添加更多。"))
        subtitle.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 11px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # 分割线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {theme.color('stroke.divider')};")
        layout.addWidget(line)

        # 条目列表（滚动）
        scroll = QScrollArea()
        self._entries_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        self._scroll_content = QWidget()
        self._entries_layout = QVBoxLayout(self._scroll_content)
        self._entries_layout.setContentsMargins(0, 0, 0, 0)
        self._entries_layout.setSpacing(8)
        self._entries_layout.addStretch()
        scroll.setWidget(self._scroll_content)
        layout.addWidget(scroll, 1)

        # 添加按钮
        add_row = QHBoxLayout()
        add_btn = QPushButton(tr("＋ 添加类别"))
        add_btn.setStyleSheet(
            f"QPushButton "
            f"{{ background-color: transparent; "
            f"color: {theme.color('accent')}; "
            f"border: 1px dashed {theme.color('accent')}; "
            f"border-radius: 6px; padding: 8px 16px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ background-color: rgba(30, 136, 229, 0.1); }}"
        )
        add_btn.clicked.connect(self._add_entry)
        add_row.addWidget(add_btn)
        add_row.addStretch()

        self._entry_count_label = QLabel(tr("共 1 个类别"))
        self._entry_count_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 11px;")
        add_row.addWidget(self._entry_count_label)
        layout.addLayout(add_row)

        # 底部按钮
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet(f"color: {theme.color('stroke.divider')};")
        layout.addWidget(line2)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        cancel_btn = QPushButton(tr("取消"))
        cancel_btn.setFixedSize(90, 36)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton(tr("全部添加"))
        ok_btn.setFixedSize(110, 36)
        ok_btn.setDefault(True)
        ok_btn.setProperty("accent", True)
        ok_btn.clicked.connect(self._on_ok)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _add_entry(self):
        """添加一个角色条目"""
        entry_frame = QFrame()
        entry_frame.setStyleSheet(
            f"QFrame#batchEntry {{ background-color: {theme.color('bg.card')}; border-radius: 8px; "
            f"border: 1px solid {theme.color('stroke.divider')}; }}"
        )
        entry_frame.setObjectName("batchEntry")
        entry_layout = QHBoxLayout(entry_frame)
        entry_layout.setContentsMargins(12, 10, 12, 10)
        entry_layout.setSpacing(12)

        # 序号
        idx = len(self._entries) + 1
        num_label = QLabel(f"#{idx}")
        num_label.setFixedWidth(28)
        num_label.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {theme.color('accent')};")
        entry_layout.addWidget(num_label)

        # 缩略图 - 支持拖拽图片
        thumb_preview = _DragDropImageLabel(tr("选择\n图片"))
        thumb_preview.setFixedSize(60, 75)
        thumb_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        entry_layout.addWidget(thumb_preview)

        # 表单区
        form_layout = QVBoxLayout()
        form_layout.setSpacing(6)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_input = QLineEdit()
        name_input.setPlaceholderText(tr("类别名称 *"))
        name_row.addWidget(name_input, 2)

        name_en_input = QLineEdit()
        name_en_input.setPlaceholderText(tr("英文名（可选）"))
        name_row.addWidget(name_en_input, 2)

        # 节点类型选择（紧凑内联）
        type_combo = QComboBox()
        type_combo.addItem(icons.get_icon("meta/category"), tr("类别"), "category")
        type_combo.addItem(icons.get_icon("meta/character"), tr("角色/对象"), "character")
        type_combo.setToolTip(tr("类别：允许同时启用多个Mod\n角色/对象：同一时间只能启用一个Mod"))
        # 收起态须容下「角色/对象」+ 图标 + 下拉箭头，否则文字被截断。
        type_combo.setMinimumWidth(132)
        type_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        name_row.addWidget(type_combo)

        form_layout.addLayout(name_row)

        entry_layout.addLayout(form_layout, 1)

        # 删除按钮
        remove_btn = QPushButton()
        icons.apply_icon(remove_btn, "actions/dismiss", "text.tertiary")
        remove_btn.setIconSize(QSize(16, 16))
        remove_btn.setFixedSize(28, 28)
        remove_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; border: none; border-radius: 6px; }}"
            f"QPushButton:hover {{ background-color: {theme.rgba('danger', 0.14)}; }}"
        )
        entry_layout.addWidget(remove_btn, alignment=Qt.AlignmentFlag.AlignTop)

        entry = {
            "frame": entry_frame,
            "name_input": name_input,
            "name_en_input": name_en_input,
            "type_combo": type_combo,
            "thumb_preview": thumb_preview,
            "thumbnail_path": "",
            "num_label": num_label,
        }
        self._clear_thumbnail(entry)

        # 点击缩略图选择图片
        def select_thumb(e, ent=entry):
            """为回调绑定的类别条目选择图片，成功选择后应用该条目的预览。"""
            path, _ = QFileDialog.getOpenFileName(
                self,
                tr("选择类别图片"),
                "",
                tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)"),
            )
            if path:
                _apply_thumb(path, ent)

        def _apply_thumb(path, ent):
            """将图片应用到缩略图"""
            ent["thumbnail_path"] = path
            pixmap = load_pixmap_file(path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    ent["thumb_preview"].size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                ent["thumb_preview"].setPixmap(scaled)
                ent["thumb_preview"].setStyleSheet(
                    f"QLabel {{ border: 1px solid {theme.color('accent')}; border-radius: 4px; "
                    f"background-color: {theme.color('bg.subtle')}; }}"
                )

        thumb_preview.mousePressEvent = select_thumb
        # 拖拽图片支持
        thumb_preview.set_drop_callback(lambda path, ent=entry: _apply_thumb(path, ent))

        # 删除条目
        def remove_entry(e=entry):
            """删除指定类别编辑行并重新编号，始终保留至少一条可编辑记录。"""
            if len(self._entries) <= 1:
                return  # 至少保留一个
            self._entries.remove(e)
            e["frame"].deleteLater()
            self._renumber()

        remove_btn.clicked.connect(lambda: remove_entry())

        self._entries.append(entry)
        self._entries_layout.insertWidget(self._entries_layout.count() - 1, entry_frame)
        self._renumber()

    def _renumber(self):
        """重新编号"""
        for i, e in enumerate(self._entries):
            e["num_label"].setText(f"#{i + 1}")
        self._entry_count_label.setText(tr("共 {count} 个类别", count=len(self._entries)))

    def _on_ok(self):
        """收集名称非空的类别条目，存在可提交条目时保存结果并接受对话框。"""
        self._results = []
        for entry in self._entries:
            name = entry["name_input"].text().strip()
            if not name:
                continue  # 跳过空条目
            self._results.append(
                {
                    "name": name,
                    "name_en": entry["name_en_input"].text().strip(),
                    "thumbnail": entry.get("thumbnail_path", ""),
                    "node_type": entry["type_combo"].currentData(),
                }
            )
        if self._results:
            self.accept()

    def get_results(self) -> list[dict]:
        """返回已收集的批量类别字段，尚未确认时为空列表。"""
        return self._results


class _PixmapLoadWorker(QThread):
    """后台线程：从原生路径解码显示用 QImage，避免阻塞主线程。

    使用 Python queue.Queue 做线程安全的任务队列，主线程只需 enqueue/clear，
    工作线程持续从队列取任务并 emit 已解码的 QImage。缓存尺寸覆盖卡片允许的
    200% 最大缩放，既避免保存整张 2K/4K 位图，也不会在放大卡片时失真。
    """

    _MAX_CACHE_SIZE = QSize(
        ModCard.CARD_WIDTH * 2 - 8,
        int(ModCard.CARD_HEIGHT * 2 * 0.64),
    )

    image_loaded = Signal(str, str, int, object)  # item_id, path, request_id, DecodedPreview

    def __init__(self, parent=None):
        """创建卡片预览工作队列，初始化允许处理任务的运行标记。"""
        super().__init__(parent)
        import queue as _queue

        self._queue: _queue.Queue = _queue.Queue()
        self._running = True

    def enqueue(
        self,
        item_id: str,
        path: str,
        width: int,
        height: int,
        request_id: int = 0,
        *,
        mode: str = "contain",
        dpr: float = 1.0,
    ) -> None:
        """主线程调用：添加加载任务"""
        if self._running:
            layout = PreviewLayout(width, height, mode, dpr)
            self._queue.put((item_id, path, layout, request_id, preview_cache_token()))

    @classmethod
    def cache_size(cls, layout: PreviewLayout) -> QSize:
        """按布局物理像素尺寸和缓存基准尺寸的较大值选择解码尺寸。"""
        return QSize(
            max(round(layout.width * layout.dpr), cls._MAX_CACHE_SIZE.width()),
            max(round(layout.height * layout.dpr), cls._MAX_CACHE_SIZE.height()),
        )

    def clear_queue(self) -> None:
        """主线程调用：数据全量刷新时清空待处理任务。"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break

    def stop(self) -> None:
        """停止工作线程"""
        self._running = False
        self.clear_queue()
        self._queue.put(None)  # 哨兵值唤醒 get

    def run(self) -> None:
        """按队列解码并准备卡片预览，持续检查运行状态，携带请求编号发布仍有效的结果。"""
        while self._running:
            try:
                task = self._queue.get(timeout=0.05)
            except Exception:
                continue
            if task is None:  # 哨兵 → 退出
                break
            item_id, path, layout, request_id, token = task
            preview = None
            try:
                # 由 QImageReader 直接读取原生路径，并在解码时限制显示缓存尺寸。
                preview = load_preview(
                    path,
                    self.cache_size(layout),
                    load_scaled_qimage_file,
                    cache_token=token,
                    cancelled=lambda: not self._running,
                )
                if self._running:
                    preview = prepare_preview(preview, layout, cancelled=lambda: not self._running)
            except Exception:
                preview = None
            if self._running:
                self.image_loaded.emit(item_id, path, request_id, preview)
            preview = None  # 空闲线程不保留上一张图片。


class _PanelPreviewLoadWorker(QThread):
    """侧栏首图专用线程；新选择会丢弃尚未开始的旧请求。"""

    image_loaded = Signal(int, str, str, object)

    def __init__(self, parent=None):
        """创建详情预览解码队列并初始化运行状态。"""
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue()
        self._running = True

    def enqueue(
        self,
        request_id: int,
        mod_id: str,
        path: str,
        target_w: int,
        target_h: int,
    ) -> None:
        """清除尚未开始的详情图片请求，仅排入最新目标及其请求编号。"""
        self.clear_queue()
        self._queue.put((request_id, mod_id, path, target_w, target_h))

    def clear_queue(self) -> None:
        """丢弃尚未开始的详情图片解码任务，保留正在执行的任务。"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """标记详情图片工作线程停止，清空队列并用哨兵唤醒阻塞读取。"""
        self._running = False
        self.clear_queue()
        self._queue.put(None)

    def run(self) -> None:
        """后台解码目标尺寸的详情图片，携带请求身份返回结果，由接收方校验是否过期。"""
        while self._running:
            task = self._queue.get()
            if task is None:
                break
            request_id, mod_id, path, target_w, target_h = task
            image = None
            try:
                target_size = QSize(max(1, target_w), max(1, target_h))
                image = load_scaled_qimage_file(
                    path,
                    target_size,
                    auto_transform=True,
                )
            except Exception:
                image = None
            self.image_loaded.emit(request_id, mod_id, path, image)


class _KeyBindingsLoadWorker(QThread):
    """在后台解析有效 INI；主线程只负责创建最终控件。"""

    bindings_loaded = Signal(int, str, object)

    def __init__(self, parent=None):
        """创建按键配置解析队列并初始化运行状态。"""
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue()
        self._running = True

    def enqueue(self, request_id: int, mod: Mod, manager) -> None:
        """清除等待中的旧解析任务，登记最新 Mod、管理器与请求编号。"""
        self.clear_queue()
        self._queue.put((request_id, mod, manager))

    def clear_queue(self) -> None:
        """清空尚未开始的按键解析请求，不中断当前解析。"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """标记解析线程停止，并以哨兵唤醒等待中的队列读取。"""
        self._running = False
        self.clear_queue()
        self._queue.put(None)

    def run(self) -> None:
        """后台解析 Mod 按键配置，失败时返回空列表，结果携带请求编号供界面校验。"""
        while self._running:
            task = self._queue.get()
            if task is None:
                break
            request_id, mod, manager = task
            try:
                bindings = manager.parse_mod_key_bindings(mod)
            except Exception:
                bindings = []
            self.bindings_loaded.emit(request_id, mod.mod_id, bindings)


class _EnabledRowDelegate(QStyledItemDelegate):
    """在列表第 0 列左边缘绘制 success 色圆角竖条，标识已启用的 Mod。

    竖条与状态列的对钩和胶囊共同提示启用状态。竖条宽 3px，位于 x∈[0,3)，
    与约 4px 起始的类型图标分离；在 super.paint 后叠加，不占列宽并保留选中高亮。
    启用状态读取第 0 列的 ``UserRole + 3``，未设置该值的类别行不绘制标记。
    """

    ROLE_ENABLED = ModListTable.ROLE_ENABLED
    BAR_W = 3.0

    def paint(self, painter, option, index):
        """在已启用条目的首列左侧绘制状态色圆角标记，其他单元格保留默认绘制。"""
        super().paint(painter, option, index)
        if index.column() != 0 or not bool(index.data(self.ROLE_ENABLED)):
            return
        rect = option.rect
        h = max(8.0, rect.height() - 8.0)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.color("success")))
        painter.drawRoundedRect(
            QRectF(float(rect.left()), rect.top() + (rect.height() - h) / 2.0, self.BAR_W, h),
            self.BAR_W / 2.0,
            self.BAR_W / 2.0,
        )
        painter.restore()


class OverviewPage(QWidget):
    """类别总览页面 - 混合类别+Mod视图"""

    # 首屏已缓存的池卡片在有界批次内完整绑定；新建、未缓存和离屏缓冲仍按短预算让步。
    _VS_INITIAL_BATCH_INTERVAL_MS = 0
    _VS_INITIAL_BUFFER_ROWS = 1
    _VS_INITIAL_REUSE_BUDGET_S = 0.064
    _VS_INITIAL_MAX_BINDINGS = 96
    _VS_SCROLL_BUFFER_ROWS = 1
    _VS_SCROLL_CREATION_BUDGET_S = 0.008
    _VS_PIXMAP_BATCH_SIZE = 4
    _VS_PIXMAP_INTERVAL_MS = 32
    _VS_MAX_IMAGE_REQUESTS = 8
    _VS_MAX_PREFETCH_REQUESTS = 2

    character_selected = Signal(str)
    settings_requested = Signal()
    category_preset_import_requested = Signal()
    character_add_requested = Signal(dict)
    character_edit_requested = Signal(str, dict)
    character_delete_requested = Signal(str)
    character_batch_add_requested = Signal(list)  # list[dict]
    character_batch_delete_requested = Signal(list)  # list[str]
    game_changed = Signal(str)
    check_updates_requested = Signal()  # 工具条 🔄 检查更新
    # 预设信号
    preset_apply_requested = Signal(str)  # preset_id
    preset_rename_requested = Signal(str, str)  # preset_id, new_name
    preset_create_requested = Signal()  # 默认名由 MainWindow 生成
    preset_delete_requested = Signal(list)  # preset_ids（空=删最后一项）
    preset_duplicate_requested = Signal(list)  # preset_ids
    # Mod 操作信号
    mod_toggle_requested = Signal(str)
    mod_delete_requested = Signal(str)
    mod_batch_delete_requested = Signal(list)  # list[str]
    mod_import_requested = Signal()
    mod_edit_requested = Signal(str)  # mod_id
    files_dropped_for_import = Signal(list, str)  # paths, node_id
    # 导航信号
    category_navigated = Signal(str)  # parent_id
    # 剪切/粘贴
    items_move_requested = Signal(list, list, str)  # cat_ids, mod_ids, target_parent
    # 预览图操作信号（从面板转发）
    preview_image_dropped = Signal(str, str)  # mod_id/cat_id, image_path
    category_preview_dropped = Signal(str, str)  # cat_id, image_path
    mod_preview_delete_requested = Signal(str, str)  # mod_id, image_path
    mod_preview_delete_all_requested = Signal(str)  # mod_id
    # 载荷是**图片路径**而非索引：顺序现在是实时落库的，DB 里的排列会随时变，
    # 而预览栏内部的图片列表保持加载时的顺序不动（否则翻页会在用户手下跳序）。
    # 两边索引因此对不上，只有路径是稳定标识。
    mod_preview_reorder_requested = Signal(str, str)  # mod_id, 设为主图的图片路径
    adult_mode_changed = Signal(str)  # 同步到其他页面（"show"|"hide"|"blur"）
    batch_disable_requested = Signal(list)  # 待禁用的 Mod 标识列表。
    items_export_requested = Signal(list, list)  # cat_ids, mod_ids —— 类别由 MainWindow 展开成子树 Mod 后导出
    variant_context_changed = Signal(str)  # 未选择单个 Mod 时为空。

    def __init__(self, parent: QWidget | None = None):
        """初始化分类导航、筛选、多选和剪贴板，以及虚拟卡片复用和独立的图片、详情与按键解析线程。
        从配置恢复视图和缩放，建立防抖调度、预览排序持久化及请求代次，随后构建页面。
        """
        super().__init__(parent)
        self._characters: list[dict] = []
        self._all_characters: list[dict] = []  # 所有类别（不分层级）
        self._children_map: dict[str, list[dict]] = {}  # parent_id -> 直接子类别
        self._current_parent_id: str = ""  # 当前导航层级
        self._current_game_id: str = ""
        self._batch_add_dialog: BatchAddCharacterDialog | None = None
        self._presented_view: _PresentedOverviewView | None = None
        self._view_positions: dict[tuple[str, str, str], tuple[tuple, int]] = {}
        self._pending_view: tuple[int, str, str, str] | None = None
        self._view_request_generation = 0
        self._applying_position = False
        self._tag_catalog_by_name: dict[str, TagDefinition] = {}
        self._detail_mod_id = ""
        self._mod_detail_tab = 0
        self._last_detail_tab = 0
        self._detail_scroll_positions: dict[tuple[str, int], int] = {}
        # 视图模式（grid/list）：从配置恢复上次的选择（视图模式记忆）。
        from src.core.config import Config as _Cfg

        _vm = _Cfg.instance().get("ui.overview_view_mode", "grid")
        self._view_mode = _vm if _vm in ("grid", "list") else "grid"
        # 卡片缩放百分比（50~200）：从配置恢复上次的值（持久化缩放）
        self._card_scale = max(50, min(200, int(_Cfg.instance().get("ui.overview_card_scale", 100))))
        # 当前层级下的Mod列表
        self._current_mods: list[Mod] = []
        # 当前层级（含子孙层级）所有 Mod，用于递归搜索
        self._subtree_mods: list[Mod] = []
        self._request_mods = None
        self._enabled_mod_ids_loader = None
        self._mods_loading = False
        self._loading_table_state = None
        # 混合显示项列表 [{"id":..., "type":"category"|"mod", ...}]
        self._display_items: list[dict] = []
        self._display_idx_map: dict[str, int] = {}  # item_id → index（O(1) 查找）
        # 多选
        self._selected_ids: set[str] = set()
        self._last_clicked_index: int = -1
        # 剪切板
        self._clipboard_cat_ids: list[str] = []
        self._clipboard_mod_ids: list[str] = []
        self._clipboard_source_parent: str = ""
        # 当前选中的Mod（用于右侧面板）
        self._selected_mod: Mod | None = None
        self._mod_manager = None  # 主窗口注入（parse_mod_key_bindings/edit_mod_key/部署用）
        self._adult_mode = "show"
        self._sort_key = "alpha_asc"  # 排序方式
        self._category_sort_key = "alpha_asc"
        self._collator = QCollator()
        self._collator.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
        self._collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._lazy_pinyin = _lazy_pinyin
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(80)
        self._search_timer.timeout.connect(self._execute_search_refresh)
        self._search_token = 0
        self._pending_search_text = ""
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(50)
        self._resize_timer.timeout.connect(self._deferred_refresh)
        # 卡片大小调整结束后再缩放 pixmap，避免拖动时重复计算。
        self._card_scale_active = False
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(150)
        self._scale_timer.timeout.connect(self._apply_card_scale_pixmaps)
        self._last_cards_per_row: int = -1
        # 虚拟滚动。
        # 只为可视区域 + 缓冲区的条目创建卡片 Widget，其余只保留数据。
        # 滚动时动态创建 / 回收 Widget，大幅降低 Widget 数量和布局开销。
        self._vs_card_cache: dict[str, QFrame] = {}  # item_id → 卡片 Widget
        self._vs_card_pool = CardPool()
        self._directory_intro_pending = False
        self._vs_window: tuple | None = None
        self._vs_population_pending = False
        self._vs_image_request_id = 0
        self._vs_image_requests: dict[str, tuple[int, QFrame | None, str]] = {}
        self._vs_pending_prefetch: list[tuple[str, str]] = []
        self._vs_prefetch_key: tuple | None = None
        self._vs_waiting_prefetch: dict[str, tuple[int, str, tuple]] = {}
        # 部署环形进度：正在后台部署的 mod_id → 最新进度 frac；卡片存在时挂 DeployOverlay。
        # 只保留进度数值，浮层随卡片销毁；重建卡片时恢复浮层，避免悬空控件引用。
        self._deploying_frac: dict[str, float] = {}  # Mod 标识到当前部署进度。
        self._deploy_overlays: dict[str, DeployOverlay] = {}  # mod_id → 蒙层(随卡片析构)
        self._updatable_ids: set[str] = set()
        self._vs_card_w: int = CharacterCard.CARD_WIDTH
        self._vs_card_h: int = CharacterCard.CARD_HEIGHT
        self._vs_row_h: int = CharacterCard.CARD_HEIGHT + 12
        self._vs_cols: int = 1
        self._vs_total_rows: int = 0
        self._vs_scroll_timer = QTimer(self)
        self._vs_scroll_timer.setSingleShot(True)
        self._vs_scroll_timer.setInterval(8)  # 合并高频滚动事件。
        self._vs_scroll_timer.timeout.connect(self._vs_update_visible)
        # 大目录首屏按帧渐进创建，避免全屏下一次构造几十张复杂卡片而冻结双击反馈。
        self._vs_population_timer = QTimer(self)
        self._vs_population_timer.setInterval(self._VS_INITIAL_BATCH_INTERVAL_MS)
        self._vs_population_timer.timeout.connect(self._vs_update_visible)
        self._vs_population_anchor_value = 0
        # 滚动期间合并批次仍持续推进。
        self._vs_pixmap_timer = QTimer(self)
        self._vs_pixmap_timer.setSingleShot(True)
        self._vs_pixmap_timer.setInterval(self._VS_PIXMAP_INTERVAL_MS)
        self._vs_pixmap_timer.timeout.connect(self._vs_load_deferred_pixmaps)
        self._vs_pending_pixmap_cards: list[tuple[str, QFrame]] = []  # 待加载pixmap的卡片
        # 由工作线程读取和解码图片。
        self._pixmap_worker = _PixmapLoadWorker(self)
        manage_page_worker(self, self._pixmap_worker)
        self._pixmap_worker.image_loaded.connect(self._on_bg_pixmap_loaded)
        self._pixmap_worker.start()
        self._panel_preview_request_id = 0
        self._panel_preview_worker = _PanelPreviewLoadWorker(self)
        manage_page_worker(self, self._panel_preview_worker)
        self._panel_preview_worker.image_loaded.connect(self._on_panel_preview_loaded)
        self._panel_preview_worker.start()
        self._key_bindings_request_id = 0
        self._keys_panel_mod_id = None
        self._keys_panel_signature = None
        self._key_bindings_worker = _KeyBindingsLoadWorker(self)
        manage_page_worker(self, self._key_bindings_worker)
        self._key_bindings_worker.bindings_loaded.connect(self._on_key_bindings_loaded)
        self._key_bindings_worker.start()
        # 连续翻页合并为一次主图排序写入，减少数据库操作；离开 Mod 或退出前显式提交待处理顺序。
        self._pending_reorder_mod_id: str = ""
        self._pending_reorder_path: str = ""
        self._preview_reorder_timer = QTimer(self)
        self._preview_reorder_timer.setSingleShot(True)
        self._preview_reorder_timer.setInterval(200)
        self._preview_reorder_timer.timeout.connect(self._flush_pending_preview_reorder)
        # 保存当前选中的预览路径，供卡片和列表切换后重建时复用。
        # 预览排序持久化后与数据库保持一致。
        self._preview_override_paths: dict[str, str] = {}
        # 拖拽路径收集器（后台线程）
        self._drop_collector = None
        # 拖拽悬停视觉反馈：当前高亮的卡片
        self._drag_hover_card: CharacterCard | None = None
        self._overview_col_ratios = (0.12, 0.33, 0.22, 0.13, 0.20)
        self._overview_col_min_widths = (78, 160, 115, 96, 110)
        self._overview_syncing_widths = False
        self._overview_header_dragging = False
        self._overview_user_resized = False
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._setup_ui()
        # 启动时预览栏进入未选中状态（隐藏按钮、清空内容）
        self._hide_panel()
        # 恢复上次的视图模式：grid 是默认布局无需动作；list 需主动切到表格视图。
        if self._view_mode == "list":
            self._set_view("list")
            # 首帧布局后按真实视口宽度重新分配列宽，避免初始化尺寸造成右侧留白。
            call_later(0, self, lambda: self._apply_overview_column_widths(force_ratio=True))

    def _setup_ui(self):
        """组装预设与筛选工具栏、混合卡片和列表视图，以及预览、信息、按键与变体详情面板。
        连接导航、选择和业务请求，恢复分栏尺寸，并建立加载与空状态入口。
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # 顶部工具栏
        toolbar = QHBoxLayout()
        title = QLabel(t("类别总览"))
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        toolbar.addWidget(title)
        toolbar.addStretch()

        # 游戏切换由游戏页的卡片和详情栏处理。

        # 预设选择器使用自定义下拉浮层。
        from src.ui.widgets.preset_selector import PresetSelector

        self._preset_selector = PresetSelector()
        self._preset_selector.apply_requested.connect(self.preset_apply_requested.emit)
        self._preset_selector.rename_requested.connect(self.preset_rename_requested.emit)
        self._preset_selector.create_requested.connect(self.preset_create_requested.emit)
        self._preset_selector.delete_requested.connect(self.preset_delete_requested.emit)
        self._preset_selector.duplicate_requested.connect(self.preset_duplicate_requested.emit)
        toolbar.addWidget(self._preset_selector)

        # 批量添加类别
        batch_add_btn = QPushButton(tr("批量添加"))
        icons.apply_icon(batch_add_btn, "actions/new-folder")
        batch_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        batch_add_btn.clicked.connect(self._on_batch_add)
        toolbar.addWidget(batch_add_btn)

        # 视图切换（下拉框 + SVG 图标）
        self._view_combo = QComboBox()
        self._view_combo.addItem(icons.get_icon("actions/card-view"), tr("卡片视图"), "grid")
        self._view_combo.addItem(icons.get_icon("actions/list-view"), tr("列表视图"), "list")
        icons.apply_combo_item_icon(self._view_combo, 0, "actions/card-view")
        icons.apply_combo_item_icon(self._view_combo, 1, "actions/list-view")
        self._view_combo.setToolTip(tr("切换 卡片 / 列表 视图"))
        self._view_combo.currentIndexChanged.connect(lambda _i: self._set_view(self._view_combo.currentData()))
        toolbar.addWidget(self._view_combo)

        # 成人级 Mod 预览模式与 Mod 商店保持一致，眼睛图标仅显示在下拉框当前值前方。
        self._adult_mode_combo = LeadingIconComboBox("status/visible")
        self._adult_mode_combo.addItem(tr("显示 NSFW"), "show")
        self._adult_mode_combo.addItem(tr("隐藏 NSFW"), "hide")
        self._adult_mode_combo.addItem(tr("模糊 NSFW"), "blur")
        self._adult_mode_combo.setToolTip(
            tr("显示：正常显示 NSFW Mod 预览\n隐藏：用占位遮挡\n模糊：模糊预览（与在线 Mod 一致）")
        )
        self._adult_mode_combo.currentIndexChanged.connect(self._on_adult_mode_changed)
        toolbar.addWidget(self._adult_mode_combo)

        # 检查更新按钮
        self._check_updates_btn = QPushButton(tr("检查更新"))
        icons.apply_icon(self._check_updates_btn, "actions/refresh")
        self._check_updates_btn.setIconSize(QSize(16, 16))
        self._check_updates_btn.setToolTip(tr("检查已从 GameBanana 导入的 Mod 是否有新版本"))
        self._check_updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._check_updates_btn.clicked.connect(lambda: self.check_updates_requested.emit())
        toolbar.addWidget(self._check_updates_btn)

        layout.addLayout(toolbar)

        # 面包屑在分配宽度内从左侧折叠，并使用 SVG 分隔符，避免内容最小宽度挤压导航栏。
        from src.ui.widgets.breadcrumb_bar import BreadcrumbBar

        self._breadcrumb_frame = QFrame()
        self._breadcrumb_frame.setObjectName("breadcrumbFrame")
        self._apply_breadcrumb_style()
        self._breadcrumb_frame.setFixedHeight(32)
        _bc_outer = QHBoxLayout(self._breadcrumb_frame)
        _bc_outer.setContentsMargins(8, 0, 8, 0)
        _bc_outer.setSpacing(0)
        self._breadcrumb = BreadcrumbBar()
        self._breadcrumb.navigate.connect(self._navigate_to)
        _bc_outer.addWidget(self._breadcrumb)
        layout.addWidget(self._breadcrumb_frame)

        # 搜索、类别和分级筛选，以及批量操作栏。
        search_layout = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(tr("搜索名称、作者、标签..."))
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._on_search)
        self._search_input.returnPressed.connect(
            lambda: self._execute_search_refresh(force_text=self._search_input.text().strip())
        )
        search_layout.addWidget(self._search_input)

        self._tag_filter = TagFilter(self)
        self._tag_filter.changed.connect(self._on_tag_filter_changed)
        search_layout.addWidget(self._tag_filter.button)

        # 类型筛选
        self._type_filter = QComboBox()
        self._type_filter.setFixedWidth(120)
        self._type_filter.addItem(tr("全部类型"), "")
        self._type_filter.addItem(icons.get_icon("meta/category"), tr("类别"), "category")
        self._type_filter.addItem(icons.get_icon("meta/character"), tr("角色/对象"), "character")
        self._type_filter.addItem(icons.get_icon("actions/mod"), "Mod", "mod")
        icons.apply_combo_item_icon(self._type_filter, 1, "meta/category")
        icons.apply_combo_item_icon(self._type_filter, 2, "meta/character")
        icons.apply_combo_item_icon(self._type_filter, 3, "actions/mod")
        self._type_filter.currentIndexChanged.connect(self._on_filter_changed)
        search_layout.addWidget(self._type_filter)

        # 分级筛选仅在选择 Mod 类型后显示。
        self._age_filter = QComboBox()
        self._age_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._age_filter.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._age_filter.addItem(tr("全部分级"), "")
        self._age_filter.addItem(tr("G - 全年龄"), "G")
        self._age_filter.addItem(tr("P - 轻微敏感"), "P")
        self._age_filter.addItem(tr("R - 限制级"), "R")
        self._age_filter.addItem(tr("X · 18+ 内容"), "X")
        self._age_filter.currentIndexChanged.connect(self._on_filter_changed)
        self._age_filter.setVisible(False)  # 初始隐藏，仅Mod类型时显示
        search_layout.addWidget(self._age_filter)

        # 批量禁用按钮
        self._batch_disable_btn = QPushButton(tr("全部禁用"))
        icons.apply_icon(self._batch_disable_btn, "status/disabled")
        self._batch_disable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._batch_disable_btn.clicked.connect(self._on_batch_disable)
        search_layout.addWidget(self._batch_disable_btn)

        # 排序选项
        self._sort_combo = QComboBox()
        self._sort_combo.setFixedWidth(180)
        self._sort_combo.addItem(tr("按字母升序"), "alpha_asc")
        self._sort_combo.addItem(tr("按字母降序"), "alpha_desc")
        self._sort_combo.addItem(tr("按导入时间升序"), "time_asc")
        self._sort_combo.addItem(tr("按导入时间降序"), "time_desc")
        self._sort_combo.addItem(tr("按作者名升序"), "author_asc")
        self._sort_combo.addItem(tr("按作者名降序"), "author_desc")
        self._sort_combo.addItem(tr("按年龄分级升序"), "age_asc")
        self._sort_combo.addItem(tr("按年龄分级降序"), "age_desc")
        self._sort_combo.addItem(tr("按Mod启用状态降序"), "status_desc")
        self._sort_combo.setToolTip(tr("排序方式"))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        search_layout.addWidget(self._sort_combo)

        # 卡片大小滑动条
        self._card_size_label = QLabel()
        icons.apply_pixmap(self._card_size_label, "actions/search", 16, "text.tertiary")
        self._card_size_slider = QSlider(Qt.Orientation.Horizontal)
        self._card_size_slider.setRange(50, 200)
        self._card_size_slider.setValue(self._card_scale)  # 恢复持久化的缩放值
        self._card_size_slider.setFixedWidth(100)
        self._card_size_slider.setToolTip(tr("调整卡片大小"))
        self._card_size_slider.valueChanged.connect(self._on_card_scale_changed)
        self._card_size_slider.sliderPressed.connect(self._begin_card_scale)
        self._card_size_slider.sliderReleased.connect(self._apply_card_scale_pixmaps)
        search_layout.addWidget(self._card_size_label)
        search_layout.addWidget(self._card_size_slider)

        layout.addLayout(search_layout)
        layout.addWidget(self._tag_filter.selection_row)

        # 主分割器（左侧=卡片/列表 + 右侧=预览面板）
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        # 分栏手柄透明并紧贴预览面板边框，使拖动命中区域与视觉边界一致。
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.setHandleWidth(6)

        # 左侧 - 卡片/列表内容
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self._filter_empty = QWidget()
        empty_layout = QVBoxLayout(self._filter_empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(12)
        empty_title = QLabel(tr("没有找到匹配的 Mod"))
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        empty_layout.addWidget(empty_title)
        self._filter_empty_description = QLabel()
        self._filter_empty_description.setTextFormat(Qt.TextFormat.PlainText)
        self._filter_empty_description.setWordWrap(True)
        self._filter_empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._filter_empty_description)
        empty_actions = QHBoxLayout()
        empty_actions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_clear_tags = QPushButton(tr("清除标签"))
        self._empty_clear_tags.clicked.connect(self._tag_filter.clear)
        empty_actions.addWidget(self._empty_clear_tags)
        clear_all = QPushButton(tr("清除全部条件"))
        clear_all.clicked.connect(self._clear_all_conditions)
        empty_actions.addWidget(clear_all)
        empty_layout.addLayout(empty_actions)
        self._filter_empty.hide()
        left_layout.addWidget(self._filter_empty, 1)

        self._category_preset_empty = QWidget()
        preset_empty_layout = QVBoxLayout(self._category_preset_empty)
        preset_empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preset_empty_layout.setSpacing(14)
        preset_empty_title = QLabel(t("暂无类别\n点击下方「+ 新建类别」按钮添加，或导入分类预设"))
        preset_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preset_empty_title.setStyleSheet(f"color: {theme.color('text.secondary')}; font-size: 14px;")
        preset_empty_layout.addWidget(preset_empty_title)
        preset_empty_actions = QHBoxLayout()
        preset_empty_actions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preset_empty_actions.setSpacing(18)
        preset_add = QPushButton(t("+ 新建类别"))
        icons.apply_icon(preset_add, "actions/new-folder")
        preset_add.setCursor(Qt.CursorShape.PointingHandCursor)
        preset_add.clicked.connect(self._on_add_character)
        preset_empty_actions.addWidget(preset_add)
        preset_link = self._make_category_preset_link()
        preset_empty_actions.addWidget(preset_link)
        preset_empty_layout.addLayout(preset_empty_actions)
        self._category_preset_empty.hide()
        left_layout.addWidget(self._category_preset_empty, 1)
        self._category_preset_inline_link = self._make_category_preset_link()
        self._category_preset_inline_link.hide()
        left_layout.addWidget(self._category_preset_inline_link, 0, Qt.AlignmentFlag.AlignHCenter)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._scroll_content = QWidget()
        self._grid_layout = QGridLayout(self._scroll_content)
        self._grid_layout.setSpacing(12)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(self._scroll_content)
        self._scroll_area = scroll
        self._scroll_edge_fade = ScrollEdgeFade(
            self._scroll_area,
            lambda cache=self._vs_card_cache: cache.values(),
            cache_key_provider=lambda card: (
                None if isinstance(card, ModCard) and card.mod_id in self._deploying_frac else card.scroll_render_key()
            ),
        )
        self._card_reveal = CardRevealQueue(
            self, self._scroll_edge_fade, lambda card, item_id: self._vs_card_cache.get(item_id) is card
        )
        # 滚轮平滑缓动（与商店/画廊一致的柔和滚动手感）
        self._smooth_scroller = SmoothScroller.attach(self._scroll_area)
        # 在滚动区视口安装事件过滤器，处理空白区域的点击。
        scroll.viewport().installEventFilter(self)
        # 性能优化：滚动时触发可视区域优先加载
        scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        left_layout.addWidget(scroll)

        # 列表视图 (QTableWidget)
        self._table = ModListTable()
        self._table.tag_clicked.connect(self.select_tag_filter)
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            [tr("类型"), tr("名称"), tr("英文名/作者"), tr("年龄分级"), tr("Mod/状态")]
        )
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(60)  # 禁止把列拖到 0 宽度导致内容不可见
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 220)
        self._table.setColumnWidth(2, 170)
        self._table.setColumnWidth(3, 120)
        self._table.setColumnWidth(4, 140)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        # 通过内联样式设置表格视口背景。背景图模式下交替行透明，避免两层叠色遮蔽背景；
        # 普通模式保留不透明的隔行底色。
        self._apply_overview_table_style()
        # 「已启用」行标记：第 0 列左侧 success 竖条（详见 _EnabledRowDelegate）
        self._enabled_row_delegate = _EnabledRowDelegate(self._table)
        self._table.setItemDelegateForColumn(0, self._enabled_row_delegate)
        self._tag_name_delegate = TagNameDelegate(self._table)
        self._table.setItemDelegateForColumn(1, self._tag_name_delegate)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self._table.cellDoubleClicked.connect(self._on_table_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_table_context_menu)
        self._table.setVisible(False)
        header.sectionResized.connect(self._on_overview_header_section_resized)
        header.viewport().installEventFilter(self)
        # 安装事件过滤器 - 列表视图空白处点击取消选中
        self._table.viewport().installEventFilter(self)
        self._apply_overview_column_widths(force_ratio=not self._overview_user_resized)
        left_layout.addWidget(self._table)

        self._main_splitter.addWidget(left_widget)

        # 右侧 - 统一预览面板
        # 根据选中对象在类别预览和 Mod 预览模式之间切换。
        self._preview_panel = QWidget()
        self._preview_panel.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._preview_panel.setObjectName("previewPanel")
        preview_panel_layout = QVBoxLayout(self._preview_panel)
        preview_panel_layout.setContentsMargins(12, 12, 12, 12)
        preview_panel_layout.setSpacing(0)

        # 类别和 Mod 共用的预览图区域。
        self._panel_preview = PreviewWidget()
        # 短窗口为详情标签预留空间，保留预览操作控件并允许图片缩小。
        self._panel_preview._image_label.setMinimumHeight(8)
        self._panel_preview.preview_changed.connect(self._on_panel_preview_changed)
        self._panel_preview.image_dropped.connect(self._on_panel_preview_dropped)

        # Mod 预览图管理信号。
        self._panel_preview.delete_current_requested.connect(self._on_panel_delete_current)
        self._panel_preview.delete_all_requested.connect(self._on_panel_delete_all)

        # 标题和标签栏位于各自独立滚动的详情页面之外。
        self._panel_details = QWidget()
        self._panel_details.setObjectName("modPreviewDetails")
        self._panel_details.setMinimumSize(0, 280)
        details_layout = QVBoxLayout(self._panel_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("modPreviewHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(3, 7, 3, 8)
        header_layout.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._panel_name_label = _ElidedLabel(tr("选择项目查看详情"))
        self._panel_name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._panel_name_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        title_row.addWidget(self._panel_name_label, 1)
        self._panel_rating_label = QLabel()
        self._panel_rating_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._panel_rating_label.setVisible(False)
        title_row.addWidget(self._panel_rating_label)
        header_layout.addLayout(title_row)
        status_row = QHBoxLayout()
        status_row.setSpacing(7)
        self._panel_status_label = QLabel()
        status_row.addWidget(self._panel_status_label)
        self._panel_variant_label = _ElidedLabel()
        self._panel_variant_label.setObjectName("previewVariantName")
        self._panel_variant_label.setTextFormat(Qt.TextFormat.PlainText)
        self._panel_variant_label.hide()
        status_row.addWidget(self._panel_variant_label, 1)
        header_layout.addLayout(status_row)
        self._panel_deploy_hint = QLabel()
        self._panel_deploy_hint.setTextFormat(Qt.TextFormat.PlainText)
        self._panel_deploy_hint.setWordWrap(True)
        self._panel_deploy_hint.hide()
        header_layout.addWidget(self._panel_deploy_hint)
        details_layout.addWidget(header)
        self._detail_tabs = QTabWidget()
        self._detail_tabs.setObjectName("modPreviewTabs")
        self._detail_tabs.setDocumentMode(True)
        self._detail_tabs.tabBar().setExpanding(True)
        self._detail_tabs.setUsesScrollButtons(False)
        self._detail_tabs.setMinimumSize(0, 0)
        details_layout.addWidget(self._detail_tabs, 1)

        # 信息页。
        self._panel_info_scroll = QScrollArea()
        self._panel_info_scroll.setWidgetResizable(True)
        self._panel_info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._panel_info_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._panel_info_frame = QFrame()
        self._panel_info_frame.setObjectName("previewInfoFrame")
        self._panel_info_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        self._panel_info_frame.setMinimumWidth(0)
        self._panel_info_frame.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        panel_info_layout = QVBoxLayout(self._panel_info_frame)
        panel_info_layout.setContentsMargins(3, 13, 5, 8)
        panel_info_layout.setSpacing(9)
        facts = QGridLayout()
        facts.setHorizontalSpacing(12)
        facts.setVerticalSpacing(10)
        facts.setColumnMinimumWidth(0, 68)
        facts.setColumnStretch(1, 1)
        self._panel_author_label = QLabel()
        self._panel_version_label = QLabel()
        self._panel_category_label = QLabel()
        self._panel_size_label = QLabel()
        self._panel_fact_captions = []
        for index, (text, value) in enumerate(
            (
                (tr("作者"), self._panel_author_label),
                (tr("版本"), self._panel_version_label),
                (tr("所属分类"), self._panel_category_label),
                (tr("资源大小"), self._panel_size_label),
            )
        ):
            caption = QLabel(text)
            caption.setObjectName("previewFactCaption")
            self._panel_fact_captions.append(caption)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setMinimumWidth(0)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            facts.addWidget(caption, index, 0)
            facts.addWidget(value, index, 1)
        panel_info_layout.addLayout(facts)
        self._panel_tags_title = QLabel(tr("标签"))
        self._panel_tags_title.setObjectName("previewSectionTitle")
        panel_info_layout.addWidget(self._panel_tags_title)
        self._panel_tags = TagStrip(limit=None, height=24, font_size=int(theme.font("size_caption")))
        self._panel_tags.tag_clicked.connect(self.select_tag_filter)
        self._panel_tags.hide()
        panel_info_layout.addWidget(self._panel_tags)
        self._panel_desc_title = QLabel(tr("描述"))
        self._panel_desc_title.setObjectName("previewSectionTitle")
        panel_info_layout.addWidget(self._panel_desc_title)
        self._panel_desc_label = QLabel("")
        self._panel_desc_label.setTextFormat(Qt.TextFormat.PlainText)
        self._panel_desc_label.setWordWrap(True)
        self._panel_desc_label.setMinimumWidth(0)
        self._panel_desc_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._panel_desc_label.setVisible(False)
        panel_info_layout.addWidget(self._panel_desc_label)
        self._panel_file_title = QLabel(tr("文件与来源"))
        self._panel_file_title.setObjectName("previewSectionTitle")
        panel_info_layout.addWidget(self._panel_file_title)
        self._panel_path_label = QLabel("")
        self._panel_path_label.setTextFormat(Qt.TextFormat.PlainText)
        self._panel_path_label.setWordWrap(True)
        # 长 Windows 路径缺少自然分词边界，保留完整可选文本，但宽度由父布局分配。
        self._panel_path_label.setMinimumWidth(0)
        self._panel_path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._apply_panel_text_style()
        self._panel_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._panel_path_label.setVisible(False)
        panel_info_layout.addWidget(self._panel_path_label)
        self._panel_source_label = QLabel()
        self._panel_source_label.setWordWrap(True)
        self._panel_source_label.setOpenExternalLinks(True)
        self._panel_source_label.setMinimumWidth(0)
        self._panel_source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        panel_info_layout.addWidget(self._panel_source_label)
        panel_info_layout.addStretch()
        self._panel_info_scroll.setWidget(self._panel_info_frame)
        self._detail_tabs.addTab(self._panel_info_scroll, tr("信息"))

        # 按键页使用独立容器，整体替换异步解析结果并保留滚动位置。
        self._panel_keys_scroll = QScrollArea()
        self._panel_keys_scroll.setObjectName("previewKeysScroll")
        self._panel_keys_scroll.setWidgetResizable(True)
        self._panel_keys_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._panel_keys_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._panel_keys_host = QWidget()
        self._panel_keys_host.setMinimumWidth(0)
        self._panel_keys_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        keys_host_layout = QVBoxLayout(self._panel_keys_host)
        keys_host_layout.setContentsMargins(3, 10, 5, 6)
        keys_host_layout.setSpacing(6)
        self._panel_keys_container = QWidget()
        self._panel_keys_container.setMinimumWidth(0)
        self._panel_keys_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._panel_keys_layout = QVBoxLayout(self._panel_keys_container)
        self._panel_keys_layout.setContentsMargins(0, 0, 0, 0)
        self._panel_keys_layout.setSpacing(2)
        self._panel_keys_container.hide()
        keys_host_layout.addWidget(self._panel_keys_container)
        self._panel_keys_empty = QLabel(tr("这个 Mod 没有可编辑的按键配置"))
        self._panel_keys_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._panel_keys_empty.setWordWrap(True)
        keys_host_layout.addWidget(self._panel_keys_empty)
        keys_host_layout.addStretch()
        self._panel_keys_scroll.setWidget(self._panel_keys_host)
        self._detail_tabs.addTab(self._panel_keys_scroll, tr("按键 0"))
        self._variant_panel = ModVariantPanel()
        self._variant_panel.info_requested.connect(lambda: self._detail_tabs.setCurrentIndex(0))
        self._detail_tabs.addTab(self._variant_panel, tr("变体 0"))
        self._detail_tabs.currentChanged.connect(self._on_detail_tab_changed)
        self._apply_detail_style()

        # 分割线：预览图与信息之间（细分割线见 fluent.qss #previewSplitter）
        self._panel_splitter = QSplitter(Qt.Orientation.Vertical)
        self._panel_splitter.setObjectName("previewSplitter")
        self._panel_splitter.setHandleWidth(7)
        self._panel_splitter.addWidget(self._panel_preview)
        self._panel_splitter.addWidget(self._panel_details)
        # 保存预览和文本区的像素高度，并按比例恢复。
        from src.core.config import Config as _Cfg

        _psplit = _Cfg.instance().get("ui.overview_preview_split", [250, 150])
        self._panel_splitter.setSizes(
            [int(_psplit[0]), int(_psplit[1])] if isinstance(_psplit, list) and len(_psplit) == 2 else [250, 150]
        )
        self._panel_splitter.setChildrenCollapsible(False)
        self._panel_splitter.splitterMoved.connect(self._on_panel_splitter_moved)
        preview_panel_layout.addWidget(self._panel_splitter, 1)

        # 操作按钮栏（根据模式显示不同按钮）
        self._panel_btn_layout = TagFlowLayout(spacing=6)
        self._panel_btn_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._panel_btn_layout.setContentsMargins(0, 10, 0, 0)

        self._panel_toggle_btn = QPushButton(tr("启用/禁用"))
        icons.apply_icon(self._panel_toggle_btn, "status/enabled")
        self._panel_toggle_btn.setIconSize(QSize(16, 16))
        self._panel_toggle_btn.setEnabled(False)
        self._panel_toggle_btn.clicked.connect(self._on_panel_toggle)
        self._panel_btn_layout.addWidget(self._panel_toggle_btn)

        self._panel_edit_btn = QPushButton(tr("编辑"))
        icons.apply_icon(self._panel_edit_btn, "actions/edit")
        self._panel_edit_btn.setIconSize(QSize(16, 16))
        self._panel_edit_btn.setEnabled(False)
        self._panel_edit_btn.clicked.connect(self._on_panel_edit)
        self._panel_btn_layout.addWidget(self._panel_edit_btn)

        self._panel_open_btn = QPushButton(tr("打开源目录"))
        icons.apply_icon(self._panel_open_btn, "actions/folder-open")
        self._panel_open_btn.setIconSize(QSize(16, 16))
        self._panel_open_btn.setToolTip(tr("打开该 Mod / 类别的源目录"))
        self._panel_open_btn.setEnabled(False)
        self._panel_open_btn.clicked.connect(self._on_panel_open_folder)
        self._panel_btn_layout.addWidget(self._panel_open_btn)

        self._panel_delete_btn = QPushButton(tr("删除"))
        icons.apply_icon(self._panel_delete_btn, "actions/remove")
        self._panel_delete_btn.setIconSize(QSize(16, 16))
        self._panel_delete_btn.setEnabled(False)
        self._panel_delete_btn.clicked.connect(self._on_panel_delete)
        self._panel_btn_layout.addWidget(self._panel_delete_btn)
        for button in (self._panel_toggle_btn, self._panel_edit_btn, self._panel_open_btn, self._panel_delete_btn):
            button.setStyleSheet("font-size: 12px; padding: 5px 8px; min-width: 0;")

        # 在线查找按钮（仅类别模式可见，受 Config 开关控制）
        self._panel_gb_search_btn = QPushButton(tr("在线查找"))
        self._panel_gb_search_btn.setToolTip(tr("在线查找该类别的 Mod（GameBanana）"))
        icons.apply_icon(self._panel_gb_search_btn, "actions/search")
        self._panel_gb_search_btn.setIconSize(QSize(16, 16))
        self._panel_gb_search_btn.setEnabled(False)
        self._panel_gb_search_btn.setVisible(False)
        self._panel_gb_search_btn.clicked.connect(self._on_panel_gb_search)
        self._panel_btn_layout.addWidget(self._panel_gb_search_btn)
        preview_panel_layout.addLayout(self._panel_btn_layout)

        self._panel_preview.setMinimumWidth(340)
        self._preview_panel.setMinimumWidth(380)
        # 当前面板模式: "none", "category", "mod"
        self._panel_mode = "none"
        # 当前选中的类别
        self._selected_category: dict | None = None

        # 先将预览面板加入分栏，再设为可见，避免无父控件时作为顶级窗口短暂弹出。
        self._main_splitter.addWidget(self._preview_panel)
        self._preview_panel.setVisible(True)  # 重新指定父控件后恢复预览栏可见状态。
        # 预览栏保持已保存宽度，左侧区域吸收窗口尺寸变化。
        _pw = max(200, int(_Cfg.instance().get("ui.overview_preview_width", 350)))
        self._main_splitter.setSizes([600, _pw])
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setChildrenCollapsible(True)  # 允许通过拖动分割线收起预览栏。
        self._main_splitter.splitterMoved.connect(self._on_main_splitter_moved)

        layout.addWidget(self._main_splitter, 1)

        # 状态栏保持固定高度。
        self._status_label = QLabel(t("就绪"))
        self._status_label.setFixedHeight(20)
        self._status_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 11px;")
        status_layout = QHBoxLayout()
        status_layout.addWidget(self._status_label, 1)
        self._source_reconcile_notice = QLabel("")
        self._source_reconcile_notice.setFixedHeight(20)
        self._source_reconcile_notice.setStyleSheet(f"color: {theme.color('text.secondary')}; font-size: 11px;")
        self._source_reconcile_notice.hide()
        status_layout.addWidget(self._source_reconcile_notice)
        self._deploy_status_label = QLabel("")
        self._deploy_status_label.setFixedHeight(20)
        self._deploy_status_label.hide()
        status_layout.addWidget(self._deploy_status_label)
        layout.addLayout(status_layout)

    def set_current_game(self, game_id: str) -> None:
        """游戏变化时重置导航、位置与源观测状态，清空标签筛选并加载新游戏的标签目录。"""
        if self._current_game_id != game_id:
            self.begin_loading()
            self._invalidate_view_position(clear=True)
            self.set_source_observations(())
            self._current_game_id = game_id
            self._current_parent_id = ""
            self._tag_filter.close_popup()
            self._tag_filter.clear(emit=False)
            self.refresh_tags(refresh_view=False)
            self._end_loading()

    def _view_scrollbar(self, mode: str):
        """按卡片或列表模式返回对应的纵向滚动条。"""
        return (self._table if mode == "list" else self._scroll_area).verticalScrollBar()

    def _remember_presented_position(self) -> None:
        """停止平滑滚动并保存当前已呈现视图的位置，位置恢复过程中不覆盖缓存。"""
        self._smooth_scroller.cancel()
        view = self._presented_view
        if view is not None and not self._applying_position:
            self._view_positions[view.key] = (view.signature, self._view_scrollbar(view.key[2]).value())

    def _request_view_position(self, *, parent_id=None, mode=None) -> None:
        """记录离开前的位置，为待切换的游戏、目录和布局创建新的请求代次。"""
        # 连续请求切换期间，仍显示的视图拥有当前控件。
        self._remember_presented_position()
        self._view_request_generation += 1
        self._pending_view = (
            self._view_request_generation,
            self._current_game_id,
            self._current_parent_id if parent_id is None else parent_id,
            self._view_mode if mode is None else mode,
        )

    def _invalidate_view_position(self, *, clear=False) -> None:
        """停止平滑滚动并使待切换与已呈现快照失效，可同时清空历史位置。"""
        self._smooth_scroller.cancel()
        self._view_request_generation += 1
        self._pending_view = None
        self._presented_view = None
        if clear:
            self._view_positions.clear()

    def _prepare_view_position(self, filter_text: str) -> tuple[_PresentedOverviewView, int]:
        """捕获当前筛选快照，只有同一视图的筛选签名一致时才恢复保存的滚动位置。"""
        # 替换布局前重新采样滚动位置，覆盖后台读取期间的用户滚动。
        self._remember_presented_position()
        tags = tuple(sorted(tag_id for tag_id in self._tag_filter.selected_ids if tag_id in self._tag_filter.catalog))
        effective_tags = tags if self._tag_filter.button.isEnabled() else ()
        signature = (
            tag_key(filter_text),
            self._type_filter.currentData() or "",
            self._age_filter.currentData() or "",
            effective_tags,
            self._tag_filter.match_mode if effective_tags else "",
            self._sort_key,
            self._category_sort_key,
        )
        view = _PresentedOverviewView(
            (self._current_game_id, self._current_parent_id, self._view_mode),
            signature,
            self._search_input.text(),
            tags,
            self._tag_filter.match_mode,
        )
        saved = self._view_positions.get(view.key)
        return view, saved[1] if saved is not None and saved[0] == signature else 0

    def _commit_view_position(
        self, view: _PresentedOverviewView, value: int, *, grid_height: int | None = None
    ) -> None:
        """先确定布局与滚动范围，再应用受范围约束的位置并发布已呈现视图快照。"""
        self._smooth_scroller.cancel()
        scrollbar = self._view_scrollbar(view.key[2])
        if grid_height is not None:
            # 首次绑定前确定内容与视口范围，避免等待下一布局事件。
            self._scroll_content.resize(self._scroll_content.width(), grid_height)
            scrollbar.setRange(0, max(0, grid_height - self._scroll_area.viewport().height()))
            scrollbar.setPageStep(self._scroll_area.viewport().height())
            # 绑定卡片和捕获首张像素图前先确定滚动条及视口宽度。
            QApplication.sendEvent(self._scroll_area, QEvent(QEvent.Type.LayoutRequest))
        else:
            self._table.doItemsLayout()
        scrollbar.setValue(max(scrollbar.minimum(), min(value, scrollbar.maximum())))
        self._smooth_scroller.cancel()
        self._presented_view = view
        self._pending_view = None

    def _restore_presented_conditions(self) -> None:
        """将目录、布局和筛选恢复为已呈现快照，阻断回填信号并更新面包屑与空状态。"""
        view = self._presented_view
        if view is None:
            return
        self._current_parent_id = view.key[1]
        self._view_mode = view.key[2]
        self._search_timer.stop()
        widgets = (
            self._search_input,
            self._type_filter,
            self._age_filter,
            self._sort_combo,
            self._view_combo,
            self._tag_filter,
        )
        blockers = [QSignalBlocker(widget) for widget in widgets]
        try:
            self._search_input.setText(view.search_text)
            self._pending_search_text = view.search_text.strip()
            for combo, value in (
                (self._type_filter, view.signature[1]),
                (self._age_filter, view.signature[2]),
                (self._sort_combo, view.signature[5]),
                (self._view_combo, view.key[2]),
            ):
                combo.setCurrentIndex(max(0, combo.findData(value)))
            self._sort_key = view.signature[5]
            self._category_sort_key = view.signature[6]
            self._tag_filter.selected_ids = [tag for tag in view.tag_ids if tag in self._tag_filter.catalog]
            self._tag_filter.set_match_mode(view.tag_mode)
            self._tag_filter.set_mod_types_enabled(view.signature[1] not in ("category", "character"))
            self._tag_filter._render_selection()
            self._age_filter.setVisible(view.signature[1] == "mod")
        finally:
            for blocker in blockers:
                blocker.unblock()
        self._card_size_slider.setVisible(self._view_mode == "grid")
        self._card_size_label.setVisible(self._view_mode == "grid")
        self._characters = self._children_map.get(self._current_parent_id, [])
        self._update_breadcrumb()
        self._update_filter_empty(bool(self._display_items), view.search_text)

    def set_source_observations(self, observations) -> None:
        """身份检查期间可先显示已确认的路径观察。"""
        count = len({observation.mod_id for observation in observations})
        self._source_reconcile_notice.setText(
            tr("已检测到 {count} 个 Mod 的源路径变动，正在核对关联项…", count=count) if count else ""
        )
        self._source_reconcile_notice.setVisible(bool(count))

    def set_pending_missing_mods(self, mod_ids) -> None:
        """登记等待缺失确认的 Mod，并禁用其可见卡片、列表行和详情启用操作。"""
        if not hasattr(self, "_pending_missing_mods"):
            self._pending_missing_mods = set()
        newly_missing = set(mod_ids) - self._pending_missing_mods
        self._pending_missing_mods.update(newly_missing)
        for mid in newly_missing:
            card = self._vs_card_cache.get(mid)
            if card is not None:
                card.setEnabled(False)
            row = self._display_idx_map.get(mid, -1)
            if row >= 0 and self._view_mode == "list":
                item = self._table.item(row, 0)
                if item is not None:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            if self._selected_mod and self._selected_mod.mod_id == mid:
                self._panel_toggle_btn.setEnabled(False)

    def apply_node_delta(self, nodes) -> None:
        """仅替换变化的节点及其父子关系。"""
        structural = False
        current_children_changed = False
        positions = getattr(self, "_delta_node_positions", {})
        for node in nodes:
            nid = node["id"]
            if hasattr(self, "_delta_node_parents"):
                self._delta_node_parents[nid] = node.get("parent_id") or ""
            index = positions.get(nid)
            if index is not None:
                previous = self._all_characters[index]
                old_parent = previous.get("parent_id") or ""
                new_parent = node.get("parent_id") or ""
                changed = (old_parent, previous.get("name"), previous.get("name_en")) != (
                    new_parent,
                    node.get("name"),
                    node.get("name_en"),
                )
                structural |= changed
                current_children_changed |= changed and self._current_parent_id in (old_parent, new_parent)
                self._all_characters[index] = node
                if old_parent == new_parent:
                    self._children_map[old_parent] = [
                        node if child["id"] == nid else child for child in self._children_map.get(old_parent, ())
                    ]
                else:
                    self._children_map[old_parent] = [
                        child for child in self._children_map.get(old_parent, ()) if child["id"] != nid
                    ]
                    self._children_map.setdefault(new_parent, []).append(node)
            else:
                structural = True
                current_children_changed |= (node.get("parent_id") or "") == self._current_parent_id
                positions[nid] = len(self._all_characters)
                self._all_characters.append(node)
                self._children_map.setdefault(node.get("parent_id") or "", []).append(node)
            row = self._display_idx_map.get(nid, -1)
            if row >= 0:
                if self._display_items[row]["data"].get("thumbnail") != node.get("thumbnail"):
                    self._vs_invalidate_prefetch(schedule=True)
                self._display_items[row]["data"] = node
                if self._view_mode == "list" and row not in self._table._pending_rows:
                    item = self._table.item(row, 4)
                    if item is not None:
                        item.setText(f"{node.get('enabled_count', 0)} / {node.get('mod_count', 0)}")
            card = self._vs_card_cache.get(nid)
            if isinstance(card, CharacterCard):
                card.update_counts(node)
        self._characters = self._children_map.get(self._current_parent_id, [])
        if current_children_changed:
            self._refresh_current_level()
        elif structural:
            self._update_breadcrumb()

    def set_characters(self, characters: list[dict], refresh: bool = True, force_root: bool = False) -> None:
        """替换分类快照与父子索引，移除已删除目录的位置缓存，必要时回到根目录并刷新。"""
        all_ids = {c.get("id", "") for c in characters} | {""}
        self._view_positions = {key: position for key, position in self._view_positions.items() if key[1] in all_ids}
        presented_deleted = self._presented_view is not None and self._presented_view.key[1] not in all_ids
        target_deleted = self._current_parent_id not in all_ids
        if presented_deleted or target_deleted:
            # 失败回调不能重新创建已删除的源或目标。
            self._invalidate_view_position()
            force_root = True
        self._all_characters = characters
        self._delta_node_parents = {node["id"]: node.get("parent_id") or "" for node in characters}
        self._delta_node_positions = {node["id"]: i for i, node in enumerate(characters)}
        self._build_children_map()
        if force_root:
            # 切换游戏时强制回到根目录
            self._request_view_position(parent_id="")
            self._end_loading()
            self.set_source_observations(())
            self._tag_filter.clear(emit=False)
            self._current_parent_id = ""
            self._current_mods = []
            self._subtree_mods = []
            self._selected_ids.clear()
            self._selected_mod = None
            self._selected_category = None
            self._hide_panel()
        elif self._current_parent_id:
            # 常规刷新时：检查当前目录是否仍存在
            all_ids = {c.get("id", "") for c in self._all_characters}
            if self._current_parent_id not in all_ids:
                self._tag_filter.clear(emit=False)
                self._current_parent_id = ""
                self._current_mods = []
                self._subtree_mods = []
        if refresh:
            self._refresh_current_level()
        else:
            self._characters = self._children_map.get(self._current_parent_id, [])
            self._update_character_counts()

    def _update_character_counts(self) -> None:
        """更新已有分类绑定，不重建整个视图。"""
        for category in self._all_characters:
            category_id = category.get("id", "")
            row = self._display_idx_map.get(category_id, -1)
            if 0 <= row < len(self._display_items):
                item = self._display_items[row]
                if item["type"] == "category" and item["id"] == category_id:
                    if item["data"].get("thumbnail") != category.get("thumbnail"):
                        self._vs_invalidate_prefetch(schedule=True)
                    # 延迟列表行和回收卡片读取同一条记录。
                    item["data"] = category
            card = self._vs_card_cache.get(category_id)
            if isinstance(card, CharacterCard):
                card.update_counts(category)
            if self._selected_category and self._selected_category.get("id") == category_id:
                self._selected_category = category
            if 0 <= row < self._table.rowCount() and row not in self._table._pending_rows:
                identity = self._table.item(row, 0)
                if (
                    identity
                    and identity.data(Qt.ItemDataRole.UserRole) == category_id
                    and identity.data(Qt.ItemDataRole.UserRole + 1) == "category"
                ):
                    count_item = self._table.item(row, 4)
                    if count_item is not None:
                        count_item.setText(f"{category.get('enabled_count', 0)} / {category.get('mod_count', 0)}")

    def set_current_mods(
        self, mods: list[Mod], subtree_mods: list[Mod] | None = None, *, refresh_view: bool = True
    ) -> None:
        """设置当前层级下的 Mod 列表"""
        self._end_loading()
        self._current_mods = list(mods)
        self._subtree_mods = list(subtree_mods) if subtree_mods is not None else list(mods)
        self._delta_direct_positions = {mod.mod_id: i for i, mod in enumerate(self._current_mods)}
        self._delta_subtree_positions = {mod.mod_id: i for i, mod in enumerate(self._subtree_mods)}
        self._delta_by_id = {mod.mod_id: mod for mod in self._subtree_mods}
        self._delta_lists = (self._current_mods, self._subtree_mods)
        self.refresh_tags(refresh_view=False)
        if refresh_view:
            self._refresh_current_level()

    def refresh_tags(self, *, refresh_view: bool = True) -> bool:
        """刷新标签显示，并报告活动筛选是否变化。"""
        selected_before = tuple(self._tag_filter.selected_ids)
        service_factory = getattr(self._mod_manager, "get_tag_service", None)
        if callable(service_factory):
            tags = service_factory().list_tags()
        else:
            # 未注入管理器的独立页面仍可显示已有元数据。
            catalog: dict[str, TagDefinition] = {}
            for mod in self._subtree_mods:
                for tag in tags_for_names(mod.info.tags, catalog):
                    catalog.setdefault(tag.normalized_name, tag).usage_count += 1
            tags = list(catalog.values())
        self._tag_filter.set_catalog(tags)
        catalog = {tag.normalized_name: tag for tag in tags}
        if catalog != self._tag_catalog_by_name:
            # 保持卡片共享映射的对象身份，定义或颜色变化时仅刷新已有显示。
            self._tag_catalog_by_name.clear()
            self._tag_catalog_by_name.update(catalog)
            for card in self._vs_card_cache.values():
                if isinstance(card, ModCard):
                    card.set_tag_catalog(self._tag_catalog_by_name)
            for row in list(self._table._name_data):
                if row < len(self._display_items) and self._display_items[row]["type"] == "mod":
                    self._set_table_mod_name(row, self._display_items[row]["data"])
        if self._selected_mod and self._panel_mode == "mod":
            self._panel_tags.set_tags(tags_for_names(self._selected_mod.info.tags, self._tag_catalog_by_name))
        if refresh_view:
            self._on_tag_filter_changed()
        return selected_before != tuple(self._tag_filter.selected_ids)

    def select_tag_filter(self, tag_id: str) -> None:
        """卡片、表格、溢出列表和预览标签共用入口。"""
        self._tag_filter.toggle(tag_id)

    def _on_tag_filter_changed(self) -> None:
        """使用当前搜索文字重新执行标签筛选和视图刷新。"""
        self._execute_search_refresh(force_text=self._search_input.text())

    def _clear_all_conditions(self) -> None:
        """阻断中间变化信号后清空文字、类型、分级和标签条件，再统一触发刷新。"""
        for widget in (self._search_input, self._type_filter, self._age_filter):
            widget.blockSignals(True)
        self._search_input.clear()
        self._pending_search_text = ""
        self._search_timer.stop()
        self._type_filter.setCurrentIndex(0)
        self._age_filter.setCurrentIndex(0)
        self._tag_filter.clear(emit=False)
        for widget in (self._search_input, self._type_filter, self._age_filter):
            widget.blockSignals(False)
        self._on_filter_changed()

    def _filtered_content(self, filter_text: str) -> tuple[list[dict], list[Mod]]:
        """两种布局使用相同子树、规范化文字和标签条件。"""
        query = tag_key(filter_text)
        type_filter = self._type_filter.currentData() or ""
        rating_filter = self._age_filter.currentData() or ""
        has_tags = bool(self._tag_filter.selected_ids) and self._tag_filter.button.isEnabled()
        has_search = bool(query) or has_tags
        cats = [] if has_tags else self._get_search_base_categories(type_filter, has_search)
        if query:
            cats = [
                cat for cat in cats if any(query in tag_key(cat.get(field, "") or "") for field in ("name", "name_en"))
            ]
        mods = self._get_search_base_mods(type_filter, has_search)
        if query:
            mods = [
                mod
                for mod in mods
                if any(
                    query in tag_key(text or "") for text in (mod.display_name, mod.info.author, *(mod.info.tags or []))
                )
            ]
        if rating_filter:
            mods = [mod for mod in mods if mod.info.age_rating.value == rating_filter]
        if has_tags:
            mods = [mod for mod in mods if self._tag_filter.matches(mod.info.tags)]
        return self._apply_sort(cats, mods)

    def _make_category_preset_link(self) -> QPushButton:
        """创建分类预设导入入口，将点击请求交给宿主处理。"""
        link = QPushButton(tr("从分类预设开始"))
        link.setObjectName("categoryPresetStart")
        icons.apply_icon(link, "actions/import")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(self.category_preset_import_requested.emit)
        return link

    def _update_filter_empty(self, has_results: bool, filter_text: str) -> bool:
        """区分筛选无结果与游戏根目录未分类状态，切换相应提示和布局，并返回是否显示空状态。"""
        active = bool(
            filter_text.strip()
            or self._tag_filter.selected_ids
            or self._type_filter.currentData()
            or self._age_filter.currentData()
        )
        show = active and not has_results
        empty_game = bool(
            self._current_game_id and not self._current_parent_id and not self._all_characters and not active
        )
        preset_start = empty_game and not has_results
        self._category_preset_empty.setVisible(preset_start)
        self._category_preset_inline_link.setVisible(empty_game and has_results)
        self._filter_empty.setVisible(show)
        self._scroll_area.setVisible(self._view_mode == "grid" and not show and not preset_start)
        self._table.setVisible(self._view_mode == "list" and not show and not preset_start)
        if show:
            parts = [tr("已检索当前类别及子类别。")]
            if filter_text.strip():
                parts.append(tr("搜索：{value}", value=filter_text.strip()))
            if self._tag_filter.selected_ids and self._tag_filter.button.isEnabled():
                names = [self._tag_filter.catalog[tag_id].name for tag_id in self._tag_filter.selected_ids]
                mode = tr("满足全部") if self._tag_filter.match_mode == "all" else tr("满足任一")
                parts.append(tr("标签{mode}：{value}", mode=mode, value="、".join(names)))
            self._filter_empty_description.setText("\n".join(parts))
            self._empty_clear_tags.setVisible(bool(self._tag_filter.selected_ids))
        return show or preset_start

    def _set_table_mod_name(self, row: int, mod: Mod) -> None:
        """填充列表名称单元格及标签代理数据，同时标记该 Mod 是否存在可用更新。"""
        item = QTableWidgetItem(mod.display_name)
        item.setToolTip(mod.display_name)
        item.setData(TagNameDelegate.ROLE_HAS_TAG_CELL, True)
        self._table.setItem(row, 1, item)
        self._table.set_name_data(
            row,
            mod.display_name,
            tags_for_names(mod.info.tags, self._tag_catalog_by_name),
            updatable=mod.mod_id in self._updatable_ids,
        )

    def _sort_text_key(self, text: str) -> object:
        """缓存保留非汉字内容的拼音排序键，以原文区分同音名称，转换器变化时重建缓存。"""
        value = (text or "").strip()
        converter = self._lazy_pinyin
        if not hasattr(self, "_sort_text_cache") or self._sort_text_converter is not converter:
            self._sort_text_cache = OrderedDict()
            self._sort_text_converter = converter
        cache = self._sort_text_cache
        if value in cache:
            cache.move_to_end(value)
            return cache[value]
        result = (value.casefold(),)
        if self._lazy_pinyin is not None and value:
            try:
                py = "".join(self._lazy_pinyin(value, errors="default"))
                if py:
                    result = (py.casefold(), value.casefold())
            except Exception:
                pass
        # 按名称文本缓存排序键，改名立即生效，不复用过期身份缓存。
        cache[value] = result
        if len(cache) > 20_000:
            cache.popitem(last=False)
        return result

    def _get_descendant_categories(self, parent_id: str) -> list[dict]:
        """获取 parent_id 下全部子孙类别（不含 parent_id 自身）"""
        result: list[dict] = []
        queue = list(self._children_map.get(parent_id, []))
        while queue:
            node = queue.pop(0)
            result.append(node)
            queue.extend(self._children_map.get(node.get("id", ""), []))
        return result

    def _get_search_base_categories(self, type_filter: str, has_search: bool) -> list[dict]:
        """搜索时返回当前目录下所有子孙对象，非搜索时仅当前层级。"""
        source = self._get_descendant_categories(self._current_parent_id) if has_search else list(self._characters)
        if type_filter == "character":
            return [c for c in source if c.get("node_type") == "character"]
        if type_filter == "category":
            return [c for c in source if c.get("node_type", "category") != "character"]
        if type_filter == "mod":
            return []
        return source

    def _get_search_base_mods(self, type_filter: str, has_search: bool) -> list[Mod]:
        """分类类型筛选不包含 Mod；存在搜索时使用子树数据，否则仅返回当前目录内容。"""
        if type_filter in ("category", "character"):
            return []
        return list(self._subtree_mods if has_search else self._current_mods)

    def _set_searching_state(self, searching: bool) -> None:
        """搜索期间显示检索提示，结束后恢复选择与条目数量说明。"""
        if searching:
            self._status_label.setText(tr("正在检索中..."))
        else:
            self._update_status_bar_selection()

    def set_mod_loader(self, request) -> None:
        """向宿主请求新查询需要的数据范围。"""
        self._request_mods = request

    def set_enabled_mod_ids_loader(self, getter) -> None:
        """批量目录操作按完整范围处理，不依赖当前显示数据。"""
        self._enabled_mod_ids_loader = getter

    def begin_loading(self) -> None:
        """保留当前卡片，停止上一个查询的未完成工作。"""
        self._end_card_scale()
        self._set_searching_state(True)
        self._table.cancel_row_reveal()
        if self._mods_loading:
            return
        self._cancel_directory_intro()
        if self._pending_view is None:
            self._request_view_position()
        self._mods_loading = True
        for timer in (
            self._search_timer,
            self._vs_population_timer,
            self._vs_scroll_timer,
            self._vs_pixmap_timer,
            self._resize_timer,
            self._scale_timer,
        ):
            timer.stop()
        self._fresh_population = False
        self._vs_population_pending = False
        self._vs_pending_pixmap_cards.clear()
        self._vs_invalidate_prefetch()
        self._vs_image_requests.clear()
        self._pixmap_worker.clear_queue()
        self._vs_window = None
        for card in self._vs_card_cache.values():
            card._preview_requested = False
            label = card._preview_label if isinstance(card, ModCard) else card._thumb
            label._stop_loading()
        # 替换范围加载期间保留已有单元格，不在绘制和滚动中继续填充旧数据。
        self._loading_table_state = (self._table._row_factory, self._table._syncing_rows)
        self._table._row_factory = None
        self._table._syncing_rows = True
        self._table_reveal_gen = getattr(self, "_table_reveal_gen", 0) + 1

    def _end_loading(self) -> None:
        """结束 Mod 加载状态，并恢复加载前保留的列表行工厂和同步标记。"""
        self._mods_loading = False
        if self._loading_table_state is not None:
            self._table._row_factory, self._table._syncing_rows = self._loading_table_state
            self._loading_table_state = None

    def loading_failed(self) -> None:
        """替换查询失败时恢复保留的视图。"""
        self._cancel_directory_intro()
        self._remember_presented_position()
        self._view_request_generation += 1
        self._pending_view = None
        self._restore_presented_conditions()
        self._end_loading()
        self._vs_window = None
        if self._view_mode == "grid":
            for card in self._vs_card_cache.values():
                if not card._preview_resolved:
                    label = card._preview_label if isinstance(card, ModCard) else card._thumb
                    label.start_loading()
            self._apply_card_scale_fast()
        else:
            for row in range(min(24, self._table.rowCount())):
                if self._table.isRowHidden(row):
                    self._table.setRowHidden(row, False)
            self._table._sync_visible_rows()
        self._set_searching_state(False)

    def _execute_search_refresh(self, force_text: str | None = None) -> None:
        """只提交仍匹配输入框的搜索，保存位置后请求后台刷新；无外部加载器时刷新当前布局。"""
        text = self._pending_search_text if force_text is None else force_text.strip()
        if text != self._search_input.text().strip():
            return
        self._request_view_position()
        if self._request_mods is not None:
            self.begin_loading()
            self._request_mods()
            return
        self._set_searching_state(False)
        if self._view_mode == "grid":
            self._refresh_cards(text)
        else:
            self._refresh_table(text)

    def scroll_to_item(self, item_id: str) -> None:
        """滚动到指定项（Mod 或类别）的位置，使其在视口中可见"""
        idx = self._display_idx_map.get(item_id, -1)
        if idx < 0 or self._vs_cols <= 0 or self._vs_row_h <= 0:
            return
        row = idx // self._vs_cols
        target_y = row * self._vs_row_h
        # 将目标置于视口中部偏上位置
        viewport_h = self._scroll_area.viewport().height()
        target_y = max(0, target_y - viewport_h // 3)
        self._scroll_area.verticalScrollBar().setValue(target_y)

    def apply_reconcile_delta(self, mods: list[Mod], removed_ids) -> bool:
        """发布小型提交差异，不重建未变化控件。

        仅查询变化的标识，保持延迟表格行引用的列表对象；缓存无法安全接受差异时返回 False。
        """
        changed = {mod.mod_id: mod for mod in mods}
        pending_missing = getattr(self, "_pending_missing_mods", set())
        recovering = set(changed) & pending_missing
        for mid in set(changed) | set(removed_ids):
            if mid in pending_missing:
                pending_missing.discard(mid)
                if mid in removed_ids:
                    continue
                card = self._vs_card_cache.get(mid)
                if card is not None:
                    card.setEnabled(True)
                row = self._display_idx_map.get(mid, -1)
                if row >= 0 and self._view_mode == "list":
                    item = self._table.item(row, 0)
                    if item is not None:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
        affected = set(changed) | set(removed_ids)
        if not affected:
            return True
        parent = self._current_parent_id
        lists = getattr(self, "_delta_lists", ())
        if len(lists) != 2 or lists[0] is not self._current_mods or lists[1] is not self._subtree_mods:
            self._delta_direct_positions = {mod.mod_id: i for i, mod in enumerate(self._current_mods)}
            self._delta_subtree_positions = {mod.mod_id: i for i, mod in enumerate(self._subtree_mods)}
            self._delta_by_id = {mod.mod_id: mod for mod in self._subtree_mods}
            self._delta_lists = (self._current_mods, self._subtree_mods)
        old_mods = {mid: self._delta_by_id[mid] for mid in affected if mid in self._delta_by_id}
        # 监控可能回报已显示的修改，继续处理真实状态变化，不重复绘制相同数据。
        mods = [
            mod
            for mod in mods
            if mod.mod_id in recovering or old_mods.get(mod.mod_id) is mod or old_mods.get(mod.mod_id) != mod
        ]
        changed = {mod.mod_id: mod for mod in mods}
        affected = set(changed) | set(removed_ids)
        if not affected:
            return True

        def in_subtree(mod):
            """沿变化 Mod 的父节点链判断其是否属于当前子树，缓存父映射并防止环形遍历。"""
            node = mod.node_id or ""
            if not parent:
                return True
            # 仅检查变化 Mod 的祖先链。
            by_id = getattr(self, "_delta_node_parents", None)
            if by_id is None:
                by_id = {node["id"]: node.get("parent_id") or "" for node in self._all_characters}
                self._delta_node_parents = by_id
            seen = set()
            while node and node not in seen:
                if node == parent:
                    return True
                seen.add(node)
                node = by_id.get(node, "")
            return False

        for values, positions, belongs in (
            (self._current_mods, self._delta_direct_positions, lambda mod: (mod.node_id or "") == parent),
            (self._subtree_mods, self._delta_subtree_positions, in_subtree),
        ):
            for mid in affected:
                mod = changed.get(mid)
                index = positions.get(mid)
                if mod is not None and belongs(mod):
                    if index is None:
                        positions[mid] = len(values)
                        values.append(mod)
                    else:
                        values[index] = mod
                elif index is not None:
                    values.pop(index)
                    positions.pop(mid)
                    for offset in range(index, len(values)):
                        positions[values[offset].mod_id] = offset
        for mid in affected:
            self._delta_by_id.pop(mid, None)
        self._delta_by_id.update({mod.mod_id: mod for mod in mods if mod.mod_id in self._delta_subtree_positions})
        removed_tags = any(mid not in old_mods or old_mods[mid].info.tags for mid in removed_ids)
        if removed_tags or any(
            mod.mod_id not in old_mods
            or (old_mods[mod.mod_id].node_id, old_mods[mod.mod_id].info.tags) != (mod.node_id, mod.info.tags)
            for mod in mods
        ):
            self.refresh_tags(refresh_view=False)
        # 只移除和重新插入受影响条目，使用二分定位，避免全库重新排序。
        desired = list(self._display_items)
        for index in sorted(
            (self._display_idx_map[mid] for mid in affected if mid in self._display_idx_map), reverse=True
        ):
            desired.pop(index)
        query = tag_key(self._search_input.text().strip())
        type_filter = self._type_filter.currentData() or ""
        rating = self._age_filter.currentData() or ""
        has_tags = bool(self._tag_filter.selected_ids) and self._tag_filter.button.isEnabled()
        category_count = sum(item["type"] == "category" for item in desired)
        mods_first = self._sort_key not in _ALPHABETICAL_SORTS
        for mod in mods:
            membership = self._delta_subtree_positions if query or has_tags else self._delta_direct_positions
            if mod.mod_id not in membership or type_filter in ("category", "character"):
                continue
            if query and not any(
                query in tag_key(text or "") for text in (mod.display_name, mod.info.author, *(mod.info.tags or []))
            ):
                continue
            if rating and mod.info.age_rating.value != rating:
                continue
            if has_tags and not self._tag_filter.matches(mod.info.tags):
                continue
            lo = 0 if mods_first else category_count
            hi = len(desired) - category_count if mods_first else len(desired)
            while lo < hi:
                middle = (lo + hi) // 2
                other = desired[middle]["data"]
                _, pair = self._apply_sort([], [other, mod])
                if pair[0] is other:
                    lo = middle + 1
                else:
                    hi = middle
            desired.insert(lo, dict(id=mod.mod_id, type="mod", data=mod, name=mod.display_name))
        old_ids = [item["id"] for item in self._display_items]
        new_ids = [item["id"] for item in desired]
        scroll_bar = (
            self._table.verticalScrollBar() if self._view_mode == "list" else self._scroll_area.verticalScrollBar()
        )
        scroll_value = scroll_bar.value()
        anchor_id = old_ids[self._last_clicked_index] if 0 <= self._last_clicked_index < len(old_ids) else None
        self._vs_invalidate_prefetch()
        self._display_items[:] = desired
        self._display_idx_map = {item["id"]: row for row, item in enumerate(desired)}
        structural = old_ids != new_ids
        if structural and self._view_mode == "list":
            self._reconcile_table_rows(old_ids, affected)
        for mod in mods:
            previous = old_mods.get(mod.mod_id)
            if previous is mod:
                # 显式修改可能原地更新缓存对象，无法取得旧值时只重新绑定该项。
                previous = None
            card = self._vs_card_cache.get(mod.mod_id)
            previews_changed = (
                previous is None
                or previous.preview_images != mod.preview_images
                or previous.content_hash != mod.content_hash
                or previous.info.age_rating != mod.info.age_rating
            )
            preview_moves = {}
            if previous and previous.path and previous.path != mod.path:
                for path in previous.preview_images:
                    try:
                        moved_path = str(Path(mod.path) / Path(path).relative_to(previous.path))
                    except ValueError:
                        continue
                    if moved_path in mod.preview_images:
                        preview_moves[path] = moved_path
            if previews_changed:
                override = self._preview_override_paths.pop(mod.mod_id, None)
                if override in preview_moves:
                    self._preview_override_paths[mod.mod_id] = preview_moves[override]
                elif override in mod.preview_images:
                    self._preview_override_paths[mod.mod_id] = override
                if self._pending_reorder_mod_id == mod.mod_id:
                    self._pending_reorder_path = preview_moves.get(
                        self._pending_reorder_path, self._pending_reorder_path
                    )
            if isinstance(card, ModCard):
                if previews_changed:
                    self._vs_image_requests.pop(mod.mod_id, None)
                    cached = card._cached_pixmap
                    previous_preview = card._preview_override_path or (previous.primary_preview if previous else "")
                    card.set_mod_deferred(mod)
                    card._preview_override_path = self._preview_override_paths.get(mod.mod_id)
                    if preview_moves.get(previous_preview) == card.effective_preview_path():
                        card.set_preview_pixmap(cached)
                else:
                    card._mod = mod
                    card._set_mod_text(mod)
                    card._update_status()
            row = self._display_idx_map.get(mod.mod_id, -1)
            if self._view_mode == "list" and row >= 0:
                identity = self._table.item(row, 0)
                identity.setData(_EnabledRowDelegate.ROLE_ENABLED, mod.is_enabled)
                if row not in self._table._pending_rows:
                    if previous is None or (previous.display_name, previous.info) != (mod.display_name, mod.info):
                        self._set_table_mod_name(row, mod)
                        self._table.setItem(row, 2, QTableWidgetItem(mod.info.author or ""))
                        self._table.setItem(row, 3, QTableWidgetItem(_age_rating_text(mod.info.age_rating)))
                    self._table.refresh_mod_status(row)
            if self._selected_mod and self._selected_mod.mod_id == mod.mod_id:
                self._selected_mod = mod
                self._update_mod_panel_info(mod)
                self._refresh_panel_mod_status()
                if previous is None or (previous.path, previous.deployed_path, previous.content_hash) != (
                    mod.path,
                    mod.deployed_path,
                    mod.content_hash,
                ):
                    self._build_keys_panel()
                if previews_changed:
                    index = self._panel_preview._current_index
                    cached_path = self._panel_preview._cached_path
                    if cached_path in preview_moves:
                        self._panel_preview._cached_path = preview_moves[cached_path]
                    self._load_mod_panel_preview(
                        mod,
                        initial_index=index,
                        force_reload=previous is None or previous.content_hash != mod.content_hash,
                    )
        if self._pending_reorder_mod_id in removed_ids:
            self._preview_reorder_timer.stop()
            self._pending_reorder_mod_id = self._pending_reorder_path = ""
        if structural:
            for mid in set(old_ids) - set(new_ids):
                self._vs_image_requests.pop(mid, None)
                card = self._vs_card_cache.pop(mid, None)
                if card is not None:
                    self._dispose_vs_card(card)
                overlay = self._deploy_overlays.pop(mid, None)
                if overlay is not None:
                    overlay.deleteLater()
            self._vs_pending_pixmap_cards[:] = [
                entry for entry in self._vs_pending_pixmap_cards if entry[0] in self._display_idx_map
            ]
            self._vs_pending_prefetch[:] = [
                entry for entry in self._vs_pending_prefetch if entry[0] in self._display_idx_map
            ]
        self._prune_selection()
        self._last_clicked_index = self._display_idx_map.get(anchor_id, -1)
        self._update_card_selection()
        if self._view_mode == "list":
            self._sync_table_selection_visual()
        elif structural and (not old_ids or not new_ids):
            # 进入或离开空状态布局时没有可共享卡片。
            self._refresh_cards(self._search_input.text().strip())
        else:
            if structural:
                cols = max(1, self._vs_cols)
                self._vs_total_rows = (len(desired) + cols - 1) // cols
                height = self._vs_total_rows * self._vs_row_h + 12
                self._scroll_content.setFixedHeight(height)
            self._vs_window = None
            if structural:
                self._vs_update_visible()
            elif not self._vs_scroll_timer.isActive():
                # 已有卡片已更新，待创建卡片和预览请求在各自调度轮次处理。
                self._vs_scroll_timer.start()
        scroll_bar.setValue(scroll_value)
        self._update_filter_empty(bool(desired), self._search_input.text().strip())
        return True

    def _reconcile_table_rows(self, old_ids: list[str], affected: set[str]) -> None:
        """重新插入受影响身份，并映射延迟行缓存。"""
        table = self._table
        blocker = QSignalBlocker(table)
        was_syncing = table._syncing_rows
        table._syncing_rows = True
        pending_ids = {old_ids[row] for row in table._pending_rows}
        names = {old_ids[row]: data for row, data in table._name_data.items()}
        cells = {old_ids[row]: cell for row, cell in table._name_cells.items()}
        try:
            for row in range(len(old_ids) - 1, -1, -1):
                if old_ids[row] in affected:
                    table.removeRow(row)
            for row, entry in enumerate(self._display_items):
                mid = entry["id"]
                if mid not in affected:
                    continue
                table.insertRow(row)
                identity = QTableWidgetItem(icons.get_icon("actions/mod"), "Mod")
                identity.setData(Qt.ItemDataRole.UserRole, mid)
                identity.setData(Qt.ItemDataRole.UserRole + 1, "mod")
                identity.setData(_EnabledRowDelegate.ROLE_ENABLED, entry["data"].is_enabled)
                table.setItem(row, 0, identity)
                table.setRowHeight(row, 52)
                pending_ids.add(mid)
            table._pending_rows = {row for mid, row in self._display_idx_map.items() if mid in pending_ids}
            table._name_data = {
                self._display_idx_map[mid]: data
                for mid, data in names.items()
                if mid not in affected and mid in self._display_idx_map
            }
            table._name_cells = {
                self._display_idx_map[mid]: cell
                for mid, cell in cells.items()
                if mid not in affected and mid in self._display_idx_map
            }
            table._mod_rows = {
                entry["id"]: row for row, entry in enumerate(self._display_items) if entry["type"] == "mod"
            }
            self._table_category_rows = [
                row for row, entry in enumerate(self._display_items) if entry["type"] == "category"
            ]
        finally:
            table._syncing_rows = was_syncing
            blocker.unblock()

    def update_mods_in_place(self, mods: list[Mod]) -> None:
        """一次发布提交状态，保留卡片和选择。"""
        if self._sort_key == "status_desc" and self.apply_reconcile_delta(mods, ()):
            return
        changed = {mod.mod_id: mod for mod in mods}
        if not changed:
            return
        self._current_mods[:] = [changed.get(mod.mod_id, mod) for mod in self._current_mods]
        self._subtree_mods[:] = [changed.get(mod.mod_id, mod) for mod in self._subtree_mods]
        # 及时更新监控比较基线，避免将同一次禁用误判为第二次变化并重建按键控件。
        baseline = getattr(self, "_delta_by_id", {})
        for mod_id, mod in changed.items():
            if mod_id in baseline:
                baseline[mod_id] = mod
        for item in self._display_items:
            if item["type"] == "mod" and item["id"] in changed:
                item["data"] = changed[item["id"]]
        for mod_id, mod in changed.items():
            card = self._vs_card_cache.get(mod_id)
            if isinstance(card, ModCard):
                card._mod = mod
                card._update_status()
            row = self._display_idx_map.get(mod_id, -1)
            if 0 <= row < self._table.rowCount():
                item = self._table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == mod_id:
                    item.setData(_EnabledRowDelegate.ROLE_ENABLED, mod.is_enabled)
                    # 延迟单元格显示时读取更新后的条目。
                    if row not in self._table._pending_rows:
                        self._table.refresh_mod_status(row)
        if self._selected_mod and self._selected_mod.mod_id in changed:
            self._selected_mod = changed[self._selected_mod.mod_id]
            self._refresh_panel_mod_status()

    def update_mod_toggle_in_place(self, mod_id: str, updated_mod: Mod, disabled_ids: list[str] | None = None) -> None:
        """就地更新 Mod 卡片的启用/禁用状态，避免全量重建卡片"""
        # 更新内部 mods 列表中的对象引用
        for i, m in enumerate(self._current_mods):
            if m.mod_id == mod_id:
                self._current_mods[i] = updated_mod
                break
            if disabled_ids and m.mod_id in disabled_ids:
                # 被冲突禁用的 Mod 也需更新
                m.disable()

        # 同步 _subtree_mods
        for i, m in enumerate(self._subtree_mods):
            if m.mod_id == mod_id:
                self._subtree_mods[i] = updated_mod
            elif disabled_ids and m.mod_id in disabled_ids:
                m.disable()

        # 同步 _display_items
        for item in self._display_items:
            if item["type"] == "mod" and item["id"] == mod_id:
                item["data"] = updated_mod
            elif disabled_ids and item["type"] == "mod" and item["id"] in disabled_ids:
                item["data"].disable()

        # 仅更新受影响的卡片视觉状态
        affected_ids = {mod_id}
        if disabled_ids:
            affected_ids.update(disabled_ids)

        for aid in affected_ids:
            card = self._vs_card_cache.get(aid)
            if card and isinstance(card, ModCard):
                if aid == mod_id:
                    card._mod = updated_mod
                card._update_status()

            row = self._display_idx_map.get(aid, -1)
            if row >= 0 and row < self._table.rowCount():
                item = self._table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole + 1) == "mod":
                    target_mod = updated_mod if aid == mod_id else self._mod_by_id(aid)
                    if target_mod:
                        self._set_table_mod_status(row, target_mod)

        # 如果当前面板显示的 Mod 是被切换的 Mod，更新面板
        if self._selected_mod and self._selected_mod.mod_id == mod_id:
            self._selected_mod = updated_mod
            self._refresh_panel_mod_status()

    # 部署反馈（按 mod_id 管理，卡片 / 列表共用同一份进度）

    def begin_deploy_overlay(self, mod_id: str) -> None:
        """立即显示启用反馈；收到真实进度前使用不定进度，避免长时间停在 0%。

        进度/结束信号来自后台线程 → 主线程槽。卡片可能因滚动被回收，故 overlay 不长期
        持有卡片引用：状态存 _deploying_frac，_attach_deploy_overlay 每次按 mod_id 现取卡片。
        """
        self._deploying_frac[mod_id] = 0.0
        self._table.set_deploy_progress(mod_id, None)
        self._attach_deploy_overlay(mod_id)
        self._refresh_panel_mod_status()
        self._refresh_deploy_summary()

    def update_deploy_overlay(self, mod_id: str, frac: float) -> None:
        """后台线程 progress 信号同时更新卡片蒙层与列表行。"""
        if mod_id not in self._deploying_frac:
            return
        self._deploying_frac[mod_id] = max(0.0, min(1.0, frac))
        progress = self._deploying_frac[mod_id]
        self._table.set_deploy_progress(mod_id, progress if progress > 0 else None)
        self._refresh_panel_mod_status()
        self._refresh_deploy_summary()
        ov = self._deploy_overlays.get(mod_id)
        if ov is not None:
            try:
                ov.set_progress(self._deploying_frac[mod_id])
            except RuntimeError:
                self._deploy_overlays.pop(mod_id, None)  # C++ 侧已析构 → 丢弃引用

    def finish_deploy_overlay(
        self,
        mod_id: str,
        *,
        success: bool = True,
        error: str = "",
    ) -> None:
        """提交结果后撤下进度；成功短暂亮绿，失败保留说明与重试入口。"""
        self._deploying_frac.pop(mod_id, None)
        ov = self._deploy_overlays.pop(mod_id, None)
        if ov is not None:
            try:
                ov.deleteLater()
            except RuntimeError:
                pass
        self._table.finish_deploy(mod_id, success=success, error=error)
        self._refresh_panel_mod_status()
        self._refresh_deploy_summary()

    def _refresh_deploy_summary(self) -> None:
        """即使活动行被筛选掉，底部仍能确认后台正在启用 Mod。"""
        count = len(self._deploying_frac)
        root_error = self._deploy_status_label.property("deploymentRootError") or ""
        text = tr("正在启用 {count} 个 Mod", count=count) if count else root_error
        if count == 1:
            progress = next(iter(self._deploying_frac.values()))
            if progress >= 1:
                text += tr(" · 正在完成")
            elif progress > 0:
                text += f" · {int(progress * 100)}%"
        self._deploy_status_label.setText(text)
        color = "danger" if root_error and not count else "accent"
        style = f"color: {theme.color(color)}; font-size: 11px;"
        if self._deploy_status_label.styleSheet() != style:
            self._deploy_status_label.setStyleSheet(style)
        self._deploy_status_label.setVisible(bool(count or root_error))

    def _refresh_panel_mod_status(self) -> None:
        """选择另一行再返回、刷新或换肤时，详情也能恢复当前部署状态。"""
        mod = self._selected_mod
        if not mod or self._panel_mode != "mod":
            self._panel_deploy_hint.hide()
            return
        busy = mod.mod_id in self._deploying_frac
        error = self._table.deploy_error(mod.mod_id)
        text = self._table.status_text(mod.mod_id, mod.is_enabled)
        if not busy and not error:
            text = ("● " if mod.is_enabled else "○ ") + text
        token = "accent" if busy else "danger" if error else "success" if mod.is_enabled else "text.tertiary"
        style = f"color: {theme.color(token)}; font-weight: {'bold' if mod.is_enabled else 'normal'};"
        self._panel_status_label.setText(text)
        self._panel_status_label.setToolTip(error)
        if self._panel_status_label.styleSheet() != style:
            self._panel_status_label.setStyleSheet(style)
        self._panel_toggle_btn.setEnabled(not busy)
        self._panel_toggle_btn.setText(
            tr("正在启用…") if busy else tr("重试启用") if error else tr("禁用") if mod.is_enabled else tr("启用")
        )
        icons.apply_icon(
            self._panel_toggle_btn,
            "actions/refresh" if busy or error else "status/disabled" if mod.is_enabled else "status/enabled",
        )
        hint = error or (tr("正在处理 Mod 文件，可以继续浏览其他项目。") if busy else "")
        self._panel_deploy_hint.setText(hint)
        hint_style = f"color: {theme.color('danger' if error else 'text.tertiary')}; font-size: 12px;"
        if self._panel_deploy_hint.styleSheet() != hint_style:
            self._panel_deploy_hint.setStyleSheet(hint_style)
        self._panel_deploy_hint.setVisible(bool(hint))
        self._variant_panel.set_busy(busy)

    def _attach_deploy_overlay(self, mod_id: str) -> None:
        """为可见 Mod 卡片创建或复用部署进度覆盖层。

        覆盖层随卡片销毁，虚拟滚动重新创建卡片时恢复对应进度。
        """
        if mod_id not in self._deploying_frac:
            return
        card = self._vs_card_cache.get(mod_id)
        if card is None or not isinstance(card, ModCard):
            return  # 卡片当前不可见（表格视图/滚出缓冲区）→ 仅保留 frac，滚回时再挂
        ov = self._deploy_overlays.get(mod_id)
        try:
            if ov is None or ov.parentWidget() is not card:
                # 卡片实例已换（回收后重建）→ 丢旧的、在新卡片上重建
                if ov is not None:
                    try:
                        ov.deleteLater()
                    except RuntimeError:
                        pass
                from src.ui.widgets.deploy_overlay import DeployOverlay

                ov = DeployOverlay(card)
                self._deploy_overlays[mod_id] = ov
            ov.set_progress(self._deploying_frac[mod_id])
            # cover 同时提升并显示初始隐藏的部署覆盖层。
            ov.cover()
        except RuntimeError:
            self._deploy_overlays.pop(mod_id, None)

    def _build_children_map(self) -> None:
        """根据 parent_id 构建父子映射"""
        self._children_map = {}
        for c in self._all_characters:
            parent = c.get("parent_id") or ""
            if parent not in self._children_map:
                self._children_map[parent] = []
            self._children_map[parent].append(c)

    def _refresh_current_level(self) -> None:
        """刷新当前导航层级的显示"""
        self._characters = self._children_map.get(self._current_parent_id, [])
        self._update_breadcrumb()
        if not self._mods_loading:
            self._card_size_slider.setVisible(self._view_mode == "grid")
            self._card_size_label.setVisible(self._view_mode == "grid")
        if self._view_mode == "grid":
            self._refresh_cards(self._search_input.text().strip())
        else:
            self._refresh_table(self._search_input.text().strip())

    def _build_display_items(self) -> None:
        """构建混合显示列表，Mod 专用排序将 Mod 分组置于类别之前。"""
        self._display_items = []
        self._display_idx_map = {}
        sorted_cats = list(self._characters)
        sorted_mods = list(self._current_mods)
        sorted_cats, sorted_mods = self._apply_sort(sorted_cats, sorted_mods)

        items_order: list[tuple[str, object]] = []
        if self._sort_key not in _ALPHABETICAL_SORTS:
            items_order.extend([("mod", mod) for mod in sorted_mods])
            items_order.extend([("category", cat) for cat in sorted_cats])
        else:
            items_order.extend([("category", cat) for cat in sorted_cats])
            items_order.extend([("mod", mod) for mod in sorted_mods])

        for item_type, payload in items_order:
            idx = len(self._display_items)
            if item_type == "category":
                cat = payload
                item_id = cat.get("id", "")
                self._display_items.append(
                    {
                        "id": item_id,
                        "type": "category",
                        "name": cat.get("name", ""),
                        "data": cat,
                    }
                )
                self._display_idx_map[item_id] = idx
            else:
                mod = payload
                self._display_items.append(
                    {
                        "id": mod.mod_id,
                        "type": "mod",
                        "name": mod.display_name,
                        "data": mod,
                    }
                )
                self._display_idx_map[mod.mod_id] = idx

    def _set_table_mod_status(self, row: int, mod: Mod) -> None:
        """更新真实启用态；状态胶囊优先呈现该 Mod 尚未结束的部署反馈。"""
        type_item = self._table.item(row, 0)
        if type_item is not None:
            type_item.setData(_EnabledRowDelegate.ROLE_ENABLED, bool(mod.is_enabled))
        self._table.refresh_mod_status(row)

    def _flush_pending_preview_reorder(self) -> None:
        """立刻把待写的「主图」落库（去抖定时器到点 / 离开 Mod / 关程序时都会调）。"""
        self._preview_reorder_timer.stop()
        mod_id, img_path = self._pending_reorder_mod_id, self._pending_reorder_path
        self._pending_reorder_mod_id = ""
        self._pending_reorder_path = ""
        if mod_id and img_path:
            self.mod_preview_reorder_requested.emit(mod_id, img_path)

    def _navigate_to(self, parent_id: str) -> None:
        """导航到指定类别层级"""
        self._flush_pending_preview_reorder()
        self._request_view_position(parent_id=parent_id)
        self.begin_loading()
        self._directory_intro_pending = anim.is_enabled()
        self._tag_filter.close_popup()
        self._tag_filter.clear(emit=False)
        self._current_parent_id = parent_id
        # 清空选择和Mod面板
        self._selected_ids.clear()
        self._selected_mod = None
        self._selected_category = None
        self._last_clicked_index = -1
        self._hide_panel()
        # 通知主窗口加载此层级的Mod → set_current_mods → _refresh_current_level
        self.category_navigated.emit(parent_id)

    def _apply_breadcrumb_style(self) -> None:
        """地址/面包屑栏底色（随主题）。抽出以便换肤时重涂。"""
        self._breadcrumb_frame.setStyleSheet(
            f"QFrame#breadcrumbFrame {{ background-color: {theme.surface_color('bg.card', 'panel')}; "
            f"border: 1px solid {theme.color('stroke.divider')}; border-radius: 4px; }}"
        )

    def _apply_overview_table_style(self) -> None:
        """列表视图表格内联底色（随主题/背景图档位）。抽出以便换肤时重涂。"""
        if theme.is_background_active():
            list_alt = "transparent"
        else:
            list_alt = theme.surface_color("bg.subtle", "list")
        self._table.setStyleSheet(
            f"QTableWidget {{ background-color: {theme.surface_color('bg.card', 'list')};"
            f" alternate-background-color: {list_alt}; }}"
        )

    def _apply_panel_text_style(self) -> None:
        """按表面颜色和语义字号刷新详情作者、描述及路径文本。"""
        ink = theme.surface_text_color("text.secondary")
        for label, role in (
            (self._panel_author_label, "size_secondary"),
            (self._panel_desc_label, "size_body"),
            (self._panel_path_label, "size_caption"),
        ):
            label.setStyleSheet(f"color: {ink}; font-size: {theme.font(role)}px;")

    def _apply_detail_style(self) -> None:
        """统一设置详情标签页、字段标题和空状态样式，保持各滚动区域背景透明。"""
        ink = theme.surface_text_color("text.secondary")
        self._panel_details.setStyleSheet(
            "#modPreviewDetails, #modPreviewHeader, #modPreviewTabs,"
            " #modPreviewTabs QTabBar, #modPreviewTabs QStackedWidget,"
            " #modPreviewTabs QScrollArea, #modPreviewTabs QScrollArea > QWidget { background: transparent; }"
            "#modPreviewTabs::pane { border: none; background: transparent; }"
            "#modPreviewTabs QTabBar::tab { padding: 8px 10px; }"
            f"#previewVariantName, #previewFactCaption {{ color: {ink}; font-size: 12px; }}"
            f"#previewSectionTitle {{ color: {theme.surface_text_color('text.primary')};"
            f" border-top: 1px solid {theme.color('stroke.divider')}; font-size: 12px;"
            " font-weight: 600; padding-top: 10px; margin-top: 4px; background: transparent; }"
            "#modPreviewDetails QLabel { background: transparent; }"
        )
        self._panel_keys_host.setStyleSheet("background: transparent;")
        self._panel_source_label.setStyleSheet(f"color: {ink}; font-size: 11px;")
        self._panel_keys_empty.setStyleSheet(f"color: {ink}; padding: 20px 6px; font-size: 12px;")
        for label in (self._panel_version_label, self._panel_category_label, self._panel_size_label):
            label.setStyleSheet(f"color: {theme.surface_text_color('text.primary')}; font-size: 12px;")

    def _detail_scrollbar(self, index: int):
        """按信息、按键或变体标签索引返回对应的纵向滚动条。"""
        return (self._panel_info_scroll, self._panel_keys_scroll, self._variant_panel.scroll_area)[
            index
        ].verticalScrollBar()

    def _remember_detail_scroll(self) -> None:
        """按 Mod 与标签记录位置，只保存已绑定当前 Mod 的按键和变体页面滚动位置。"""
        if self._detail_mod_id:
            for index in range(3):
                if index == 1 and self._keys_panel_mod_id != self._detail_mod_id:
                    continue
                if index == 2 and self._variant_panel.mod_id != self._detail_mod_id:
                    continue
                self._detail_scroll_positions[(self._detail_mod_id, index)] = self._detail_scrollbar(index).value()

    def _restore_detail_scroll(self, mod_id: str, index: int) -> None:
        """仅在详情仍绑定指定 Mod 时恢复对应标签页的滚动位置。"""
        if self._panel_mode == "mod" and self._detail_mod_id == mod_id:
            self._detail_scrollbar(index).setValue(self._detail_scroll_positions.get((mod_id, index), 0))

    def _on_detail_tab_changed(self, index: int) -> None:
        """保存离开标签的位置并恢复新标签位置，在 Mod 模式下记住最后使用的详情标签。"""
        if self._detail_mod_id:
            self._detail_scroll_positions[(self._detail_mod_id, self._last_detail_tab)] = self._detail_scrollbar(
                self._last_detail_tab
            ).value()
            self._detail_scrollbar(index).setValue(self._detail_scroll_positions.get((self._detail_mod_id, index), 0))
        self._last_detail_tab = index
        if getattr(self, "_panel_mode", "none") == "mod":
            self._mod_detail_tab = index

    def _clear_variant_context(self) -> None:
        """保存详情位置并清除 Mod 与变体绑定，隐藏相关标签并通知宿主上下文已清空。"""
        self._remember_detail_scroll()
        self._detail_mod_id = ""
        self._variant_panel.clear_context()
        self._panel_variant_label.hide()
        self._detail_tabs.hide()
        self.variant_context_changed.emit("")

    def set_variant_state(self, mod_id: str, state: dict, variants, *, reference_counts=None) -> bool:
        """只刷新所选 Mod 的变体标签页，不更新按键和图片。"""
        if self._panel_mode != "mod" or self._selected_mod is None or self._selected_mod.mod_id != mod_id:
            return False
        changed_mod = self._variant_panel.mod_id != mod_id
        self._variant_panel.set_state(state, variants, reference_counts=reference_counts)
        if changed_mod:
            self._restore_detail_scroll(mod_id, 2)
            call_later(0, self, lambda: self._restore_detail_scroll(mod_id, 2))
        self._detail_tabs.setTabText(2, tr("变体 {count}", count=len(variants)))
        selected = state.get("selected_variant_id")
        name = (
            tr("Mod 默认")
            if selected is None
            else next((item.name for item in variants if item.variant_id == selected), tr("已缺失的变体"))
        )
        self._panel_variant_label.setText(f"· {tr('当前') if state.get('enabled') else tr('下次使用')}：{name}")
        self._panel_variant_label.setVisible(bool(variants or state.get("variable_count")))
        return True

    def retheme(self) -> None:
        """主题切换后重着色本页内联样式控件（地址栏 / 列表表格）。

        卡片会跨刷新复用，当前可见卡片的内联颜色也必须在这里更新。
        池中的类别卡片则在重新绑定时更新，避免遍历整库。
        """
        try:
            if self._batch_add_dialog is not None:
                self.prepare_batch_add_dialog()
            self._apply_breadcrumb_style()
            self._breadcrumb.retheme()
            self._apply_overview_table_style()
            self._apply_panel_text_style()
            self._tag_filter.retheme()
            if not self._panel_preview._images:
                self._panel_preview._style_placeholder()
            if self._selected_mod and self._panel_mode == "mod":
                self._panel_tags.set_tags(tags_for_names(self._selected_mod.info.tags, self._tag_catalog_by_name))
            for card in self._vs_card_cache.values():
                if isinstance(card, ModCard):
                    card.set_tag_catalog(self._tag_catalog_by_name)
                elif isinstance(card, CharacterCard):
                    card.retheme()
            # 状态列的图标/前景/底色是按主题算出来的内联值，而表格不随换肤重建
            # （只有卡片会），故这里逐行重写一遍。
            for row, entry in enumerate(self._display_items):
                if entry.get("type") == "mod" and row < self._table.rowCount():
                    self._set_table_mod_status(row, entry["data"])
                    self._set_table_mod_name(row, entry["data"])
            self._apply_detail_style()
            if self._panel_mode == "mod" and self._selected_mod is not None:
                self._update_mod_panel_source(self._selected_mod)
            self._variant_panel.retheme()
            self._refresh_panel_mod_status()
            self._refresh_deploy_summary()
        except Exception:
            pass

    def _update_breadcrumb(self) -> None:
        """向面包屑控件同步当前导航路径。"""
        self._breadcrumb.set_items(t("全部"), self._get_path_to_current())

    def _get_path_to_current(self) -> list[tuple[str, str]]:
        """获取从根到当前层级的路径 [(id, name), ...]"""
        path = []
        current = self._current_parent_id
        while current:
            for c in self._all_characters:
                if c["id"] == current:
                    path.append((c["id"], c.get("name", "")))
                    current = c.get("parent_id", "")
                    break
            else:
                break
        path.reverse()
        return path

    def get_current_path(self) -> list[tuple[str, str]]:
        """暴露当前导航路径 (供 MainWindow 使用)"""
        return self._get_path_to_current()

    def set_updatable(self, mod_ids: set[str]) -> None:
        """设置"有更新"的本地 mod id 集合，并刷新可见卡片角标。"""
        self._updatable_ids = set(mod_ids or set())
        for item_id, card in self._vs_card_cache.items():
            if isinstance(card, ModCard):
                card.set_update_available(item_id in self._updatable_ids)
        if self._view_mode == "list":
            for row, item in enumerate(self._display_items):
                if item["type"] == "mod":
                    self._set_table_mod_name(row, item["data"])

    def set_update_status_text(self, text: str) -> None:
        """在状态栏显示更新检查进度或结果。"""
        if hasattr(self, "_status_label") and self._status_label is not None:
            self._status_label.setText(text)

    def _dispose_vs_card(self, card) -> None:
        """停止卡片动画和加载占位后延迟销毁控件。

        先移除图形效果及共享动画驱动注册，防止异步绘制访问已销毁的 Qt 对象。
        """
        try:
            for attr in ("_move_anim", "_geom_anim"):
                a = getattr(card, attr, None)
                if a is not None:
                    a.stop()
            if card.graphicsEffect() is not None:
                card.setGraphicsEffect(None)  # 同步删除特效 + 让其上的动画 QPointer 置空
            lbl = getattr(card, "_preview_label", None)
            stop = getattr(lbl, "_stop_loading", None)
            if callable(stop):
                stop()  # 停止加载占位并注销共享动画驱动。
        except RuntimeError:
            pass
        card.hide()
        card.setParent(None)
        card.deleteLater()

    def _refresh_cards(self, filter_text: str = "") -> None:
        """虚拟滚动版: 只计算布局参数, 按需创建可视区域的卡片 Widget"""
        if self._mods_loading:
            return
        view, position = self._prepare_view_position(filter_text)
        self._applying_position = True
        try:
            self._rebuild_cards(filter_text, view, position)
        finally:
            self._applying_position = False

    def _rebuild_cards(self, filter_text: str, view: _PresentedOverviewView, position: int) -> None:
        """撤销旧图片绑定并回收卡片，按筛选与排序建立混合索引，再恢复滚动位置并渐进绑定可见内容。"""
        self._end_card_scale()
        if self._card_reveal.is_active():
            self._cancel_directory_intro()
        self._vs_scroll_timer.stop()
        self._vs_population_timer.stop()
        self._vs_pixmap_timer.setInterval(self._VS_PIXMAP_INTERVAL_MS)
        self._fresh_population = False
        self._vs_population_pending = False
        # 新视图使旧图片绑定失效，取消在途加载；保留滚动区拥有的控件供后续复用。
        self._vs_pixmap_timer.stop()
        self._vs_pending_pixmap_cards.clear()
        self._vs_invalidate_prefetch()
        self._pixmap_worker.clear_queue()

        # 将旧绑定归还有界视口控件池。
        for card in self._vs_card_cache.values():
            self._vs_card_pool.release(card)
        self._vs_card_cache.clear()
        self._vs_window = None
        self._vs_image_requests.clear()
        for overlay in self._deploy_overlays.values():
            overlay.hide()
            overlay.deleteLater()
        self._deploy_overlays.clear()
        # 清空 grid_layout（仅用于空状态提示）
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 过滤当前目录内容。
        filtered_cats, filtered_mods = self._filtered_content(filter_text)

        # 构建混合显示条目及索引映射。
        self._display_items = []
        self._display_idx_map = {}
        items_order: list[tuple[str, object]] = []
        if self._sort_key not in _ALPHABETICAL_SORTS:
            items_order.extend([("mod", mod) for mod in filtered_mods])
            items_order.extend([("category", cat) for cat in filtered_cats])
        else:
            items_order.extend([("category", cat) for cat in filtered_cats])
            items_order.extend([("mod", mod) for mod in filtered_mods])

        for item_type, payload in items_order:
            idx = len(self._display_items)
            if item_type == "category":
                cat = payload
                item_id = cat.get("id", "")
                self._display_items.append({"id": item_id, "type": "category", "data": cat})
                self._display_idx_map[item_id] = idx
            else:
                mod = payload
                self._display_items.append({"id": mod.mod_id, "type": "mod", "data": mod})
                self._display_idx_map[mod.mod_id] = idx

        # 根据目录和筛选条件处理空状态。
        self._prune_selection()
        if self._update_filter_empty(bool(self._display_items), filter_text):
            self._scroll_content.setMinimumHeight(0)
            self._scroll_content.setMaximumHeight(16777215)
            self._commit_view_position(view, 0, grid_height=0)
            self._update_status_bar_selection()
            return
        if not self._display_items:
            empty_container = QWidget()
            empty_layout = QVBoxLayout(empty_container)
            empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.setSpacing(12)

            if self._current_parent_id:
                empty_label = QLabel(tr("当前类别为空，可以新建子类别或拖拽导入 Mod"))
                empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 14px;")
                empty_layout.addWidget(empty_label)

                btn_row = QHBoxLayout()
                btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
                btn_row.setSpacing(12)

                add_sub_btn = QPushButton(tr("+ 新建子类别"))
                icons.apply_icon(add_sub_btn, "actions/new-folder")
                add_sub_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                add_sub_btn.clicked.connect(self._on_add_character)
                btn_row.addWidget(add_sub_btn)

                import_btn = QPushButton(tr("导入 Mod"))
                icons.apply_icon(import_btn, "actions/mod")
                import_btn.setIconSize(QSize(16, 16))
                import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                import_btn.clicked.connect(self.mod_import_requested.emit)
                btn_row.addWidget(import_btn)

                empty_layout.addLayout(btn_row)
            else:
                # 根目录不直接接收 Mod，提供显式创建类别入口以引导后续导入。
                empty_label = QLabel(t("暂无类别\n点击下方「+ 新建类别」按钮添加，或导入分类预设"))
                empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 14px;")
                empty_layout.addWidget(empty_label)

                btn_row = QHBoxLayout()
                btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
                btn_row.setSpacing(12)
                add_cat_btn = QPushButton(t("+ 新建类别"))
                icons.apply_icon(add_cat_btn, "actions/new-folder")
                add_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                add_cat_btn.clicked.connect(self._on_add_character)
                btn_row.addWidget(add_cat_btn)
                empty_layout.addLayout(btn_row)

            empty_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid_layout.addWidget(empty_container, 0, 0, Qt.AlignmentFlag.AlignCenter)
            self._status_label.setText(t("共 {count} 个类别，{mod_count} 个 Mod", count=0, mod_count=0))
            self._scroll_content.setMinimumHeight(0)
            self._scroll_content.setMaximumHeight(16777215)
            self._commit_view_position(view, 0, grid_height=0)
            self._cancel_directory_intro()
            return

        # 计算虚拟滚动布局参数。
        scaled_w = int(CharacterCard.CARD_WIDTH * self._card_scale / 100)
        scaled_h = int(CharacterCard.CARD_HEIGHT * self._card_scale / 100)
        avail_w = (
            self._main_splitter.widget(0).width() if self._main_splitter.widget(0).width() > 100 else self.width() - 48
        )
        cols = max(1, (avail_w - 48) // (scaled_w + 12))

        self._vs_card_w = scaled_w
        self._vs_card_h = scaled_h
        self._vs_row_h = scaled_h + 12
        self._vs_cols = cols
        self._vs_total_rows = (len(self._display_items) + cols - 1) // cols
        total_h = self._vs_total_rows * self._vs_row_h + 12
        self._scroll_content.setMinimumHeight(total_h)
        self._scroll_content.setMaximumHeight(total_h)
        self._last_cards_per_row = cols

        self._update_status_bar_selection()

        # 恢复位置并开始绑定可见区域。
        self._commit_view_position(view, position, grid_height=total_h)
        # 首帧前提交已有缓存的可见卡片，缓冲行和新绑定继续分时处理。
        self._vs_population_anchor_value = self._scroll_area.verticalScrollBar().value()
        self._fresh_population = True
        self._vs_update_visible()

    # 虚拟滚动。

    def _vs_visible_rows(self, buffer_rows: int | None = None) -> tuple[int, int, int, int]:
        """返回当前滚动位置对应的行和索引范围。

        结果为 (first_row, last_row, first_idx, last_idx)，供可视更新和重新布局
        共同使用，确保离屏卡片也能按一致的范围重新定位。
        """
        if buffer_rows is None:
            buffer_rows = self._VS_SCROLL_BUFFER_ROWS
        buffer_rows = max(0, int(buffer_rows))
        scroll_y = self._scroll_area.verticalScrollBar().value()
        viewport_h = self._scroll_area.viewport().height()
        row_h = self._vs_row_h if self._vs_row_h > 0 else 1
        cols = self._vs_cols if self._vs_cols > 0 else 1
        first_row = max(0, scroll_y // row_h - buffer_rows)
        # 两端额外行覆盖部分可见区域，同一行内滚动不重建控件窗口。
        last_row = min(self._vs_total_rows - 1, scroll_y // row_h + (viewport_h + row_h - 1) // row_h + buffer_rows)
        first_idx = first_row * cols
        last_idx = min(len(self._display_items) - 1, (last_row + 1) * cols - 1)
        return first_row, last_row, first_idx, last_idx

    def _vs_update_visible(self) -> None:
        """根据当前滚动位置，创建/显示可视区域内的卡片，隐藏/回收远离区域的卡片。
        已有图片立即复用，缺失图片按小批次后台加载。
        """
        if self._mods_loading:
            return
        if self._view_mode != "grid" or not self._display_items:
            self._vs_population_timer.stop()
            self._fresh_population = False
            self._vs_population_pending = False
            return

        initial_population = bool(getattr(self, "_fresh_population", False))
        cols = self._vs_cols if self._vs_cols > 0 else 1
        # 首屏只预建 1 行缓冲，后续滚动再使用完整缓冲。大窗口下每一行可能有十几张卡，
        # 若首屏直接构造 5 行缓冲，虚拟滚动反而会制造 100~300ms 的同步加载瓶颈。
        buffer_rows = self._VS_INITIAL_BUFFER_ROWS if initial_population else self._VS_SCROLL_BUFFER_ROWS
        first_row, last_row, first_idx, last_idx = self._vs_visible_rows(buffer_rows)

        window = (first_idx, last_idx, cols, self._vs_card_w, self._vs_card_h, self._scroll_content.devicePixelRatioF())
        if (
            window == self._vs_window
            and not initial_population
            and not self._vs_population_pending
            and not getattr(self, "_reflowing", False)
        ):
            # 即使行未变，策略或缓存变化也可能需要重新处理。
            self._vs_schedule_pixmaps()
            return
        if window != self._vs_window:
            self._vs_invalidate_prefetch()
            # 重定向已有图片工作，最多提交八个解码任务，避免跳转积压过期请求。
            self._vs_pending_pixmap_cards.clear()
            self._vs_image_requests = {
                item_id: (request_id, None, path)
                for item_id, (request_id, _card, path) in self._vs_image_requests.items()
            }
            for card in self._vs_card_cache.values():
                card._preview_requested = False
        self._vs_window = window
        visible_ids = {self._display_items[i]["id"] for i in range(first_idx, last_idx + 1)}
        for item_id in list(self._vs_card_cache):
            if item_id in visible_ids:
                continue
            card = self._vs_card_cache.pop(item_id)
            overlay = self._deploy_overlays.pop(item_id, None)
            if overlay is not None:
                overlay.hide()
                overlay.deleteLater()
            self._vs_card_pool.release(card)

        spacing = 12
        x_pad = 6  # 左侧内边距
        new_cards: list[tuple[str, QFrame]] = []
        population_remaining = False
        started = perf_counter()
        deadline = started + self._VS_SCROLL_CREATION_BUDGET_S
        reuse_deadline = started + self._VS_INITIAL_REUSE_BUDGET_S
        reuse_visible = initial_population
        scroll_y = self._scroll_area.verticalScrollBar().value()
        visible_first = scroll_y // self._vs_row_h * cols
        visible_last = ((scroll_y + self._scroll_area.viewport().height() - 1) // self._vs_row_h + 1) * cols - 1

        def index_priority(index):
            """优先处理实际可见条目，其余条目按距可见区的距离排序。"""
            visible = visible_first <= index <= visible_last
            return not visible, index if visible else min(abs(index - visible_first), abs(index - visible_last))

        # 当前窗口中尚未获得预览图的卡片。
        needs_pixmap: list[tuple[str, QFrame]] = []

        # 创建/定位可视区域内的卡片
        indices = range(first_idx, last_idx + 1)
        indices = sorted(indices, key=index_priority)
        for i in indices:
            item = self._display_items[i]
            item_id = item["id"]
            row = i // cols
            col = i % cols
            x = x_pad + col * (self._vs_card_w + spacing)
            y = spacing // 2 + row * self._vs_row_h

            card = self._vs_card_cache.get(item_id)
            is_existing = card is not None
            if card is None:
                try_reuse = reuse_visible and visible_first <= i <= visible_last
                binding_deadline = reuse_deadline if try_reuse else deadline
                if new_cards and (
                    (initial_population and len(new_cards) >= self._VS_INITIAL_MAX_BINDINGS)
                    or perf_counter() >= binding_deadline
                ):
                    population_remaining = True
                    continue
                if try_reuse:
                    # 创建预算耗尽后仅使用空闲控件，不开始耗时构造。
                    card_type = CharacterCard if item["type"] == "category" else ModCard
                    pooled_card = self._vs_card_pool.take(card_type)
                    if pooled_card is not None:
                        binding_started = perf_counter()
                        card = self._vs_create_card(item, defer_pixmap=True, pooled_card=pooled_card)
                        reuse_visible = (
                            card._preview_resolved
                            or perf_counter() - binding_started < self._VS_SCROLL_CREATION_BUDGET_S
                        )
                    else:
                        reuse_visible = False
                if card is None:
                    if new_cards and perf_counter() >= deadline:
                        population_remaining = True
                        continue
                    card = self._vs_create_card(item, defer_pixmap=True)
                self._vs_card_cache[item_id] = card
                new_cards.append((item_id, card))
            if (
                not card._preview_resolved
                and not card._preview_requested
                and not (self._card_scale_active and card._cached_pixmap is not None)
            ):
                card._preview_requested = True
                request = self._vs_image_requests.get(item_id)
                if request is not None and request[2] == self._vs_item_preview_path(item):
                    self._vs_image_requests[item_id] = (request[0], card, request[2])
                else:
                    needs_pixmap.append((item_id, card))
            # 重排（列数变化触发的 _vs_relayout）时，让**已存在**卡片从旧格位滑到新格位；
            # 新建卡片 / 普通刷新 / 滚动则直接定位（动画只在 _reflowing 期对老卡片生效）。
            if is_existing and getattr(self, "_reflowing", False):
                anim.animate_move(card, QPoint(x, y))
            else:
                if card.x() != x or card.y() != y:
                    card.move(x, y)
            if not is_existing and self._directory_intro_pending:
                self._reveal_directory_cards([(item_id, card)], visible_first, visible_last)
            if card.isHidden():
                if not is_existing and visible_first <= i <= visible_last:
                    # 隐藏复用控件尚未收到尺寸事件，只处理变化内容的布局，避免首次捕获重复布局和绘制。
                    card.layout().activate()
                    card._preview_container.layout().activate()
                    if isinstance(card, ModCard):
                        card._info_widget.layout().activate()
                        card._tag_row.layout().activate()
                card.show()
                # 激活布局后立即投递当前卡片的布局事件，避免下一轮丢弃首张像素图。
                QApplication.sendPostedEvents(card, QEvent.Type.LayoutRequest)
            if item_id in self._deploying_frac:
                self._attach_deploy_overlay(item_id)

        self._vs_pending_pixmap_cards.extend(needs_pixmap)

        def image_priority(entry):
            """将图片任务映射为显示索引，沿用可见区域优先的加载顺序。"""
            index = self._display_idx_map.get(entry[0], last_idx + 1)
            return index_priority(index)

        self._vs_pending_pixmap_cards.sort(key=image_priority)
        self._vs_population_pending = population_remaining
        self._vs_schedule_pixmaps()

        if population_remaining:
            interval = self._VS_INITIAL_BATCH_INTERVAL_MS if initial_population else self._vs_scroll_timer.interval()
            if not self._vs_population_timer.isActive() or self._vs_population_timer.interval() != interval:
                self._vs_population_timer.start(interval)
        else:
            self._fresh_population = False
            self._vs_population_timer.stop()
            if initial_population:
                self._scroll_edge_fade.refresh_later()
        self._scroll_edge_fade.refresh()

        if self._directory_intro_pending:
            if not population_remaining:
                self._directory_intro_pending = False

    def _reveal_directory_cards(self, cards, first: int, last: int) -> None:
        """仅为可见页面中位于指定索引范围且与视口相交的卡片安排出现效果。"""
        if not anim.is_enabled() or not self.isVisible():
            return
        viewport = self._scroll_area.viewport().rect().translated(0, self._scroll_area.verticalScrollBar().value())
        for item_id, card in cards:
            index = self._display_idx_map.get(item_id, -1)
            if first <= index <= last and card.geometry().intersects(viewport):
                self._card_reveal.add(card, item_id)

    def _cancel_directory_intro(self) -> None:
        """清除目录首次呈现标记，并取消待执行的卡片出现效果。"""
        self._directory_intro_pending = False
        self._card_reveal.cancel()

    def _vs_item_preview_path(self, item: dict) -> str:
        """按项目类型与成人显示策略取得预览路径，Mod 优先使用仍存在的临时主图覆盖路径。"""
        if item["type"] == "category":
            return str(item["data"].get("thumbnail") or "")
        mod = item["data"]
        if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
            return ""
        override = self._preview_override_paths.get(mod.mod_id)
        return str(override if override and fs.exists(override) else mod.primary_preview or "")

    def _vs_restore_preview(self, card: QFrame) -> bool:
        """从缓存恢复已合成预览，合成未命中时可应用临时图片，并返回是否完成恢复。"""
        if card._preview_resolved:
            return True
        path = card.effective_preview_path() if isinstance(card, ModCard) else card._data.get("thumbnail", "")
        layout = self._vs_preview_layout(card)
        image = cached_preview(path, _PixmapLoadWorker.cache_size(layout)) if path else None
        if image is None:
            return False
        rendered = cached_rendered_preview(image, layout)
        if rendered is None:
            if self._card_scale_active or card._cached_pixmap is not None:
                label = card._preview_label if isinstance(card, ModCard) else card._thumb
                previous = label.pixmap()
                self._vs_apply_temporary_preview(
                    card, image, previous.toImage() if previous is not None and not previous.isNull() else None
                )
            return False
        self._vs_apply_preview(card, image, rendered=rendered)
        return True

    def _vs_apply_temporary_preview(self, card: QFrame, image, rendered=None) -> None:
        """新尺寸图片准备好之前保留已解码像素。"""
        layout = self._vs_preview_layout(card)
        preview = QPixmap.fromImage(rendered if rendered is not None else image)
        preview = preview.scaled(
            round(layout.width * layout.dpr),
            round(layout.height * layout.dpr),
            Qt.AspectRatioMode.IgnoreAspectRatio if rendered is not None else Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        preview.setDevicePixelRatio(layout.dpr)
        card.set_preview_pixmap(QPixmap.fromImage(image), rendered=preview)
        card._preview_resolved = False
        # 缩放复用图像不启动新的淡入，避免批量回收卡片时创建大量动画。
        label = card._preview_label if isinstance(card, ModCard) else card._thumb
        label._stop_loading()
        effect = card.graphicsEffect()
        if effect is not None and effect is getattr(card, "_scroll_edge_fade_effect", None):
            if not effect._resize_finishing:
                effect.invalidate_source()

    @staticmethod
    def _vs_preview_layout(card: QFrame) -> PreviewLayout:
        """按卡片种类、实际图片区域和设备像素比生成预览布局，Mod 保留全图而分类使用裁剪。"""
        is_mod = isinstance(card, ModCard)
        return PreviewLayout(
            card._card_width - 8,
            card._preview_height if is_mod else card._preview_h,
            "contain" if is_mod else "crop",
            card.devicePixelRatioF(),
        )

    def _vs_item_preview_layout(self, item: dict) -> PreviewLayout:
        """不构造控件即可计算未来卡片的布局尺寸。"""
        category = item["type"] == "category"
        if category:
            preview_height = CharacterCard._preview_height_for(self._vs_card_h)
        else:
            ratio = 0.64 if self._vs_card_h >= 200 else max(0.52, 0.64 - (200 - self._vs_card_h) * 0.002)
            preview_height = int(self._vs_card_h * ratio)
        return PreviewLayout(
            self._vs_card_w - 8,
            preview_height,
            "crop" if category else "contain",
            self._scroll_content.devicePixelRatioF(),
        )

    @staticmethod
    def _vs_apply_preview(card: QFrame, image: object, *, rendered: object = None) -> None:
        """在界面线程转换原图和合成预览，二者有效时更新卡片，原图无效时显示占位。"""
        pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
        if pixmap.isNull():
            card.set_no_preview()
            return
        if rendered is not None and not rendered.isNull():
            card.set_preview_pixmap(pixmap, rendered=QPixmap.fromImage(rendered))

    def _vs_invalidate_prefetch(self, *, schedule: bool = False) -> None:
        """清空预取范围和等待队列，可在卡片模式空闲时安排一次新的视口更新。"""
        self._vs_prefetch_key = None
        self._vs_pending_prefetch.clear()
        self._vs_waiting_prefetch.clear()
        if schedule and not self._mods_loading and self._view_mode == "grid" and not self._vs_scroll_timer.isActive():
            self._vs_scroll_timer.start()

    def _vs_prepare_prefetch(self, first_idx: int, last_idx: int) -> None:
        """当前控件窗口填充完成后扫描一次相邻数据。"""
        if self._mods_loading or self._card_scale_active:
            return
        if not image_cache_budget.prefetch_allowed:
            self._vs_invalidate_prefetch()
            return
        if self._vs_population_pending or self._vs_window is None:
            return
        buffer_rows = (
            self._VS_INITIAL_BUFFER_ROWS if getattr(self, "_fresh_population", False) else self._VS_SCROLL_BUFFER_ROWS
        )
        _, _, visible_first, visible_last = self._vs_visible_rows(buffer_rows)
        current_window = (
            visible_first,
            visible_last,
            max(1, self._vs_cols),
            self._vs_card_w,
            self._vs_card_h,
            self._scroll_content.devicePixelRatioF(),
        )
        if current_window != self._vs_window:
            # 滚动信号可能早于控件更新调度图片。
            self._vs_invalidate_prefetch()
            return
        rows = max(1, (self._scroll_area.viewport().height() + self._vs_row_h - 1) // self._vs_row_h)
        _, _, first, last = self._vs_visible_rows(rows)
        key = (
            self._vs_window,
            first,
            last,
            self._adult_mode,
            preview_cache_token(),
            image_cache_budget.prefetch_generation,
        )
        if key == self._vs_prefetch_key:
            return
        self._vs_prefetch_key = key
        indices = [i for i in range(first, last + 1) if i < first_idx or i > last_idx]
        indices.sort(key=lambda i: max(first_idx - i, i - last_idx, 0))
        self._vs_pending_prefetch.clear()
        self._vs_waiting_prefetch.clear()
        for i in indices:
            item = self._display_items[i]
            path = self._vs_item_preview_path(item)
            if not path:
                continue
            request = self._vs_image_requests.get(item["id"])
            if request and request[2] == path:
                # 保留仍有价值的旧工作，但不能写入当前布局或缓存代次，等待数量受在途请求限制。
                self._vs_waiting_prefetch[item["id"]] = (request[0], path, key)
                continue
            layout = self._vs_item_preview_layout(item)
            if cached_preview(path, _PixmapLoadWorker.cache_size(layout), validate=False) is None:
                self._vs_pending_prefetch.append((item["id"], path))

    def _vs_resume_waiting_prefetch(self, item_id: str, path: str, request_id: int) -> bool:
        """仅重新检查本次因旧请求而跳过的候选。"""
        waiting = self._vs_waiting_prefetch.pop(item_id, None)
        if (
            waiting is None
            or waiting != (request_id, path, self._vs_prefetch_key)
            or self._mods_loading
            or self._view_mode != "grid"
            or self._vs_population_pending
            or item_id in self._vs_card_cache
        ):
            return False
        index = self._display_idx_map.get(item_id)
        if index is None or not waiting[2][1] <= index <= waiting[2][2]:
            return False
        item = self._display_items[index]
        if self._vs_item_preview_path(item) != path:
            return False
        layout = self._vs_item_preview_layout(item)
        if cached_preview(path, _PixmapLoadWorker.cache_size(layout), validate=False) is not None:
            return False
        # 替换结果正常消费，不创建额外等待，失败图片不能在同次扫描中反复重启。
        self._vs_pending_prefetch.append((item_id, path))
        return True

    def _vs_schedule_pixmaps(self) -> None:
        """结合加载状态、内存预取策略及请求上限，合并调度下一批卡片图片任务。"""
        if self._mods_loading:
            return
        if not image_cache_budget.prefetch_allowed:
            self._vs_invalidate_prefetch()
        elif self._view_mode == "grid" and self._vs_window is not None and not self._vs_population_pending:
            self._vs_prepare_prefetch(*self._vs_window[:2])
        prefetch_count = sum(card is None for _req, card, _path in self._vs_image_requests.values())
        has_work = self._vs_pending_pixmap_cards or (
            image_cache_budget.prefetch_allowed
            and self._vs_pending_prefetch
            and prefetch_count < self._VS_MAX_PREFETCH_REQUESTS
        )
        if (
            has_work
            and len(self._vs_image_requests) < self._VS_MAX_IMAGE_REQUESTS
            and not self._vs_pixmap_timer.isActive()
        ):
            self._vs_pixmap_timer.start()

    def _vs_load_deferred_pixmaps(self) -> None:
        """滚动期间有界提交图片批次，优先可见卡片。"""
        if self._mods_loading:
            return
        if not image_cache_budget.prefetch_allowed:
            self._vs_invalidate_prefetch()
        for _ in range(self._VS_PIXMAP_BATCH_SIZE):
            if len(self._vs_image_requests) >= self._VS_MAX_IMAGE_REQUESTS:
                break
            if self._vs_pending_pixmap_cards:
                item_id, card = self._vs_pending_pixmap_cards.pop(0)
                if self._vs_card_cache.get(item_id) is not card or card._preview_resolved:
                    continue
                if self._card_scale_active and card._cached_pixmap is not None:
                    card._preview_requested = False
                    continue
                if isinstance(card, ModCard):
                    mod = card._mod
                    if not mod or (self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X):
                        continue
                    path = card.effective_preview_path()
                else:
                    path = card._data.get("thumbnail", "")
                if not path:
                    card.set_no_preview()
                    continue
                if self._vs_restore_preview(card):
                    continue
                layout = self._vs_preview_layout(card)
            else:
                prefetch_count = sum(c is None for _r, c, _p in self._vs_image_requests.values())
                if not self._vs_pending_prefetch or prefetch_count >= self._VS_MAX_PREFETCH_REQUESTS:
                    break
                item_id, path = self._vs_pending_prefetch.pop(0)
                card = None
                index = self._display_idx_map.get(item_id)
                if index is None:
                    continue
                item = self._display_items[index]
                path = self._vs_item_preview_path(item)
                if not path:
                    continue
                layout = self._vs_item_preview_layout(item)
                if cached_preview(path, _PixmapLoadWorker.cache_size(layout), validate=False) is not None:
                    continue
            request = self._vs_image_requests.get(item_id)
            if request is not None and request[2] == str(path):
                if card is not None:
                    self._vs_image_requests[item_id] = (request[0], card, request[2])
                elif self._vs_prefetch_key is not None:
                    self._vs_waiting_prefetch[item_id] = (request[0], str(path), self._vs_prefetch_key)
                continue
            self._vs_image_request_id += 1
            request_id = self._vs_image_request_id
            self._vs_image_requests[item_id] = (request_id, card, str(path))
            self._pixmap_worker.enqueue(
                item_id, str(path), layout.width, layout.height, request_id, mode=layout.mode, dpr=layout.dpr
            )
        self._vs_schedule_pixmaps()

    def _on_bg_pixmap_loaded(
        self,
        item_id: str,
        path: str,
        request_id: int,
        result: DecodedPreview | None,
    ) -> None:
        """后台线程完成图片加载 → 主线程设置 QPixmap（极快，无IO）"""
        request = self._vs_image_requests.get(item_id)
        if request is None or request[0] != request_id or request[2] != path:
            return
        self._vs_image_requests.pop(item_id, None)
        self._vs_schedule_pixmaps()
        if self._vs_resume_waiting_prefetch(item_id, path, request_id):
            self._vs_schedule_pixmaps()
        card = self._vs_card_cache.get(item_id)
        if card is None or card is not request[1]:
            return
        if isinstance(card, ModCard):
            if card._mod is None or card._mod.mod_id != item_id:
                return
            current_path = card.effective_preview_path()
        elif isinstance(card, CharacterCard):
            current_path = card._data.get("thumbnail", "")
        else:
            return
        card._preview_requested = False
        if not current_path or not self._preview_paths_equal(str(current_path), path):
            return
        layout = self._vs_preview_layout(card)
        if result is not None and (not result.is_current(path) or result.layout != layout):
            if (
                self._card_scale_active
                and result.is_current(path)
                and result.image is not None
                and not result.image.isNull()
            ):
                self._vs_apply_temporary_preview(card, result.image, result.rendered)
                return
            card._preview_resolved = False
            card._preview_requested = True
            self._vs_pending_pixmap_cards.insert(0, (item_id, card))
            self._vs_schedule_pixmaps()
            return
        # 仅匹配的实际卡片消费图片时才提升缓存级别。
        retain_viewed_preview(path, _PixmapLoadWorker.cache_size(layout), result)
        # 后台已完成缩放合成；ModCard 负责结束 shimmer 和成人内容模糊。若加载期间切成隐藏模式，
        # 卡片入口会再次拒绝 X 级图片，避免在途结果绕过策略。
        self._vs_apply_preview(
            card,
            result.image if result is not None else None,
            rendered=result.rendered if result is not None else None,
        )

    def prepare_visible_card_pool(self, viewport_size: QSize) -> None:
        """预创建并预热首屏所需的有限 Mod 卡片，放入复用池以减少进入目录时的构造工作。"""
        width = int(CharacterCard.CARD_WIDTH * self._card_scale / 100)
        height = int(CharacterCard.CARD_HEIGHT * self._card_scale / 100)
        cols = max(1, (viewport_size.width() - 48) // (width + 12))
        rows = max(1, (viewport_size.height() + height + 11) // (height + 12)) + 1
        desired = min(self._VS_INITIAL_MAX_BINDINGS, cols * rows)
        existing = self._vs_card_pool.count(ModCard) + sum(
            isinstance(card, ModCard) for card in self._vs_card_cache.values()
        )
        for _ in range(max(0, desired - existing)):
            card = self._new_mod_card()
            card.set_mod_deferred(Mod(name="Prepared card"))
            card.resize_card(width, height)
            card.ensurePolished()
            card.layout().activate()
            self._vs_card_pool.release(card)

    def _new_mod_card(self) -> ModCard:
        """创建滚动区域所属的空 Mod 卡片，绑定一次点击、标签和菜单信号供后续数据复用。"""
        card = ModCard(
            mod=None,
            parent=self._scroll_content,
            adult_mode=self._adult_mode,
            card_width=self._vs_card_w,
            card_height=self._vs_card_h,
        )
        card.clicked.connect(self._on_mod_card_clicked)
        card.double_clicked.connect(self._on_mod_card_double_clicked)
        card.tag_clicked.connect(self.select_tag_filter)
        card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda pos, current=card: self._show_mod_card_context_menu(current, current.mod_id, pos)
        )
        return card

    def _vs_create_card(self, item: dict, defer_pixmap: bool = False, *, pooled_card: QFrame | None = None) -> QFrame:
        """为单个 display_item 创建卡片 Widget
        defer_pixmap=True 时仅设置文字信息，pixmap由外部定时器批量加载
        pooled_card 可传入调用方已取得的池卡片，始终返回绑定好的 Widget。
        """
        if item["type"] == "category":
            card = pooled_card if pooled_card is not None else self._vs_card_pool.take(CharacterCard)
            if card is None:
                card = CharacterCard(
                    item["data"],
                    parent=self._scroll_content,
                    card_width=self._vs_card_w,
                    card_height=self._vs_card_h,
                    defer_pixmap=defer_pixmap,
                )
                card.clicked.connect(self._on_card_clicked)
                card.double_clicked.connect(self._on_card_double_clicked)
                card.edit_requested.connect(self._on_edit_character)
                card.delete_requested.connect(self._on_category_card_delete)
                card.export_requested.connect(self._on_category_card_export)
                card.context_select_requested.connect(self._select_for_context_menu)
                card.clipboard_menu_requested.connect(self._add_item_clipboard_actions)
                card.open_folder_requested.connect(self._open_category_folder)
                card.online_search_requested.connect(self._online_search_by_cat_id)
                card.preview_dropped.connect(self.category_preview_dropped)
            else:
                card.set_category(item["data"], defer_pixmap=defer_pixmap)
                card.resize_card(self._vs_card_w, self._vs_card_h)
        else:
            mod = item["data"]
            card = pooled_card if pooled_card is not None else self._vs_card_pool.take(ModCard)
            if card is None:
                card = self._new_mod_card()
            card._adult_mode = self._adult_mode
            card.set_tag_catalog(self._tag_catalog_by_name)
            card.set_mod_deferred(mod)
            card.resize_card(self._vs_card_w, self._vs_card_h)
            # 沿用预览栏切到的非首张图：视图切换/滚动重建卡片后仍显示同一张，
            # 延迟与后台加载都走 effective_preview_path，不会覆盖回首图（串图 bug）。
            override = self._preview_override_paths.get(mod.mod_id)
            if override:
                card._preview_override_path = override
            if not defer_pixmap:
                card.load_pixmap_deferred()  # 立即加载预览图
            # defer_pixmap=True 时 pixmap 由 _vs_load_deferred_pixmaps 延迟加载
            card.set_update_available(mod.mod_id in self._updatable_ids)

        # 回收卡片的子布局可能延迟更新，复用时始终恢复实际控件边界。
        card.setFixedSize(self._vs_card_w, self._vs_card_h)

        # 控件隐藏期间绑定缓存像素，返回滚动区域时不重复播放加载动画。
        if defer_pixmap:
            self._vs_restore_preview(card)

        card.setEnabled(item["type"] == "category" or item["id"] not in getattr(self, "_pending_missing_mods", ()))
        if item["id"] in self._selected_ids:
            card.set_selected(True)
        # 剪切状态
        cut_ids = set(self._clipboard_cat_ids + self._clipboard_mod_ids)
        if item["id"] in cut_ids:
            if isinstance(card, CharacterCard):
                card.set_cut_state(True)
            elif isinstance(card, ModCard):
                card.set_cut_state(True)

        return card

    def _on_scroll_value_changed(self, _value: int) -> None:
        """滚动条变化时，延迟触发虚拟滚动更新"""
        if self._mods_loading or self._applying_position:
            return
        if _value != self._vs_population_anchor_value:
            self._cancel_directory_intro()
        # 首屏渐进填充期间若用户真的滚动了，优先补齐新的目标区域；继续按单卡
        # 填充旧首屏会让滚动位置出现空洞。刷新/布局引起的同值通知不算用户滚动。
        if (
            getattr(self, "_fresh_population", False)
            and self._vs_population_timer.isActive()
            and _value != self._vs_population_anchor_value
        ):
            self._vs_population_timer.stop()
            self._fresh_population = False
        self._vs_schedule_pixmaps()
        if not self._vs_scroll_timer.isActive():
            self._vs_scroll_timer.start()

    # 选择与导航

    def _on_card_clicked(self, char_id: str) -> None:
        """类别卡片单击 → 选择（Ctrl/Shift多选）"""
        self._cancel_directory_intro()
        self._scroll_area.setFocus(Qt.FocusReason.MouseFocusReason)
        self._flush_pending_preview_reorder()
        modifiers = QApplication.keyboardModifiers()
        item_index = self._find_display_index(char_id)

        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+Click: 切换选中
            if char_id in self._selected_ids:
                self._selected_ids.discard(char_id)
            else:
                self._selected_ids.add(char_id)
            self._last_clicked_index = item_index
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            # Shift+Click: 范围选中
            self._select_range(item_index)
        else:
            # 普通单击: 清空其他选择，选中此项
            self._selected_ids.clear()
            self._selected_ids.add(char_id)
            self._last_clicked_index = item_index

        self._update_card_selection()
        self._show_selection_panel()

    def _on_card_double_clicked(self, char_id: str) -> None:
        """类别卡片双击 → 进入类别"""
        self._navigate_to(char_id)

    def _on_mod_card_clicked(self, mod_id: str) -> None:
        """Mod 卡片单击 → 选择并显示管理面板"""
        self._cancel_directory_intro()
        self._scroll_area.setFocus(Qt.FocusReason.MouseFocusReason)
        # 切换到不同的Mod时，先持久化上一个Mod的预览图选择
        if self._selected_mod and self._selected_mod.mod_id != mod_id:
            self._flush_pending_preview_reorder()
        modifiers = QApplication.keyboardModifiers()
        item_index = self._find_display_index(mod_id)

        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if mod_id in self._selected_ids:
                self._selected_ids.discard(mod_id)
                if self._selected_mod and self._selected_mod.mod_id == mod_id:
                    self._selected_mod = None
                    self._hide_panel()
            else:
                self._selected_ids.add(mod_id)
            self._last_clicked_index = item_index
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._select_range(item_index)
        else:
            self._selected_ids.clear()
            self._selected_ids.add(mod_id)
            self._last_clicked_index = item_index

        # 先提交选中态。右栏图片和 INI 都是后台请求，不能再挡住卡片视觉反馈。
        self._update_card_selection()
        self._show_selection_panel()

    def _mod_by_id(self, mod_id: str) -> Mod | None:
        """按已显示的子树结果解析操作，包括嵌套 Mod。"""
        index = self._display_idx_map.get(mod_id, -1)
        if 0 <= index < len(self._display_items):
            item = self._display_items[index]
            if item["id"] == mod_id and item["type"] == "mod":
                return item["data"]
        return next((mod for mod in self._subtree_mods or self._current_mods if mod.mod_id == mod_id), None)

    def _show_selection_panel(self) -> None:
        """按零选、单选或多选切换详情面板，单选时优先解析 Mod，再匹配分类。"""
        self._selected_mod = None
        self._selected_category = None
        if len(self._selected_ids) > 1:
            self._show_multi_select_panel()
            return
        if self._selected_ids:
            item_id = next(iter(self._selected_ids))
            mod = self._mod_by_id(item_id)
            if mod is not None:
                self._selected_mod = mod
                self._show_mod_panel(mod)
                return
            cat = next((cat for cat in self._all_characters or self._characters if cat.get("id") == item_id), None)
            if cat is not None:
                self._selected_category = cat
                self._show_category_panel(cat)
                return
        self._hide_panel()

    def _prune_selection(self) -> None:
        """剔除已不在显示集合中的选择，必要时刷新详情并重置范围选择锚点。"""
        previous = self._selected_ids.copy()
        self._selected_ids.intersection_update(self._display_idx_map)
        refreshed = self._selected_mod and self._mod_by_id(self._selected_mod.mod_id) is not self._selected_mod
        if self._selected_ids != previous or refreshed:
            self._last_clicked_index = -1
            self._show_selection_panel()

    def _on_mod_card_double_clicked(self, mod_id: str) -> None:
        """Mod 卡片双击 → 切换启用/禁用"""
        if mod_id not in self._deploying_frac:
            self.mod_toggle_requested.emit(mod_id)

    def _find_display_index(self, item_id: str) -> int:
        """查找项目在显示列表中的索引（O(1)）"""
        return self._display_idx_map.get(item_id, -1)

    def _select_range(self, to_index: int) -> None:
        """Shift+Click: 选择从上次点击到当前位置的所有项"""
        if self._last_clicked_index < 0:
            self._last_clicked_index = 0
        start = min(self._last_clicked_index, to_index)
        end = max(self._last_clicked_index, to_index)
        for i in range(start, end + 1):
            if i < len(self._display_items):
                self._selected_ids.add(self._display_items[i]["id"])

    def _update_card_selection(self) -> None:
        """更新所有可见卡片的选中状态视觉反馈"""
        for item_id, card in self._vs_card_cache.items():
            card.set_selected(item_id in self._selected_ids)
        self._update_status_bar_selection()

    def _update_status_bar_selection(self) -> None:
        """根据当前选中项更新底部状态栏：显示选中的类别、角色/对象、Mod 数量"""
        if not self._selected_ids:
            # 无选中时显示当前目录概览
            cat_count = sum(1 for item in self._display_items if item["type"] == "category")
            mod_count = sum(1 for item in self._display_items if item["type"] == "mod")
            char_obj_count = sum(
                1
                for item in self._display_items
                if item["type"] == "category" and item.get("data", {}).get("node_type") == "character"
            )
            pure_cat_count = cat_count - char_obj_count
            parts = []
            if pure_cat_count > 0:
                parts.append(tr("{pure_cat_count} 个类别", pure_cat_count=pure_cat_count))
            if char_obj_count > 0:
                parts.append(tr("{char_obj_count} 个角色/对象", char_obj_count=char_obj_count))
            if mod_count > 0:
                parts.append(tr("{mod_count} 个 Mod", mod_count=mod_count))
            has_conditions = bool(
                self._search_input.text().strip()
                or self._tag_filter.selected_ids
                or self._type_filter.currentData()
                or self._age_filter.currentData()
            )
            self._status_label.setText(", ".join(parts) if parts else tr("匹配 0 项") if has_conditions else t("就绪"))
            return
        # 统计选中项的类型
        sel_cat = 0
        sel_char = 0
        sel_mod = 0
        for item in self._display_items:
            if item["id"] in self._selected_ids:
                if item["type"] == "mod":
                    sel_mod += 1
                elif item["type"] == "category":
                    if item.get("data", {}).get("node_type") == "character":
                        sel_char += 1
                    else:
                        sel_cat += 1
        parts = []
        if sel_cat > 0:
            parts.append(tr("{sel_cat} 个类别", sel_cat=sel_cat))
        if sel_char > 0:
            parts.append(tr("{sel_char} 个角色/对象", sel_char=sel_char))
        if sel_mod > 0:
            parts.append(tr("{sel_mod} 个 Mod", sel_mod=sel_mod))
        total = sel_cat + sel_char + sel_mod
        self._status_label.setText(tr("已选择 {total} 项（{value}）", total=total, value="，".join(parts)))

    # 统一预览面板

    def _show_category_panel(self, cat: dict) -> None:
        """显示右侧类别预览面板。"""
        self._panel_mode = "category"
        self._clear_variant_context()
        self._detail_tabs.setCurrentIndex(0)
        self._detail_tabs.show()
        self._detail_tabs.tabBar().hide()
        self._panel_deploy_hint.hide()
        self._panel_tags.hide()
        self._preview_panel.setVisible(True)
        self._ensure_preview_panel_expanded()

        # 恢复预览图区域样式和提示文本
        self._panel_preview.set_empty_text(tr("拖拽图片到此处\n或点击添加预览图\n或右键粘贴剪切板图片"))

        self._panel_name_label.setText(cat.get("name", tr("未知")))

        count = cat.get("mod_count", 0)
        enabled = cat.get("enabled_count", 0)
        self._panel_status_label.setText(tr("Mod: {enabled}/{count} 已启用", enabled=enabled, count=count))
        self._panel_status_label.setStyleSheet(f"color: {theme.color('text.tertiary')};")

        self._panel_rating_label.setVisible(False)
        self._panel_keys_container.setVisible(False)

        name_en = cat.get("name_en", "")
        self._panel_fact_captions[0].setText(tr("英文名"))
        self._panel_fact_captions[0].setVisible(bool(name_en))
        for widget in (
            *self._panel_fact_captions[1:],
            self._panel_version_label,
            self._panel_category_label,
            self._panel_size_label,
            self._panel_tags_title,
            self._panel_desc_title,
            self._panel_file_title,
            self._panel_source_label,
        ):
            widget.hide()
        if name_en:
            self._panel_author_label.setText(name_en)
            self._panel_author_label.setVisible(True)
        else:
            self._panel_author_label.setVisible(False)

        self._panel_desc_label.setVisible(False)

        # 类别仅显示一张预览图。
        thumbnail = cat.get("thumbnail", "")
        if thumbnail and fs.exists(thumbnail):
            self._panel_preview.set_images([thumbnail])
        else:
            self._panel_preview.clear()

        # 类别预览图管理仅提供添加和删除按钮。
        self._panel_preview._add_btn.setVisible(True)
        self._panel_preview._add_btn.setText(tr("添加预览图"))
        has_thumb = bool(thumbnail and fs.exists(thumbnail))
        self._panel_preview._delete_btn.setVisible(has_thumb)
        self._panel_preview._delete_btn.setText(tr("删除预览图"))
        self._panel_preview._delete_all_btn.setVisible(False)
        # 隐藏导航按钮（类别只有一张图）
        self._panel_preview._prev_btn.setVisible(False)
        self._panel_preview._next_btn.setVisible(False)
        self._panel_preview._page_label.setVisible(False)

        self._panel_path_label.setVisible(False)

        # 类别模式按钮
        self._panel_toggle_btn.setVisible(False)
        self._panel_edit_btn.setEnabled(True)
        self._panel_edit_btn.setVisible(True)
        self._panel_open_btn.setEnabled(True)
        self._panel_open_btn.setVisible(True)
        self._panel_delete_btn.setEnabled(True)
        self._panel_delete_btn.setVisible(True)

        # 分类模式按配置显示在线搜索按钮。
        from src.core.config import Config as _Cfg

        gb_enabled = bool(_Cfg().get("gamebanana.character_to_search_button", True))
        self._panel_gb_search_btn.setEnabled(gb_enabled)
        self._panel_gb_search_btn.setVisible(gb_enabled)

        # 启用预览图添加（类别模式）
        self._panel_preview.setAcceptDrops(True)
        # 类别仅有一张预览图。
        self._panel_preview.set_single_image_mode(True)

    def _show_mod_panel(self, mod: Mod) -> None:
        """显示右侧 Mod 管理面板。"""
        self._remember_detail_scroll()
        changed_mod = self._detail_mod_id != mod.mod_id
        self._detail_mod_id = mod.mod_id
        self._panel_mode = "mod"
        self._detail_tabs.show()
        self._detail_tabs.tabBar().show()
        with QSignalBlocker(self._detail_tabs):
            self._detail_tabs.setCurrentIndex(self._mod_detail_tab)
        self._last_detail_tab = self._mod_detail_tab
        if changed_mod:
            self._variant_panel.clear_context()
            self._panel_variant_label.hide()
            self._detail_tabs.setTabText(2, tr("变体 0"))
        self._preview_panel.setVisible(True)
        self._ensure_preview_panel_expanded()

        # PreviewWidget 按最终有图/加载/空状态恢复样式，避免每次选择先重刷占位底色。
        self._panel_preview.set_empty_text(tr("拖拽图片到此处\n或点击添加预览图\n或右键粘贴剪切板图片"))

        self._update_mod_panel_info(mod)
        self._build_keys_panel()
        self._load_mod_panel_preview(mod)

        # Mod模式下所有按钮可见
        self._panel_toggle_btn.setVisible(True)
        self._refresh_panel_mod_status()
        self._panel_edit_btn.setVisible(True)
        self._panel_edit_btn.setEnabled(True)
        self._panel_open_btn.setEnabled(True)
        self._panel_open_btn.setVisible(True)
        self._panel_delete_btn.setEnabled(True)
        self._panel_delete_btn.setVisible(True)
        self._panel_gb_search_btn.setVisible(False)
        self._panel_gb_search_btn.setEnabled(False)
        self._panel_preview.setAcceptDrops(True)
        self._panel_preview.set_single_image_mode(False)
        self._panel_preview._add_btn.setText(tr("添加预览图"))
        self._panel_preview._add_btn.setVisible(True)
        self._panel_preview._delete_btn.setText(tr("删除"))
        for index in range(3):
            self._detail_scrollbar(index).setValue(self._detail_scroll_positions.get((mod.mod_id, index), 0))
        if changed_mod:
            call_later(0, self, lambda: self._restore_detail_scroll(mod.mod_id, 0))
        self.variant_context_changed.emit(mod.mod_id)

    def _update_mod_panel_info(self, mod: Mod) -> None:
        """更新元数据，保留当前图片和异步按键布局。"""
        self._panel_name_label.setText(mod.display_name)

        # 年龄分级
        rating_labels = {
            AgeRating.G: (tr("G - 全年龄"), "#4CAF50"),
            AgeRating.P: (tr("P - 轻微敏感"), "#E8A838"),
            AgeRating.R: (tr("R - 限制级"), "#FF7043"),
            AgeRating.X: (tr("X · 18+ 内容"), "#E53935"),
        }
        label_text, label_color = rating_labels.get(mod.info.age_rating, (tr("G - 全年龄"), "#4CAF50"))
        self._panel_rating_label.setText(mod.info.age_rating.value)
        self._panel_rating_label.setToolTip(label_text)
        rating_style = (
            f"font-size: 10px; font-weight: bold; padding: 1px 5px; border-radius: 3px; "
            f"color: white; background-color: {label_color};"
        )
        if self._panel_rating_label.styleSheet() != rating_style:
            self._panel_rating_label.setStyleSheet(rating_style)
        self._panel_rating_label.setVisible(True)

        self._panel_fact_captions[0].setText(tr("作者"))
        for widget in (
            *self._panel_fact_captions,
            self._panel_author_label,
            self._panel_version_label,
            self._panel_category_label,
            self._panel_size_label,
            self._panel_tags_title,
            self._panel_file_title,
            self._panel_source_label,
        ):
            widget.show()
        self._panel_author_label.setText(mod.info.author or "—")
        self._panel_version_label.setText(mod.info.version or "—")
        category = self._find_category_by_id(mod.node_id)
        self._panel_category_label.setText(category.get("name", "—") if category else "—")
        size = "—"
        if mod.path:
            try:
                resource = Path(mod.path)
                if resource.is_file():
                    amount = resource.stat().st_size
                    size = f"{amount / 1024**2:.1f} MB" if amount >= 1024**2 else f"{amount / 1024:.1f} KB"
                elif resource.is_dir():
                    size = tr("文件夹资源")
            except OSError:
                pass
        self._panel_size_label.setText(size)

        if mod.info.description:
            self._panel_desc_label.setText(mod.info.description)
            self._panel_desc_label.setVisible(True)
        else:
            self._panel_desc_label.setVisible(False)
        self._panel_desc_title.setVisible(bool(mod.info.description))

        self._panel_tags.set_tags(tags_for_names(mod.info.tags, self._tag_catalog_by_name))
        self._panel_tags.show()

        self._panel_path_label.setText(mod.path)
        self._panel_path_label.setVisible(True)
        self._update_mod_panel_source(mod)

    def _update_mod_panel_source(self, mod: Mod) -> None:
        """主题切换后刷新富文本链接的显式颜色。"""
        url = str(mod.info.url or "")
        if QUrl(url).scheme() in ("http", "https"):
            source = (
                f'<a href="{escape(url, quote=True)}" style="color: {theme.color("text.link")}; '
                f'text-decoration: underline;">{escape(tr("查看来源"))}</a>'
            )
        else:
            source = escape(tr("本地导入"))
        self._panel_source_label.setText(f"{source} · {escape(str(mod.imported_at)[:10])}")

    def _load_mod_panel_preview(self, mod: Mod, *, initial_index: int = 0, force_reload: bool = False) -> None:
        """立即复用现有缓存，并在分辨率不足时后台升级为侧栏高清图。"""
        self._panel_preview_request_id += 1
        request_id = self._panel_preview_request_id
        self._panel_preview_worker.clear_queue()

        if not mod.preview_images:
            self._panel_preview.clear()
            return
        if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
            self._panel_preview.clear()
            self._panel_preview._image_label.setText(tr("18+ 内容已隐藏"))
            return

        panel_card = self._vs_card_cache.get(mod.mod_id)
        if not isinstance(panel_card, ModCard):
            panel_card = None
        cached_path = panel_card.effective_preview_path() if panel_card is not None else ""
        cached_pixmap = panel_card._cached_pixmap if panel_card is not None else None
        initial_index = min(initial_index, len(mod.preview_images) - 1)
        image_path = mod.preview_images[initial_index]
        label_size = self._panel_preview._image_label.size()
        target_w = min(
            PreviewWidget._WORK_MAX_EDGE,
            max(680, label_size.width() * 2),
        )
        target_h = min(
            PreviewWidget._WORK_MAX_EDGE,
            max(500, label_size.height() * 2),
        )
        self._panel_preview.set_images_deferred(
            mod.preview_images,
            initial_index=initial_index,
            cached_path=cached_path or "",
            cached_pixmap=cached_pixmap,
        )
        if not force_reload and not self._panel_preview.cached_pixmap_needs_upgrade(
            image_path,
            target_w,
            target_h,
        ):
            return
        self._panel_preview_worker.enqueue(
            request_id,
            mod.mod_id,
            image_path,
            target_w,
            target_h,
        )

    def _on_panel_preview_loaded(
        self,
        request_id: int,
        mod_id: str,
        image_path: str,
        qimage: object,
    ) -> None:
        """验证请求编号与当前 Mod 后应用详情预览，并尝试将结果复用于该 Mod 的可见卡片。"""
        selected = self._selected_mod
        if (
            request_id != self._panel_preview_request_id
            or self._panel_mode != "mod"
            or selected is None
            or selected.mod_id != mod_id
        ):
            return
        try:
            pixmap = QPixmap.fromImage(qimage) if qimage is not None else None
        except (TypeError, RuntimeError):
            pixmap = None
        if not self._panel_preview.apply_deferred_pixmap(image_path, pixmap):
            return
        panel_card = self._vs_card_cache.get(mod_id)
        if isinstance(panel_card, ModCard):
            self._reuse_panel_preview_for_card(
                mod_id,
                image_path,
                panel_card,
            )

    def _action_label(self, section: str) -> str:
        """按键作用名始终显示 INI 中的原始 section 标头。"""
        return section

    def _clear_layout(self, layout) -> None:
        """递归移除布局项并延后销毁子控件，供详情内容整体替换使用。"""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)

    def refresh_keys_if_selected(self, mod_id: str) -> bool:
        """仅当指定 Mod 正在右侧详情栏中显示时，重建其按键面板。

        IniSyncManager 已过滤掉“无按键 ini 从无按键变到无按键”的事件；这里再做选中态
        守卫，避免后台其他 Mod 的同步引起无意义的界面重建。返回是否实际刷新。
        """
        mod = self._selected_mod
        if mod is None or mod.mod_id != mod_id:
            return False
        self._build_keys_panel()
        return True

    def _build_keys_panel(self) -> None:
        """后台解析 Mod 的有效 INI，完成后再渲染可编辑面板。

        mod_manager.parse_mod_key_bindings(mod) 支持读取目录及 .7z/.rar/.zip 归档中的 INI。
        以 binding.rel（本体内相对路径）分组，编辑结果写入 override 层，保留原始本体。
        """
        self._key_bindings_request_id += 1
        mod = self._selected_mod
        if mod is None or self._keys_panel_mod_id != mod.mod_id:
            self._clear_layout(self._panel_keys_layout)
            self._panel_keys_container.hide()
            self._keys_panel_signature = None
        self._panel_keys_container.setEnabled(False)
        if mod is None or self._mod_manager is None:
            self._key_bindings_worker.clear_queue()
            return
        self._key_bindings_worker.enqueue(
            self._key_bindings_request_id,
            mod,
            self._mod_manager,
        )

    def _on_key_bindings_loaded(
        self,
        request_id: int,
        mod_id: str,
        bindings: object,
    ) -> None:
        """仅为当前请求和当前 Mod 应用按键结果，非列表结果按空配置显示。"""
        selected = self._selected_mod
        if (
            request_id != self._key_bindings_request_id
            or self._panel_mode != "mod"
            or selected is None
            or selected.mod_id != mod_id
        ):
            return
        self._render_keys_panel(bindings if isinstance(bindings, list) else [])

    def _render_keys_panel(self, bindings: list) -> None:
        """按 INI 来源分组构建按键编辑项，完整替换容器后恢复该 Mod 的标签页滚动位置。"""
        mod = self._selected_mod
        changed_mod = self._keys_panel_mod_id != (mod.mod_id if mod else None)
        self._detail_tabs.setTabText(1, tr("按键 {count}", count=len(bindings)))
        self._panel_keys_empty.setVisible(not bool(bindings))
        signature = (mod.mod_id, mod.path, tuple(bindings)) if mod is not None else None
        if signature == self._keys_panel_signature:
            self._panel_keys_container.setEnabled(True)
            self._panel_keys_container.setVisible(bool(bindings))
            return
        self._keys_panel_mod_id = mod.mod_id if mod is not None else None
        self._keys_panel_signature = signature
        if mod is None or not bindings:
            self._clear_layout(self._panel_keys_layout)
            self._panel_keys_container.setVisible(False)
            self._panel_keys_container.setEnabled(True)
            return

        # 在可见树之外准备替换内容，全部行和文件操作就绪前保留原布局高度。
        container = QWidget()
        container.setMinimumWidth(0)
        container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title = QLabel(t("按键设置"))
        title.setObjectName("iniKeysTitle")
        title.setStyleSheet(f"color: {theme.color('warning')}; font-size: 13px; font-weight: bold;")
        title.hide()  # 标签页已经提供分区标题。
        layout.addWidget(title)

        # 按来源 ini 的「本体内相对路径 rel」分组（归档本体无绝对路径，用 rel 稳定分组）
        by_file: dict[str, list] = {}
        for b in bindings:
            by_file.setdefault(getattr(b, "rel", "") or b.ini_path, []).append(b)

        for gi, (rel, group) in enumerate(by_file.items()):
            # 多个 ini 之间用一道细分割线 + 上下留白，清晰分组（仅加空白间隔不够明显）
            if gi > 0:
                layout.addSpacing(8)
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background-color: {theme.color('stroke.card')}; border: none;")
                layout.addWidget(sep)
                layout.addSpacing(8)
            # 文件头同行显示 INI 标识、文件名及固定宽度的打开按钮，
            # 极长文件名只在中间弹性区域省略，不能再把按钮挤出预览栏。
            display_rel = fs.normal_path(rel)
            file_name = Path(display_rel).name
            header_widget = QWidget()
            header_widget.setObjectName("iniFileHeader")
            header_widget.setMinimumWidth(0)
            header_layout = QHBoxLayout(header_widget)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)

            file_badge = QLabel("INI")
            file_badge.setObjectName("iniFileBadge")
            file_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            file_badge.setFixedSize(31, 20)
            file_badge.setStyleSheet(
                f"color: {theme.color('accent')};"
                f" background-color: {theme.rgba('accent', 0.12)};"
                f" border: 1px solid {theme.rgba('accent', 0.35)};"
                " border-radius: 4px; font-size: 10px; font-weight: bold;"
            )
            header_layout.addWidget(file_badge)

            file_label = _ElidedLabel(file_name)
            file_label.setObjectName("iniFileName")
            file_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 12px;")
            file_label.setToolTip("")
            header_layout.addWidget(file_label, 1)

            # 文件夹本体返回原文件；归档本体的 ini 已在解析阶段播种到容器 override 层，
            # 同样有可直接打开的磁盘路径。
            open_target = self._mod_manager.effective_ini_disk_path(mod, rel)
            if open_target:
                open_btn = QPushButton(t("打开文件"))
                open_btn.setObjectName("iniOpenFileButton")
                icons.apply_icon(open_btn, "actions/open")
                open_btn.setIconSize(QSize(14, 14))
                open_btn.setFixedHeight(30)
                open_btn.setMinimumWidth(88)
                open_btn.setSizePolicy(
                    QSizePolicy.Policy.Fixed,
                    QSizePolicy.Policy.Fixed,
                )
                # 内容、边距和边框均限制在当前行高内。
                open_btn.setStyleSheet("font-size: 12px; padding: 4px 16px;")
                open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                open_btn.clicked.connect(lambda _=False, p=open_target: self._on_open_ini(p))
                header_layout.addWidget(open_btn)
            layout.addWidget(header_widget)

            for binding_index, binding in enumerate(group):
                if binding_index > 0:
                    layout.addSpacing(5)
                layout.addWidget(self._build_binding_columns(binding, show_labels=binding_index == 0))

        old = self._panel_keys_container
        scroll_bar = self._panel_keys_scroll.verticalScrollBar()
        scroll_value = self._detail_scroll_positions.get((mod.mod_id, 1), 0) if changed_mod else scroll_bar.value()
        self._panel_keys_host.layout().replaceWidget(old, container)
        self._panel_keys_container, self._panel_keys_layout = container, layout
        old.hide()
        old.deleteLater()
        container.show()
        self._panel_keys_host.layout().activate()
        scroll_bar.setValue(scroll_value)
        if changed_mod:
            call_later(0, self, lambda: self._restore_detail_scroll(mod.mod_id, 1))

    def _build_binding_columns(self, b: KeyBinding, *, show_labels=False) -> QWidget:
        """按键页中的 INI 分组共用动作、前向键和后向键表格。"""
        field = QWidget()
        field.setObjectName("iniBindingRow")
        field.setMinimumWidth(0)
        field.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        grid = QGridLayout(field)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(0, 1)
        grid.setColumnMinimumWidth(1, 96)
        grid.setColumnMinimumWidth(2, 96)

        action_label = _ElidedLabel(self._action_label(b.section))
        action_label.setObjectName("iniBindingActionLabel")
        action_label.setMinimumWidth(0)
        action_label.setToolTip("")
        action_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 13px;")
        row = 1 if show_labels else 0
        if show_labels:
            for column, (text, name) in enumerate(
                ((tr("动作"), "iniActionLabel"), (tr("前进 / 开关"), "iniForwardLabel"), (tr("后退"), "iniBackLabel"))
            ):
                label = QLabel(text)
                label.setObjectName(name)
                label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 12px;")
                if column:
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(label, 0, column)
        grid.addWidget(action_label, row, 0)
        for column, role, value, line in (
            (1, "key", b.key_value, b.key_line_no),
            (2, "back", b.back_value, b.back_line_no),
        ):
            if value is None or line is None:
                empty = QLabel("—")
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(empty, row, column)
                continue
            control = KeyCaptureField(value)
            control.setProperty("bindingRole", "forward" if role == "key" else "back")
            control.setFixedWidth(96)
            control._btn.setMinimumWidth(0)
            control._btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            control._edit.setFixedWidth(20)
            control.setStyleSheet("QPushButton { padding: 2px 3px; min-width: 0; min-height: 24px; font-size: 11px; }")
            control.token_changed.connect(lambda tok, bn=b, key=role: self._on_key_changed(bn, key, tok))
            grid.addWidget(control, row, column)

        # 原始 type=/$var=/condition 是 3DMigoto 内部实现细节，对用户无展示价值，不再显示。
        return field

    def _on_key_changed(self, binding: KeyBinding, field: str, token: str) -> None:
        """通过管理器保存所选 Mod 的按键覆盖，失败时提示，成功后重新解析并刷新详情状态。"""
        # 通过容器覆盖层保存按键变更，并为已启用 Mod 同步部署副本。
        mod = self._selected_mod
        rel = getattr(binding, "rel", "") or ""
        line_no = binding.key_line_no if field == "key" else binding.back_line_no
        if mod is None or line_no is None:
            return
        try:
            ok = self._mod_manager.edit_mod_key(mod.mod_id, rel, line_no, field, token)
        except IniWriteError as e:
            logger.warning("按键设置保存失败：%s", e)
            QMessageBox.warning(
                self,
                t("按键设置"),
                t("按键设置保存失败"),
            )
            return
        if not ok:
            QMessageBox.warning(
                self,
                t("按键设置"),
                t("按键设置保存失败"),
            )
            return
        # 写成功 → 现场重扫刷新面板（不缓存解析结果）
        self._build_keys_panel()
        owner = self.window()
        if hasattr(owner, "_toast_done"):
            owner._toast_done(t("已写入 {file}").format(file=Path(fs.normal_path(rel)).name or rel))
        self._refresh_panel_mod_status()

    def _on_open_ini(self, ini_path: str) -> None:
        """将 INI 磁盘路径交给系统默认应用打开。"""
        QDesktopServices.openUrl(QUrl.fromLocalFile(ini_path))

    def _hide_panel(self) -> None:
        """取消选中时显示预览栏空状态；用户可通过分割线手动收起。"""
        self._panel_mode = "none"
        self._clear_variant_context()
        self._panel_deploy_hint.hide()
        self._panel_status_label.setToolTip("")
        self._panel_tags.hide()
        # 不再隐藏预览面板，仅重置内容为空状态
        self._panel_name_label.setText(tr("选择项目查看详情"))
        self._panel_status_label.setText("")
        self._panel_rating_label.setVisible(False)
        self._panel_keys_container.setVisible(False)
        self._panel_author_label.setVisible(False)
        self._panel_desc_label.setVisible(False)
        self._panel_path_label.setVisible(False)
        self._panel_preview.clear()
        self._panel_toggle_btn.setVisible(False)
        self._panel_toggle_btn.setEnabled(False)
        self._panel_edit_btn.setVisible(False)
        self._panel_edit_btn.setEnabled(False)
        self._panel_open_btn.setVisible(False)
        self._panel_open_btn.setEnabled(False)
        self._panel_delete_btn.setVisible(False)
        self._panel_delete_btn.setEnabled(False)
        # GameBanana 搜索按钮也隐藏
        self._panel_gb_search_btn.setVisible(False)
        self._panel_gb_search_btn.setEnabled(False)
        # 未选中 Mod 时禁用预览图添加。
        self._panel_preview.setAcceptDrops(False)
        self._panel_preview._add_btn.setVisible(False)
        # 未选中时不显示拖拽提示
        self._panel_preview.set_empty_text(tr("选择项目查看预览图"))

    def _show_multi_select_panel(self) -> None:
        """多选时显示汇总信息，不显示具体预览"""
        self._panel_mode = "multi"
        self._clear_variant_context()
        self._panel_deploy_hint.hide()
        self._panel_tags.hide()
        # 统计选中的类别和Mod数量
        sel_cat = 0
        sel_mod = 0
        for item in self._display_items:
            if item["id"] in self._selected_ids:
                if item["type"] == "mod":
                    sel_mod += 1
                else:
                    sel_cat += 1
        parts = []
        if sel_cat > 0:
            parts.append(tr("{sel_cat} 个类别", sel_cat=sel_cat))
        if sel_mod > 0:
            parts.append(tr("{sel_mod} 个 Mod", sel_mod=sel_mod))
        self._panel_name_label.setText(tr("已选中 {value}", value=", ".join(parts)))
        self._panel_status_label.setText("")
        self._panel_rating_label.setVisible(False)
        self._panel_keys_container.setVisible(False)
        self._panel_author_label.setVisible(False)
        self._panel_desc_label.setVisible(False)
        self._panel_path_label.setVisible(False)
        self._panel_preview.clear()
        self._panel_preview.setAcceptDrops(False)
        self._panel_preview._add_btn.setVisible(False)
        self._panel_preview.set_empty_text("")
        self._panel_toggle_btn.setVisible(False)
        self._panel_toggle_btn.setEnabled(False)
        self._panel_edit_btn.setVisible(False)
        self._panel_edit_btn.setEnabled(False)
        self._panel_open_btn.setVisible(False)
        self._panel_open_btn.setEnabled(False)
        self._panel_delete_btn.setVisible(False)
        self._panel_delete_btn.setEnabled(False)
        # GameBanana 搜索按钮多选模式下也隐藏
        self._panel_gb_search_btn.setVisible(False)
        self._panel_gb_search_btn.setEnabled(False)

    def _on_panel_toggle(self) -> None:
        """所选 Mod 没有正在部署时，向宿主请求切换启用状态。"""
        if self._selected_mod and self._selected_mod.mod_id not in self._deploying_frac:
            self.mod_toggle_requested.emit(self._selected_mod.mod_id)

    def _on_panel_edit(self) -> None:
        """根据当前的类别模式或 Mod 模式执行编辑操作。"""
        if self._panel_mode == "category" and self._selected_category:
            self._on_edit_character(self._selected_category.get("id", ""))
        elif self._panel_mode == "mod" and self._selected_mod:
            self.mod_edit_requested.emit(self._selected_mod.mod_id)

    def _on_panel_open_folder(self) -> None:
        """按详情模式定位 Mod 源项或打开分类目录，解析分类路径时不创建缺失目录。"""
        import os
        import subprocess

        if self._panel_mode == "mod" and self._selected_mod and self._selected_mod.path:
            path = Path(self._selected_mod.path)
            if path.exists():
                if os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)])
        elif self._panel_mode == "category" and self._selected_category:
            # 打开类别的文件夹
            from src.core.mod_manager import ModManager

            mm = ModManager()
            try:
                path = mm._get_character_library(self._selected_category.get("id", ""), create=False)
                if path.exists():
                    if os.name == "nt":
                        subprocess.Popen(["explorer", str(path)])
                    else:
                        subprocess.Popen(["xdg-open", str(path)])
            except Exception:
                pass

    def _on_panel_delete(self) -> None:
        """根据详情当前绑定的 Mod 或分类，向宿主发布对应的删除请求。"""
        if self._panel_mode == "mod" and self._selected_mod:
            self.mod_delete_requested.emit(self._selected_mod.mod_id)
        elif self._panel_mode == "category" and self._selected_category:
            self.character_delete_requested.emit(self._selected_category.get("id", ""))

    def _on_panel_gb_search(self) -> None:
        """在线查找当前类别的 Mod（预览栏按钮）。"""
        if not self._selected_category:
            return
        self._online_search_category(self._selected_category)

    def _online_search_category(self, cat: dict) -> None:
        """使用类别英文名搜索 GameBanana；未填写时提示补充英文名。"""
        name_en = (cat.get("name_en") or "").strip()
        if not name_en:
            QMessageBox.information(
                self,
                tr("提示"),
                tr("为了提供更好的搜索结果，请先键入此类别的英文名后继续"),
            )
            return
        main = self.window()
        if hasattr(main, "switch_to_mod_store_with_query"):
            main.switch_to_mod_store_with_query(name_en)

    def _online_search_by_cat_id(self, cat_id: str) -> None:
        """右键「在线查找」：按 cat_id 找回含 name_en 的类别 dict 后走统一逻辑。"""
        cat = self._find_category_by_id(cat_id)
        if cat is not None:
            self._online_search_category(cat)

    def _find_category_by_id(self, cat_id: str) -> dict | None:
        """在全部类别（含各层级）里按 id 找回 dict（含 name_en）。"""
        for c in self._all_characters:
            if c.get("id") == cat_id:
                return c
        return None

    def _open_category_folder(self, cat_id: str) -> None:
        """打开类别源目录（右键菜单）。复用与预览栏「打开源目录」一致的解析。"""
        import os
        import subprocess

        from src.core.mod_manager import ModManager

        try:
            path = ModManager()._get_character_library(cat_id, create=False)
            if path and Path(path).exists():
                if os.name == "nt":
                    subprocess.Popen(["explorer", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def _on_panel_preview_changed(self, index: int) -> None:
        """预览面板图片切换 → 同步卡片预览图 + 实时把该图写成主图（去抖 200ms）。

        路径**取自预览栏自己的列表**而不是 ``_selected_mod.preview_images[index]``：
        顺序实时落库后 DB 的排列会变，而预览栏列表保持加载时的顺序，两边索引对不上。
        """
        if self._panel_mode != "mod" or not self._selected_mod:
            return
        img_path = self._panel_preview.current_image_path()
        if not img_path:
            if not (self._selected_mod.preview_images and 0 <= index < len(self._selected_mod.preview_images)):
                return
            img_path = self._selected_mod.preview_images[index]
        # 同步卡片预览图
        panel_card = self._vs_card_cache.get(self._selected_mod.mod_id)
        if isinstance(panel_card, ModCard):
            self._reuse_panel_preview_for_card(
                self._selected_mod.mod_id,
                img_path,
                panel_card,
                update_preview_path=True,
            )
        # 保存当前预览路径，供视图切换后重建卡片时使用。
        if self._preview_override_paths.get(self._selected_mod.mod_id) != img_path:
            self._preview_override_paths[self._selected_mod.mod_id] = img_path
            self._vs_invalidate_prefetch(schedule=True)
        self._pending_reorder_mod_id = self._selected_mod.mod_id
        self._pending_reorder_path = img_path
        self._preview_reorder_timer.start()

    @staticmethod
    def _preview_paths_equal(left: str, right: str) -> bool:
        """比较不访问磁盘的规范化图片路径。"""
        if not left or not right:
            return False
        return Path(fs.normal_path(left)).absolute() == Path(fs.normal_path(right)).absolute()

    def _reuse_panel_preview_for_card(
        self,
        mod_id: str,
        image_path: str,
        expected_card: ModCard | None,
        *,
        update_preview_path: bool = False,
    ) -> bool:
        """经选择、实例、路径和分级校验后，将右栏缓存复用到当前卡片。"""
        selected = self._selected_mod
        if (
            self._panel_mode != "mod"
            or selected is None
            or selected.mod_id != mod_id
            or expected_card is None
            or self._vs_card_cache.get(mod_id) is not expected_card
        ):
            return False

        card_mod = getattr(expected_card, "_mod", None)
        if card_mod is None or card_mod.mod_id != mod_id:
            return False
        if self._adult_mode == "hide" and card_mod.info.age_rating == AgeRating.X:
            return False

        cached_path = self._panel_preview._cached_path
        cached_pixmap = self._panel_preview._cached_pixmap
        if not self._preview_paths_equal(cached_path, image_path) or cached_pixmap is None or cached_pixmap.isNull():
            return False

        if update_preview_path:
            return expected_card.show_preview_by_path(image_path, cached_pixmap)
        if not self._preview_paths_equal(expected_card.effective_preview_path() or "", image_path):
            return False
        return expected_card.set_preview_pixmap(cached_pixmap)

    def _on_panel_preview_dropped(self, image_path: str) -> None:
        """预览面板拖入图片"""
        if self._panel_mode == "mod" and self._selected_mod:
            # 转发给 main_window 处理持久化
            self.preview_image_dropped.emit(self._selected_mod.mod_id, image_path)
        elif self._panel_mode == "category" and self._selected_category:
            self.category_preview_dropped.emit(self._selected_category.get("id", ""), image_path)

    def _on_panel_delete_current(self, index: int) -> None:
        """使用预览控件的实际图片路径，避免 _selected_mod 过期导致删除无效。"""
        if self._panel_mode == "mod" and self._selected_mod:
            # 优先从预览控件获取当前显示的图片路径
            widget_images = self._panel_preview._images
            if 0 <= index < len(widget_images):
                image_path = widget_images[index]
            elif 0 <= index < len(self._selected_mod.preview_images):
                image_path = self._selected_mod.preview_images[index]
            else:
                return
            mod_id = self._selected_mod.mod_id
            reply = QMessageBox.question(
                self,
                tr("确认删除预览图"),
                tr("确定删除当前预览图吗？\n将删除对应的图片文件，此操作不可恢复。"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.mod_preview_delete_requested.emit(mod_id, image_path)
        elif self._panel_mode == "category" and self._selected_category:
            # 删除类别预览图。
            cat_id = self._selected_category.get("id", "")
            self.category_preview_dropped.emit(cat_id, "")  # 空路径表示删除预览图

    def _on_panel_delete_all(self) -> None:
        """确认后向宿主请求删除当前 Mod 的全部预览图片。"""
        if self._panel_mode == "mod" and self._selected_mod:
            mod_id = self._selected_mod.mod_id
            reply = QMessageBox.question(
                self,
                tr("确认删除所有预览图"),
                tr("确定删除该 Mod 的所有预览图吗？\n将删除对应的图片文件，此操作不可恢复。"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.mod_preview_delete_all_requested.emit(mod_id)

    def _update_card_preview_by_path(self, mod_id: str, image_path: str) -> None:
        """将指定Mod卡片预览图更新为特定路径"""
        card = self._vs_card_cache.get(mod_id)
        if card and isinstance(card, ModCard):
            card.show_preview_by_path(image_path)

    def _on_main_splitter_moved(self, pos: int, index: int) -> None:
        """拖动分割线后重新排列卡片并保存预览栏宽度。"""
        self._resize_timer.start()
        sizes = self._main_splitter.sizes()
        if len(sizes) >= 2 and sizes[1] > 100:  # >100 视作真实宽度（排除拖拽折叠到 ~0 的情形）
            from src.core.config import Config as _Cfg

            _Cfg.instance().set("ui.overview_preview_width", int(sizes[1]))

    def _on_panel_splitter_moved(self, pos: int, index: int) -> None:
        """保存预览与文本区域的分栏高度。"""
        sizes = self._panel_splitter.sizes()
        if len(sizes) == 2 and sum(sizes) > 0:
            from src.core.config import Config as _Cfg

            _Cfg.instance().set("ui.overview_preview_split", [int(sizes[0]), int(sizes[1])])

    def _ensure_preview_panel_expanded(self) -> None:
        """选中内容时展开已折叠的预览栏，并播放淡入动画。

        通过 setSizes 一次恢复目标宽度，卡片布局独立处理位置动画。
        预览栏已经展开时保留用户调整的宽度。
        """
        try:
            sizes = self._main_splitter.sizes()
        except RuntimeError:
            return
        if len(sizes) < 2 or sizes[1] > 8:
            return  # 未折叠：预览栏仍有可见宽度
        total = sizes[0] + sizes[1]
        if total <= 0:
            return
        panel = self._preview_panel
        target = max(380, min(420, total // 3))
        panel.setMinimumWidth(380)  # 恢复展开状态的最小宽度。
        # 先应用分栏尺寸，再重排卡片并显示预览面板。
        self._main_splitter.setSizes([max(0, total - target), target])
        if self._view_mode == "grid" and (self._characters or self._current_mods):
            self._vs_relayout()
        anim.fade_in(panel)

    def _on_adult_mode_changed(self, _index: int = 0) -> None:
        """切换成人级 Mod 预览模式，仅更新 X 级卡片，避免全量重绘。"""
        mode = self._adult_mode_combo.currentData() or "show"
        self._adult_mode = mode
        if self._view_mode == "grid" and self._vs_card_cache:
            self._scroll_content.setUpdatesEnabled(False)
            for item_id, card in self._vs_card_cache.items():
                if isinstance(card, ModCard):
                    # 所有卡片都要记住最新策略。非 X 卡片无需立即重绘，但之后若被
                    # 编辑为 X，必须使用当前模式，不能保留创建时的 show/hide 状态。
                    card._adult_mode = mode
                    idx = self._display_idx_map.get(item_id)
                    if idx is not None and idx < len(self._display_items):
                        mod = self._display_items[idx]["data"]
                        if mod.info.age_rating == AgeRating.X:
                            card.set_mod(mod)
            self._scroll_content.setUpdatesEnabled(True)
            self._scroll_content.update()
        else:
            # 列表视图无图片，直接刷新即可
            self._refresh_current_level()
        self.adult_mode_changed.emit(mode)

    def set_adult_mode(self, mode: str) -> None:
        """外部同步成人级预览模式（show|hide|blur）"""
        if mode not in ("show", "hide", "blur"):
            mode = "show"
        self._adult_mode = mode
        self._adult_mode_combo.blockSignals(True)
        idx = self._adult_mode_combo.findData(mode)
        if idx >= 0:
            self._adult_mode_combo.setCurrentIndex(idx)
        self._adult_mode_combo.blockSignals(False)
        if self._view_mode == "grid" and self._vs_card_cache:
            self._scroll_content.setUpdatesEnabled(False)
            for item_id, card in self._vs_card_cache.items():
                if isinstance(card, ModCard):
                    card._adult_mode = mode
                    idx2 = self._display_idx_map.get(item_id)
                    if idx2 is not None and idx2 < len(self._display_items):
                        mod = self._display_items[idx2]["data"]
                        if mod.info.age_rating == AgeRating.X:
                            card.set_mod(mod)
            self._scroll_content.setUpdatesEnabled(True)
            self._scroll_content.update()
        if self._selected_mod:
            self._show_mod_panel(self._selected_mod)

    def set_mod_manager(self, manager) -> None:
        """注入 ModManager（供按键面板 parse/edit override）。主窗口构造后调用一次。"""
        self._mod_manager = manager
        self.refresh_tags(refresh_view=False)

    def update_mod_in_view(self, mod: Mod) -> None:
        """外部调用：更新某个Mod在视图中的状态 - 就地更新卡片，避免全量刷新"""
        self.update_mods_in_view([mod])

    def update_mods_in_view(self, mods: list[Mod]) -> None:
        """一次发布元数据批次并更新筛选和布局。"""
        self.apply_reconcile_delta(mods, ())

    # 键盘快捷键

    def install_shortcuts(self, dispatcher) -> None:
        """仅把当前列表及其卡片登记为操作目标，预览区/工具栏不继承列表动作。"""
        self._shortcut_settings = dispatcher.settings
        actions = {
            "edit.select_all": self._select_all,
            "edit.cut": self._cut_selected,
            "edit.paste": self._paste_items,
            "edit.delete": self._delete_selected,
            "selection.edit": self._edit_selected,
            "mod.toggle": self._toggle_selected,
            "selection.export": self._export_selected,
        }
        dispatcher.register_context(self, "overview", actions, children=False)
        dispatcher.register_context(self._scroll_area, "overview", actions)
        dispatcher.register_context(self._table, "overview", actions)
        self._preset_selector.install_shortcuts(dispatcher)

    def _edit_selected(self) -> None:
        """仅在选中一项时打开分类编辑或发布 Mod 编辑请求。"""
        categories, mods = self._split_selected_ids()
        if len(categories) + len(mods) != 1:
            return
        if categories:
            self._on_edit_character(categories[0])
        else:
            self.mod_edit_requested.emit(mods[0])

    def _toggle_selected(self) -> None:
        """仅选中单个 Mod 且没有在途部署时发布启用状态切换请求。"""
        categories, mods = self._split_selected_ids()
        if not categories and len(mods) == 1 and mods[0] not in self._deploying_frac:
            self.mod_toggle_requested.emit(mods[0])

    def _export_selected(self) -> None:
        """将当前选择拆分为分类和 Mod 标识，存在可导出项时通知宿主。"""
        categories, mods = self._split_selected_ids()
        if categories or mods:
            self.items_export_requested.emit(categories, mods)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc 保持固定取消语义；可配置命令统一由分派器处理。"""
        if event.key() == Qt.Key.Key_Escape:
            # Esc 取消剪切。
            if self._clipboard_cat_ids or self._clipboard_mod_ids:
                self._cancel_cut()
                return

        super().keyPressEvent(event)

    def _select_all(self) -> None:
        """Ctrl+A: 选中当前层级所有项目"""
        if self._view_mode == "list":
            self._table.selectAll()
            return
        self._selected_ids.clear()
        for item in self._display_items:
            self._selected_ids.add(item["id"])
        self._update_card_selection()
        # 多选时显示汇总面板
        if len(self._selected_ids) > 1:
            self._selected_mod = None
            self._selected_category = None
            self._show_multi_select_panel()

    # 右键菜单：选中感知的删除 / 导出

    def _select_for_context_menu(self, item_id: str) -> None:
        """右键目标未选中时先单选；目标已在多选集合内则保持选择，并同步面板及高亮。"""
        if not item_id or item_id in self._selected_ids:
            return
        self._selected_ids.clear()
        self._selected_ids.add(item_id)
        self._last_clicked_index = self._find_display_index(item_id)
        self._show_selection_panel()
        self._update_card_selection()
        if self._view_mode == "list":
            self._sync_table_selection_visual()

    def _sync_table_selection_visual(self) -> None:
        """按业务 ID 恢复行高亮；批量同步期间不反向覆盖业务选区。"""
        if not hasattr(self, "_table"):
            return
        selection = QItemSelection()
        model = self._table.model()
        for i, item in enumerate(self._display_items):
            if item["id"] in self._selected_ids:
                selection.select(model.index(i, 0), model.index(i, self._table.columnCount() - 1))
        with QSignalBlocker(self._table):
            self._table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
            )

    def _split_selected_ids(self) -> tuple[list, list]:
        """把当前选中项按类型拆成 (cat_ids, mod_ids)。"""
        cat_ids: list[str] = []
        mod_ids: list[str] = []
        for item in self._display_items:
            if item["id"] in self._selected_ids:
                if item["type"] == "category":
                    cat_ids.append(item["id"])
                else:
                    mod_ids.append(item["id"])
        return cat_ids, mod_ids

    def _export_from_context(self, item_id: str, *, is_mod: bool) -> None:
        """右键「导出」。多选时导出整个选择，否则只导出右键这一项。

        类别 id 交给 MainWindow 展开成其子树下的全部 Mod 再导出。
        """
        if item_id in self._selected_ids and len(self._selected_ids) > 1:
            cat_ids, mod_ids = self._split_selected_ids()
        elif is_mod:
            cat_ids, mod_ids = [], [item_id]
        else:
            cat_ids, mod_ids = [item_id], []
        if not cat_ids and not mod_ids:
            return
        self.items_export_requested.emit(cat_ids, mod_ids)

    def _on_category_card_delete(self, cat_id: str) -> None:
        """删除右键类别；目标属于当前多选集合时执行批量删除。"""
        if cat_id in self._selected_ids and len(self._selected_ids) > 1:
            self._delete_selected()
        else:
            self.character_delete_requested.emit(cat_id)

    def _on_category_card_export(self, cat_id: str) -> None:
        """将分类卡片的导出操作转交统一的上下文导出入口。"""
        self._export_from_context(cat_id, is_mod=False)

    def _delete_selected(self) -> None:
        """Delete: 删除所有选中的类别和Mod"""
        if not self._selected_ids:
            return

        cat_ids = []
        mod_ids = []
        for item in self._display_items:
            if item["id"] in self._selected_ids:
                if item["type"] == "category":
                    cat_ids.append(item["id"])
                else:
                    mod_ids.append(item["id"])

        # 单个 Mod 时使用单独的删除信号（走单条删除逻辑，含已启用提示）
        if not cat_ids and len(mod_ids) == 1:
            self.mod_delete_requested.emit(mod_ids[0])
            # 列表只有实际删除后的重建才移除选择；取消确认仍保留当前行。
            if self._view_mode != "list":
                self._selected_ids.clear()
                self._selected_mod = None
                self._hide_panel()
            return

        parts = []
        if cat_ids:
            parts.append(tr("{count} 个类别", count=len(cat_ids)))
        if mod_ids:
            parts.append(tr("{count} 个 Mod", count=len(mod_ids)))
        if mod_ids and not cat_ids:
            from src.core.config import Config

            delete_method = Config().get("mods.delete_method", "permanent")
            if delete_method == "recycle_bin":
                msg = tr(
                    "确定将选中的 {value} 放入系统回收站？\n若路径过长或当前位置不支持回收站，会再次询问是否直接删除。",
                    value=tr("和").join(parts),
                )
            else:
                msg = tr("确定删除选中的 {value}？\n此操作不可恢复。", value=tr("和").join(parts))
        else:
            msg = tr("确定删除选中的 {value}？\n此操作不可恢复。", value=tr("和").join(parts))

        reply = QMessageBox.question(
            self,
            tr("确认删除"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if cat_ids:
            self.character_batch_delete_requested.emit(cat_ids)
        if mod_ids:
            self.mod_batch_delete_requested.emit(mod_ids)

        if self._view_mode != "list":
            self._selected_ids.clear()
            self._selected_mod = None
            self._hide_panel()

    def _cut_selected(self) -> None:
        """剪切选中项目并更新视觉反馈。"""
        if not self._selected_ids:
            return
        self._clipboard_cat_ids.clear()
        self._clipboard_mod_ids.clear()
        self._clipboard_source_parent = self._current_parent_id
        for item in self._display_items:
            if item["id"] in self._selected_ids:
                if item["type"] == "category":
                    self._clipboard_cat_ids.append(item["id"])
                else:
                    self._clipboard_mod_ids.append(item["id"])
        # 为剪切的卡片应用虚线和半透明样式。
        total = len(self._clipboard_cat_ids) + len(self._clipboard_mod_ids)
        settings = getattr(self, "_shortcut_settings", None)
        paste = settings.binding("edit.paste") if settings else "Ctrl+V"
        paste_hint = tr("{paste} 粘贴", paste=paste) if paste else tr("粘贴未绑定")
        self._status_label.setText(
            tr("已剪切 {total} 个项目 ({paste_hint}, Esc 取消)", total=total, paste_hint=paste_hint)
        )
        self._update_cut_visual()

    def _cancel_cut(self) -> None:
        """取消剪切。"""
        self._clipboard_cat_ids.clear()
        self._clipboard_mod_ids.clear()
        self._clipboard_source_parent = ""
        self._status_label.setText(tr("已取消剪切"))
        self._update_cut_visual()

    def _update_cut_visual(self) -> None:
        """更新剪切状态的视觉反馈。"""
        cut_ids = set(self._clipboard_cat_ids + self._clipboard_mod_ids)
        for item_id, card in self._vs_card_cache.items():
            is_cut = item_id in cut_ids
            if isinstance(card, CharacterCard):
                card.set_cut_state(is_cut)
            elif isinstance(card, ModCard):
                card.set_cut_state(is_cut)

    def _paste_items(self) -> None:
        """Ctrl+V: 将剪切的项目粘贴到当前目录"""
        if not self._clipboard_cat_ids and not self._clipboard_mod_ids:
            return
        # 同级目录不做操作
        if self._clipboard_source_parent == self._current_parent_id:
            self._status_label.setText(tr("同级目录下粘贴无效"))
            return
        self.items_move_requested.emit(
            self._clipboard_cat_ids,
            self._clipboard_mod_ids,
            self._current_parent_id,
        )
        total = len(self._clipboard_cat_ids) + len(self._clipboard_mod_ids)
        self._status_label.setText(tr("已粘贴 {total} 个项目", total=total))
        self._clipboard_cat_ids.clear()
        self._clipboard_mod_ids.clear()
        self._clipboard_source_parent = ""

    # 拖拽导入

    def dragEnterEvent(self, event) -> None:
        """仅检查外部 URL 格式以接受拖放，不在进入事件中读取实际路径列表。"""
        # 仅检查 hasUrls 即可，不调用 urls 避免大量文件时 IPC 卡顿
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        """拖拽移动时高亮光标下的类别/角色卡片（仅对单张图片拖拽场景）"""
        event.acceptProposedAction()
        # 尝试检测光标下的 CharacterCard 并添加高亮
        gpos = event.position().toPoint()
        card = self._find_card_at_global_pos(gpos)
        target_card = card if isinstance(card, CharacterCard) else None
        if target_card != self._drag_hover_card:
            # 恢复上一个高亮卡片
            if self._drag_hover_card is not None:
                self._drag_hover_card._apply_style()
            self._drag_hover_card = target_card
            # 高亮新卡片
            if target_card is not None:
                target_card.setStyleSheet(
                    f"#charCard {{ border: 2px dashed {theme.color('accent')}; "
                    f"background-color: rgba(30, 136, 229, 0.15); }}"
                )

    def dragLeaveEvent(self, event) -> None:
        """拖拽离开时恢复高亮卡片样式"""
        if self._drag_hover_card is not None:
            self._drag_hover_card._apply_style()
            self._drag_hover_card = None
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        """处理拖入分类视图的图片或 Mod 路径。

        单张预览图就地处理，批量路径解析及导入交给后台任务。
        """
        # 恢复拖拽悬停高亮
        if self._drag_hover_card is not None:
            self._drag_hover_card._apply_style()
            self._drag_hover_card = None
        # 立即接受事件，释放拖拽源窗口（Explorer）
        event.acceptProposedAction()
        mime = event.mimeData()

        # 快速路径: 直接获取 uri-list 原始字节
        raw_data = mime.data("text/uri-list")
        if raw_data and not raw_data.isEmpty():
            raw_bytes = bytes(raw_data)

            # 类别卡片上的少量拖入数据优先按预览图片处理。
            if len(raw_bytes) < 2048:
                paths = self._parse_urilist_quick(raw_bytes)
                if len(paths) == 1:
                    p_lower = paths[0].lower()
                    if any(p_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")):
                        # 查找光标下的卡片
                        gpos = event.position().toPoint()
                        card = self._find_card_at_global_pos(gpos)
                        if card and isinstance(card, CharacterCard):
                            self.category_preview_dropped.emit(card._data.get("id", ""), paths[0])
                            return

            # Mod 导入的大量路径由后台线程收集。
            if not self._current_parent_id:
                QMessageBox.information(self, tr("提示"), tr("请先进入一个类别后再导入 Mod。"))
                return
            parent_id = self._current_parent_id
            call_later(0, self, lambda: self._process_raw_urilist_async(raw_bytes, parent_id))
        else:
            if not self._current_parent_id:
                QMessageBox.information(self, tr("提示"), tr("请先进入一个类别后再导入 Mod。"))
                return
            # 回退: 少量文件时直接用urls
            url_strings = []
            for url in mime.urls():
                if url.isLocalFile():
                    url_strings.append(url.toLocalFile())
            if url_strings:
                parent_id = self._current_parent_id
                call_later(0, self, lambda: self._process_dropped_urls_async(url_strings))

    @staticmethod
    def _parse_urilist_quick(raw_bytes: bytes) -> list[str]:
        """快速解析小型 uri-list，返回本地路径列表"""
        import urllib.parse as _up

        paths: list[str] = []
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = raw_bytes.decode("latin-1", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("file:///"):
                path = _up.unquote(line[8:])
                path = path.replace("/", "\\") if "\\" not in path else path
                paths.append(path)
            elif line.startswith("file://"):
                path = _up.unquote(line[7:])
                path = path.replace("/", "\\") if "\\" not in path else path
                paths.append(path)
        return paths

    def _find_card_at_global_pos(self, pos) -> CharacterCard | None:
        """查找指定位置下的 CharacterCard（用于拖拽预览图检测）"""
        # pos 是相对于 OverviewPage 的坐标
        child = self.childAt(pos)
        while child is not None:
            if isinstance(child, CharacterCard):
                return child
            child = child.parentWidget()
            if child is self or child is None:
                break
        return None

    def _process_raw_urilist_async(self, raw_bytes: bytes, parent_id: str) -> None:
        """在后台线程中解析 uri-list 原始字节并过滤有效 Mod 路径
        比主线程逐个创建 QUrl 快得多，大幅减少 UI 卡顿。
        """
        import os as _os
        import stat as _stat
        import urllib.parse

        from PySide6.QtCore import QThread

        class _RawPathCollector(QThread):
            """后台线程：从 uri-list 原始字节中提取并过滤 Mod 路径"""

            def __init__(self, raw, extensions):
                """保存原始 URI 列表及允许的扩展名，建立后台收集结果列表。"""
                super().__init__()
                self.raw = raw
                self.extensions = extensions
                self.result_paths: list[str] = []

            def run(self):
                """后台解析文件 URI 并检查路径，保留目录及可导入扩展名的项目，跳过无法访问的输入。"""
                ext_set = self.extensions
                # 解析 text/uri-list: 每行一个 file:///path URL
                try:
                    text = self.raw.decode("utf-8", errors="replace")
                except Exception:
                    text = self.raw.decode("latin-1", errors="replace")

                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # file:///C:/path → C:/path
                    if line.startswith("file:///"):
                        path = urllib.parse.unquote(line[8:])  # 去掉 file:///
                        # Windows 路径修复
                        path = path.replace("/", "\\") if "\\" not in path else path
                    elif line.startswith("file://"):
                        path = urllib.parse.unquote(line[7:])
                        path = path.replace("/", "\\") if "\\" not in path else path
                    else:
                        continue

                    try:
                        st = _os.stat(path)
                        if _stat.S_ISDIR(st.st_mode):
                            self.result_paths.append(path)
                        else:
                            dot_idx = path.rfind(".")
                            if dot_idx >= 0 and path[dot_idx:].lower() in ext_set:
                                self.result_paths.append(path)
                    except OSError:
                        pass

        self._drop_collector = _RawPathCollector(raw_bytes, ARCHIVE_EXTENSIONS)

        def _on_done():
            """将已收集路径发送到发起时的目录，并释放原始 URI 收集器引用。"""
            paths = self._drop_collector.result_paths
            if paths:
                self.files_dropped_for_import.emit(paths, parent_id)
            self._drop_collector.deleteLater()
            self._drop_collector = None

        self._drop_collector.finished.connect(_on_done)
        self._drop_collector.start()

    def _process_dropped_urls_async(self, url_strings: list[str]) -> None:
        """使用后台线程处理拖入的路径列表，避免大数据量拖拽时UI卡顿。
        使用 os.stat 替代 Path.is_dir 以获得更好的系统调用性能。
        """
        import os as _os
        import stat as _stat

        from PySide6.QtCore import QThread

        class _PathCollector(QThread):
            """后台线程：过滤有效的Mod路径（用 os.stat 代替 pathlib 降低开销）"""

            def __init__(self, urls, extensions):
                """保存已转换的本地路径及允许的扩展名，初始化有效路径结果列表。"""
                super().__init__()
                self.urls = urls
                self.extensions = extensions
                self.result_paths: list[str] = []

            def run(self):
                """后台检查本地路径，保留目录和可导入扩展名的项目，忽略无法访问的输入。"""
                ext_set = self.extensions  # 减少属性查找
                for u in self.urls:
                    try:
                        st = _os.stat(u)
                        if _stat.S_ISDIR(st.st_mode):
                            self.result_paths.append(u)
                        else:
                            # 检查扩展名
                            dot_idx = u.rfind(".")
                            if dot_idx >= 0 and u[dot_idx:].lower() in ext_set:
                                self.result_paths.append(u)
                    except OSError:
                        pass

        self._drop_collector = _PathCollector(url_strings, ARCHIVE_EXTENSIONS)
        parent_id = self._current_parent_id

        def _on_done():
            """将筛选后的本地路径发送到发起时的目录，并释放路径收集器引用。"""
            paths = self._drop_collector.result_paths
            if paths:
                self.files_dropped_for_import.emit(paths, parent_id)
            self._drop_collector.deleteLater()
            self._drop_collector = None

        self._drop_collector.finished.connect(_on_done)
        self._drop_collector.start()

    # 空白处点击取消选中

    def mousePressEvent(self, event) -> None:
        """点击页面空白处时取消选中，并保留卡片和滚动区的点击处理。"""
        child = self.childAt(event.pos())
        # 检查是否点击在卡片上
        is_on_card = child is not None and self._is_child_of_card(child)
        # 检查是否点击在scroll区域内（包含滚动区域视口和内容widget）
        is_on_scroll = child is not None and self._is_child_of_scroll(child)
        # 右侧预览栏的布局空白会把未接受的鼠标事件冒泡到本页。只要落点仍在
        # 预览栏矩形内，就属于当前 Mod 的操作上下文，不能按页面空白处理。
        pos = event.position().toPoint()
        is_on_preview_panel = self._is_point_in_preview_panel(pos)
        if not is_on_card and not is_on_scroll and not is_on_preview_panel:
            self._selected_ids.clear()
            self._selected_mod = None
            self._selected_category = None
            self._last_clicked_index = -1
            self._hide_panel()
            self._update_card_selection()
        super().mousePressEvent(event)

    def _is_point_in_preview_panel(self, pos: QPoint) -> bool:
        """判断 OverviewPage 坐标中的点是否落在可见预览栏内。"""
        if not self._preview_panel.isVisible():
            return False
        panel_pos = self._preview_panel.mapFrom(self, pos)
        return self._preview_panel.rect().contains(panel_pos)

    def _is_child_of_card(self, widget) -> bool:
        """判断widget是否为卡片的子控件"""
        parent = widget
        while parent is not None:
            if isinstance(parent, (CharacterCard, ModCard)):
                return True
            parent = parent.parentWidget()
        return False

    def _is_child_of_scroll(self, widget) -> bool:
        """判断widget是否在滚动区域内"""
        parent = widget
        while parent is not None:
            if parent is self._scroll_area or parent is self._scroll_content:
                return True
            parent = parent.parentWidget()
        return False

    def eventFilter(self, obj, event) -> bool:
        """处理滚动区视口的空白点击，并在列表空白处被点击时清理预览。"""
        if (
            event.type() == QEvent.Type.DevicePixelRatioChange
            and obj is self._scroll_area.viewport()
            and not self._mods_loading
        ):
            self._vs_scroll_timer.start()
        if hasattr(self, "_table") and obj is self._table.horizontalHeader().viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                self._overview_header_dragging = True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._overview_header_dragging = False
            return False
        # 松开鼠标时停止列表视图的自动滚动
        if event.type() == QEvent.Type.MouseButtonRelease:
            if hasattr(self, "_table") and obj is self._table.viewport():
                self._table.setAutoScroll(False)
                self._table.setAutoScroll(True)
        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if obj is self._scroll_area.viewport():
                # 将视口坐标映射到 scroll_content
                pos = self._scroll_content.mapFrom(self._scroll_area.viewport(), event.pos())
                child = self._scroll_content.childAt(pos)
                is_on_card = child is not None and self._is_child_of_card(child)
                if not is_on_card:
                    if mouse_event.button() == Qt.MouseButton.RightButton:
                        # 右键空白处弹出菜单
                        self._show_empty_area_context_menu(self._scroll_area.viewport().mapToGlobal(event.pos()))
                        return True
                    self._flush_pending_preview_reorder()
                    self._selected_ids.clear()
                    self._selected_mod = None
                    self._selected_category = None
                    self._last_clicked_index = -1
                    self._hide_panel()
                    self._update_card_selection()
            elif obj is self._table.viewport():
                index = self._table.indexAt(event.position().toPoint())
                if not index.isValid():
                    if mouse_event.button() == Qt.MouseButton.RightButton:
                        # 列表视图空白处右键菜单
                        self._show_empty_area_context_menu(
                            self._table.viewport().mapToGlobal(event.position().toPoint())
                        )
                        return True
                    self._flush_pending_preview_reorder()
                    self._table.clearSelection()
                    self._selected_ids.clear()
                    self._selected_mod = None
                    self._selected_category = None
                    self._last_clicked_index = -1
                    self._hide_panel()
                    self._update_status_bar_selection()
        return super().eventFilter(obj, event)

    def _on_search(self, text: str) -> None:
        """保存最新搜索条件并重新计时，清空搜索时立即刷新，非空搜索合并延后执行。"""
        self._search_token += 1
        self._pending_search_text = text.strip()
        self._search_timer.stop()

        if not self._pending_search_text:
            self._set_searching_state(False)
            self._execute_search_refresh(force_text="")
            return

        self._set_searching_state(True)
        self._search_timer.start()

    def _on_filter_changed(self, _index: int = 0) -> None:
        """处理类型或分级筛选变更。"""
        # 分级筛选仅在选择 Mod 类型后显示。
        type_filter = self._type_filter.currentData() or ""
        self._tag_filter.set_mod_types_enabled(type_filter not in ("category", "character"))
        self._age_filter.setVisible(type_filter == "mod")
        if type_filter != "mod":
            self._age_filter.blockSignals(True)
            self._age_filter.setCurrentIndex(0)
            self._age_filter.blockSignals(False)
        if self._mods_loading:
            self._execute_search_refresh(force_text=self._search_input.text())
            return
        if self._view_mode == "grid":
            self._refresh_cards(self._search_input.text().strip())
        else:
            self._refresh_table(self._search_input.text().strip())

    def _on_sort_changed(self, _index: int = 0) -> None:
        """排序方式变更 — 先持久化预览图切换状态，刷新后恢复选中面板"""
        self._sort_key = self._sort_combo.currentData() or "alpha_asc"
        if self._sort_key in _ALPHABETICAL_SORTS:
            self._category_sort_key = self._sort_key

        # 记住当前选中的Mod和正在显示的预览图 *路径*（而非索引，因为 flush 会重排列表）
        saved_mod = self._selected_mod
        saved_preview_path = ""
        if saved_mod and saved_mod.preview_images:
            idx = self._panel_preview._current_index
            if 0 <= idx < len(saved_mod.preview_images):
                saved_preview_path = saved_mod.preview_images[idx]

        # 持久化当前选中Mod的预览图切换（可能重排 preview_images，使已选图移至 index 0）
        self._flush_pending_preview_reorder()

        if self._view_mode == "grid":
            self._refresh_cards(self._search_input.text().strip())
        else:
            self._refresh_table(self._search_input.text().strip())

        # 恢复选中Mod的面板状态（用路径而非索引定位，避免 flush 重排后错位）
        if saved_mod:
            # flush 后 _selected_mod / _current_mods 内的对象已被 main_window 替换，
            # 重新获取最新引用
            fresh_mod = self._mod_by_id(saved_mod.mod_id)
            if fresh_mod is None:
                fresh_mod = saved_mod

            self._selected_mod = fresh_mod
            self._show_mod_panel(fresh_mod)

            # 按路径找到当前图在（可能重排过的）列表中的新位置
            if saved_preview_path and fresh_mod.preview_images:
                try:
                    new_idx = fresh_mod.preview_images.index(saved_preview_path)
                except ValueError:
                    new_idx = 0
                if new_idx != 0:
                    self._panel_preview._current_index = new_idx
                    self._panel_preview._show_current()
                    self._panel_preview._update_nav()
                # 同步卡片预览图
                self._update_card_preview_by_path(fresh_mod.mod_id, saved_preview_path)

    def _apply_sort(self, cats: list[dict], mods: list[Mod]) -> tuple[list[dict], list[Mod]]:
        """类别沿用最近的字母排序方向，Mod 使用当前排序规则。"""
        category_sort = self._sort_key if self._sort_key in _ALPHABETICAL_SORTS else self._category_sort_key
        cats = sorted(
            cats,
            key=lambda c: self._sort_text_key(c.get("name", "")),
            reverse=category_sort == "alpha_desc",
        )
        if self._sort_key == "alpha_asc":
            mods = sorted(mods, key=lambda m: self._sort_text_key(m.display_name))
        elif self._sort_key == "alpha_desc":
            mods = sorted(mods, key=lambda m: self._sort_text_key(m.display_name), reverse=True)
        elif self._sort_key == "time_asc":
            mods = sorted(mods, key=lambda m: m.imported_at or "")
        elif self._sort_key == "time_desc":
            mods = sorted(mods, key=lambda m: m.imported_at or "", reverse=True)
        elif self._sort_key == "author_asc":
            mods_with_author = [m for m in mods if (m.info.author or "").strip()]
            mods_without_author = [m for m in mods if not (m.info.author or "").strip()]
            mods_with_author = sorted(
                mods_with_author,
                key=lambda m: (
                    self._sort_text_key((m.info.author or "").strip()),
                    self._sort_text_key(m.display_name),
                ),
            )
            mods = mods_with_author + mods_without_author
        elif self._sort_key == "author_desc":
            mods_with_author = [m for m in mods if (m.info.author or "").strip()]
            mods_without_author = [m for m in mods if not (m.info.author or "").strip()]
            mods_with_author = sorted(
                mods_with_author,
                key=lambda m: (
                    self._sort_text_key((m.info.author or "").strip()),
                    self._sort_text_key(m.display_name),
                ),
                reverse=True,
            )
            mods = mods_with_author + mods_without_author
        elif self._sort_key == "status_desc":
            mods = sorted(
                mods,
                key=lambda m: (
                    0 if m.is_enabled else 1,
                    self._sort_text_key(m.display_name),
                ),
            )
        elif self._sort_key == "age_asc":
            rating_rank = {"G": 0, "P": 1, "R": 2, "X": 3}
            mods = sorted(
                mods,
                key=lambda m: (
                    rating_rank.get(m.info.age_rating.value, 999),
                    self._sort_text_key(m.display_name),
                ),
            )
        elif self._sort_key == "age_desc":
            rating_rank = {"G": 0, "P": 1, "R": 2, "X": 3}
            mods = sorted(
                mods,
                key=lambda m: (
                    -rating_rank.get(m.info.age_rating.value, -1),
                    self._sort_text_key(m.display_name),
                ),
            )
        return cats, mods

    def _on_card_scale_changed(self, value: int) -> None:
        """即时调整卡片容器与布局，非拖动输入通过定时器延后完成图片缩放。"""
        self._card_scale = value
        if self._view_mode == "grid" and not self._mods_loading:
            self._begin_card_scale()
            # 即时轻量更新（仅容器尺寸+布局，不缩放图片）
            self._apply_card_scale_fast()
            # 键盘和滚轮调整不会发出滑块释放信号。
            if not self._card_size_slider.isSliderDown():
                self._scale_timer.start()

    def _begin_card_scale(self) -> None:
        """进入卡片缩放手势状态，停止出现效果和预取，并通知边缘效果进入尺寸调整模式。"""
        self._scale_timer.stop()
        if self._card_scale_active or self._view_mode != "grid" or self._mods_loading:
            return
        self._card_scale_active = True
        self._cancel_directory_intro()
        self._vs_invalidate_prefetch()
        self._scroll_edge_fade.set_resizing(True)

    def _end_card_scale(self) -> None:
        """结束缩放并按需保存比例，统一卡片尺寸、作废不匹配的预览，再安排视口更新。"""
        self._scale_timer.stop()
        if not self._card_scale_active:
            return
        self._card_scale_active = False
        self._scroll_edge_fade.set_resizing(False, deferred=True)
        from src.core.config import Config as _Cfg

        if _Cfg.instance().get("ui.overview_card_scale") != self._card_scale:
            _Cfg.instance().set("ui.overview_card_scale", int(self._card_scale))
        for card in self._vs_card_cache.values():
            changed = card._card_width != self._vs_card_w or card._card_height != self._vs_card_h
            if isinstance(card, CharacterCard):
                card.resize_card(self._vs_card_w, self._vs_card_h, defer_preview=True)
            else:
                card.resize_card_fast(self._vs_card_w, self._vs_card_h)
            if changed and card._cached_pixmap is not None:
                card._preview_resolved = False
        self._vs_window = None
        # 隐藏或切换范围也可能结束手势，加载和关闭会停止调度，普通显示可恢复。
        self._vs_scroll_timer.start()

    def _apply_card_scale_fast(self) -> None:
        """即时轻量卡片缩放 — 更新虚拟滚动布局参数并重定位可见卡片"""
        if self._mods_loading or self._view_mode != "grid" or not self._display_items:
            return
        scaled_w = int(CharacterCard.CARD_WIDTH * self._card_scale / 100)
        scaled_h = int(CharacterCard.CARD_HEIGHT * self._card_scale / 100)
        avail_w = (
            self._main_splitter.widget(0).width() if self._main_splitter.widget(0).width() > 100 else self.width() - 48
        )
        cols = max(1, (avail_w - 48) // (scaled_w + 12))

        # 更新虚拟滚动布局参数
        self._vs_card_w = scaled_w
        self._vs_card_h = scaled_h
        self._vs_row_h = scaled_h + 12
        self._vs_cols = cols
        self._vs_total_rows = (len(self._display_items) + cols - 1) // cols
        total_h = self._vs_total_rows * self._vs_row_h + 12
        self._scroll_content.setMinimumHeight(total_h)
        self._scroll_content.setMaximumHeight(total_h)
        self._last_cards_per_row = cols

        # 调整已缓存卡片的尺寸并重新定位
        spacing = 12
        x_pad = 6
        self._scroll_content.setUpdatesEnabled(False)
        for item_id, card in self._vs_card_cache.items():
            changed = card._card_width != scaled_w or card._card_height != scaled_h
            if self._card_scale_active:
                # 手势期间缩放已有表面，仅更新外部边界，结束时统一更新字体、标签和子控件尺寸。
                card.setFixedSize(scaled_w, scaled_h)
            elif isinstance(card, CharacterCard):
                card.resize_card(scaled_w, scaled_h, defer_preview=True)
            elif isinstance(card, ModCard):
                card.resize_card_fast(scaled_w, scaled_h)
            if not self._card_scale_active and changed and card._cached_pixmap is not None:
                card._preview_resolved = False
            idx = self._display_idx_map.get(item_id, -1)
            if idx >= 0:
                row = idx // cols
                col = idx % cols
                card.move(x_pad + col * (scaled_w + spacing), spacing // 2 + row * self._vs_row_h)
        self._scroll_content.setUpdatesEnabled(True)
        self._scroll_content.update()

        # 缩放后可能需要创建/回收卡片
        self._vs_update_visible()

    def _apply_card_scale_pixmaps(self) -> None:
        """最终模糊和裁剪仍在有界后台预览队列执行。"""
        if self._card_size_slider.isSliderDown():
            return
        self._end_card_scale()
        from src.core.config import Config as _Cfg

        if _Cfg.instance().get("ui.overview_card_scale") != self._card_scale:
            _Cfg.instance().set("ui.overview_card_scale", int(self._card_scale))
        if self._mods_loading or self._view_mode != "grid" or not self._vs_card_cache:
            return
        for card in self._vs_card_cache.values():
            self._vs_restore_preview(card)
        self._vs_update_visible()

    # 预设

    def set_presets(self, presets: list, current_preset_id: str = "", rename_id: str | None = None) -> None:
        """填充预设选择器。rename_id 给出时，立即进入该预设的重命名模式"""
        self._preset_selector.set_presets(presets, current_preset_id, rename_id=rename_id)

    def _on_add_character(self) -> None:
        """以同级和全局角色名称约束新建类别，确认后附加当前父目录并通知宿主添加。"""
        # 收集同级已有名称。
        existing = [c.get("name", "") for c in self._characters]
        # 收集当前游戏中全部角色或对象名称。
        all_char_names = [c.get("name", "") for c in self._all_characters if c.get("node_type") == "character"]
        dialog = CharacterDialog(existing_names=existing, all_character_names=all_char_names, parent=self)
        if dialog.exec():
            result = dialog.get_result()
            if result:
                result["parent_id"] = self._current_parent_id
                self.character_add_requested.emit(result)

    def _on_edit_character(self, char_id: str) -> None:
        """查找当前目录中的目标类别，带入重名约束编辑，确认后向宿主提交字段变化。"""
        char_data = None
        for c in self._characters:
            if c.get("id") == char_id:
                char_data = c
                break
        if not char_data:
            return
        # 收集同级已有名称。
        existing = [c.get("name", "") for c in self._characters]
        # 收集当前游戏中全部角色或对象名称。
        all_char_names = [c.get("name", "") for c in self._all_characters if c.get("node_type") == "character"]
        dialog = CharacterDialog(
            char_data=char_data, existing_names=existing, all_character_names=all_char_names, parent=self
        )
        if dialog.exec():
            result = dialog.get_result()
            if result:
                self.character_edit_requested.emit(char_id, result)

    def prepare_batch_add_dialog(self) -> BatchAddCharacterDialog:
        """接受输入前准备静态表单，主题变化时更新局部颜色。"""
        dialog = self._batch_add_dialog
        if dialog is not None and not dialog.isVisible() and dialog._built_theme != theme.current_theme():
            dialog.deleteLater()
            dialog = None
        if dialog is None:
            # 窗口归顶层所有，避免亚克力准备使整张页面变成原生控件。
            dialog = BatchAddCharacterDialog(parent=self.window())
            dialog.layout().activate()
            self._batch_add_dialog = dialog
        return dialog

    def _on_batch_add(self) -> None:
        """批量添加类别 - 弹出批量版对话框"""
        dialog = self.prepare_batch_add_dialog()
        if dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog.prepare_for_open()
        if dialog.exec():
            results = dialog.get_results()
            if results:
                for r in results:
                    r["parent_id"] = self._current_parent_id
                self.character_batch_add_requested.emit(results)

    def _on_batch_disable(self) -> None:
        """批量禁用当前目录及所有子目录下已启用的Mod"""
        if self._enabled_mod_ids_loader is not None:
            try:
                mod_ids = list(self._enabled_mod_ids_loader(self._current_parent_id))
            except Exception:
                logger.exception("Failed to read enabled Mods for the current directory")
                QMessageBox.warning(self, tr("读取失败"), tr("无法读取当前目录的启用状态，请刷新后重试。"))
                return
        else:
            mod_ids = [mod.mod_id for mod in self._subtree_mods if mod.is_enabled]
        if not mod_ids:
            QMessageBox.information(self, tr("提示"), tr("当前目录下没有已启用的Mod。"))
            return

        reply = QMessageBox.question(
            self,
            tr("确认批量禁用"),
            tr("确定禁用当前目录及子目录下的 {count} 个已启用Mod？", count=len(mod_ids)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.batch_disable_requested.emit(mod_ids)

    def resizeEvent(self, event):
        """非加载状态下调整列表列宽，卡片模式则合并安排延后的布局刷新。"""
        super().resizeEvent(event)
        if self._mods_loading:
            return
        if self._view_mode == "list" and not self._overview_header_dragging:
            self._apply_overview_column_widths(force_ratio=not self._overview_user_resized)
        if (self._characters or self._current_mods) and self._view_mode == "grid":
            self._resize_timer.start()

    def _deferred_refresh(self) -> None:
        """窗口/分割线大小变化时的延迟刷新 — 利用虚拟滚动重排"""
        if self._mods_loading:
            return
        if self._view_mode == "grid":
            if self._display_items:
                # 已有数据时只重算列数并重定位
                self._vs_relayout()
            else:
                self._refresh_cards(self._search_input.text().strip())
        else:
            # 状态控件可能改变分隔布局，已有记录仍有效，不重建表格。
            self._apply_overview_column_widths(force_ratio=not self._overview_user_resized)

    def _vs_relayout(self) -> None:
        """窗口/分割线变化后重排活动卡片，回收新窗口外的旧绑定。"""
        if self._mods_loading:
            return
        scaled_w = self._vs_card_w
        avail_w = (
            self._main_splitter.widget(0).width() if self._main_splitter.widget(0).width() > 100 else self.width() - 48
        )
        new_cols = max(1, (avail_w - 48) // (scaled_w + 12))
        if new_cols == self._vs_cols:
            self._vs_update_visible()  # 高度增长也需要补齐新增可见行。
            return
        self._vs_cols = new_cols
        self._last_cards_per_row = new_cols
        self._vs_total_rows = (len(self._display_items) + new_cols - 1) // new_cols
        total_h = self._vs_total_rows * self._vs_row_h + 12
        self._scroll_content.setMinimumHeight(total_h)
        self._scroll_content.setMaximumHeight(total_h)

        # 保留当前绑定的重排动画；移出新窗口的卡片先隐藏再供新行复用。
        self._reflowing = True
        try:
            self._vs_update_visible()
        finally:
            self._reflowing = False

    def _set_view(self, mode: str) -> None:
        """保存切换目标和离开位置并持久化布局模式，加载中保留现有控件，空闲时切换对应视图。"""
        self._end_card_scale()
        self._cancel_directory_intro()
        if mode not in ("grid", "list"):
            mode = "grid"
        self._request_view_position(mode=mode)
        self._view_mode = mode
        self._directory_intro_pending = anim.is_enabled()
        # 记忆视图模式，下次启动恢复（与卡片缩放滑块的持久化对称）。
        try:
            from src.core.config import Config as _Cfg

            _Cfg.instance().set("ui.overview_view_mode", mode)
        except Exception:
            pass
        idx = self._view_combo.findData(mode)
        if idx >= 0 and self._view_combo.currentIndex() != idx:
            self._view_combo.blockSignals(True)
            self._view_combo.setCurrentIndex(idx)
            self._view_combo.blockSignals(False)
        if self._mods_loading:
            return  # 使用当前显示的控件，另一布局可能尚未更新。
        # 卡片大小滑动条仅卡片视图可见
        self._card_size_slider.setVisible(mode == "grid")
        self._card_size_label.setVisible(mode == "grid")
        if mode == "grid":
            self._scroll_area.setVisible(True)
            self._table.setVisible(False)
            self._refresh_cards(self._search_input.text().strip())
        else:
            self._scroll_area.setVisible(False)
            self._refresh_table(self._search_input.text().strip())

    def _refresh_table(self, filter_text: str = "") -> None:
        """刷新列表视图 - 混合显示类别和 Mod"""
        if self._mods_loading:
            return
        view, position = self._prepare_view_position(filter_text)
        restore = self._pending_view is not None or position != 0
        self._table_reveal_gen = getattr(self, "_table_reveal_gen", 0) + 1
        previous_syncing = self._table._syncing_rows
        self._table._syncing_rows = True
        self._applying_position = True
        try:
            self._rebuild_table(filter_text)
            self._commit_view_position(view, position if self._display_items else 0)
        finally:
            self._applying_position = False
            self._table._syncing_rows = previous_syncing
        self._table._sync_visible_rows()
        if self._directory_intro_pending or not restore:
            self._reveal_table_rows(len(self._display_items))
        self._directory_intro_pending = False

    def _rebuild_table(self, filter_text: str) -> None:
        """立即建立行身份与选择映射，其余单元格按需填充，再同步有效选择、空状态和列宽。"""
        filtered_cats, filtered_mods = self._filtered_content(filter_text)

        # 重建 display_items
        category_items = [{"id": cat.get("id", ""), "type": "category", "data": cat} for cat in filtered_cats]
        mod_items = [{"id": mod.mod_id, "type": "mod", "data": mod} for mod in filtered_mods]
        self._display_items = (
            mod_items + category_items if self._sort_key not in _ALPHABETICAL_SORTS else category_items + mod_items
        )
        self._display_idx_map = {item["id"]: i for i, item in enumerate(self._display_items)}

        total_rows = len(self._display_items)
        selection_blocker = QSignalBlocker(self._table)
        self._table.clearContents()
        self._table.setRowCount(total_rows)
        self._table.verticalHeader().setDefaultSectionSize(52)
        # 仅分类行覆盖默认尺寸，不为重置高度重建全部行或改变滚动范围。
        for row in getattr(self, "_table_category_rows", ()):
            if row < total_rows:
                self._table.setRowHeight(row, 52)
        self._table_category_rows = []
        for row in range(min(total_rows, 24)):
            if self._table.isRowHidden(row):
                self._table.setRowHidden(row, False)
        entries = self._display_items
        category_template = QTableWidgetItem(icons.get_icon("meta/category"), tr("类别"))
        category_template.setData(Qt.ItemDataRole.UserRole + 1, "category")
        mod_templates = {}
        for enabled in (False, True):
            template = QTableWidgetItem(icons.get_icon("actions/mod"), "Mod")
            template.setData(Qt.ItemDataRole.UserRole + 1, "mod")
            template.setData(ModListTable.ROLE_ENABLED, enabled)
            mod_templates[enabled] = template
        # 所有身份立即可供选择和快捷键使用，其余单元格按需填充。
        for i, item in enumerate(entries):
            is_mod = item["type"] == "mod"
            template = mod_templates[bool(item["data"].is_enabled)] if is_mod else category_template
            type_item = QTableWidgetItem(template)
            type_item.setData(Qt.ItemDataRole.UserRole, item["id"])
            if is_mod:
                self._table._mod_rows[item["id"]] = i
            self._table.setItem(i, 0, type_item)
            if not is_mod:
                self._table.setRowHeight(i, 32)
                self._table_category_rows.append(i)

        def populate_row(i: int) -> None:
            """按分类或 Mod 类型填充指定行的名称、作者、分级、标签和状态信息。"""
            item = entries[i]
            if item["type"] == "category":
                cat = item["data"]
                self._table.setItem(i, 1, QTableWidgetItem(cat.get("name", tr("未知"))))
                name_en = cat.get("name_en", "")
                name_item = QTableWidgetItem(name_en)
                name_item.setToolTip(name_en)
                self._table.setItem(i, 2, name_item)
                count = cat.get("mod_count", 0)
                enabled = cat.get("enabled_count", 0)
                self._table.setItem(i, 3, QTableWidgetItem("-"))
                self._table.setItem(i, 4, QTableWidgetItem(f"{enabled} / {count}"))
            else:
                mod = item["data"]
                self._set_table_mod_name(i, mod)
                author = mod.info.author or ""
                author_item = QTableWidgetItem(author)
                author_item.setToolTip(author)
                self._table.setItem(i, 2, author_item)
                self._table.setItem(i, 3, QTableWidgetItem(_age_rating_text(mod.info.age_rating)))
                self._set_table_mod_status(i, mod)

        self._table.set_row_factory(populate_row)

        self._prune_selection()
        previous_selection = self._selected_ids.copy()
        self._selected_ids.intersection_update(item["id"] for item in self._display_items)
        self._sync_table_selection_visual()
        selection_blocker.unblock()
        # 尺寸/后台刷新不应重做选择时的预览展开；只有实际选区改变才更新面板。
        if self._selected_ids != previous_selection:
            self._on_table_selection_changed()
        else:
            self._update_card_selection()
        self._update_filter_empty(bool(self._display_items), filter_text)
        self._apply_overview_column_widths(force_ratio=not self._overview_user_resized)

        # 更新状态栏
        self._update_status_bar_selection()

    def _reveal_table_rows(self, total_rows: int) -> None:
        """按当前视口逐行揭示，保持行高、选择和恢复的滚动位置不变。"""
        if total_rows > 0:
            self._table.reveal_visible_rows()

    def _apply_overview_column_widths(self, force_ratio: bool = False, preferred_col: int = 1) -> None:
        """按比例或手动宽度补足视口，自动模式下按优先顺序收缩至各列下限，并防止递归调整。"""
        if self._overview_header_dragging:
            return
        if self._overview_syncing_widths:
            return
        self._overview_syncing_widths = True
        try:
            viewport_w = max(520, self._table.viewport().width())
            if force_ratio:
                used = 0
                for idx in range(4):
                    w = max(self._overview_col_min_widths[idx], int(viewport_w * self._overview_col_ratios[idx]))
                    self._table.setColumnWidth(idx, w)
                    used += w
                self._table.setColumnWidth(4, max(self._overview_col_min_widths[4], viewport_w - used))

            widths = [self._table.columnWidth(i) for i in range(5)]
            total_w = sum(widths)
            if total_w < viewport_w:
                target_col = preferred_col if 0 <= preferred_col < 5 else 1
                self._table.setColumnWidth(target_col, widths[target_col] + (viewport_w - total_w))
                return

            if not force_ratio:
                # 用户已手动调整列宽，不强制收缩，让水平滚动条自然出现
                return

            overflow = total_w - viewport_w
            shrink_order = [1, 2, 4, 3, 0]
            if 0 <= preferred_col < 5 and preferred_col in shrink_order:
                shrink_order = [idx for idx in shrink_order if idx != preferred_col] + [preferred_col]

            for idx in shrink_order:
                if overflow <= 0:
                    break
                cur = self._table.columnWidth(idx)
                min_w = self._overview_col_min_widths[idx]
                can_shrink = max(0, cur - min_w)
                if can_shrink <= 0:
                    continue
                cut = min(overflow, can_shrink)
                self._table.setColumnWidth(idx, cur - cut)
                overflow -= cut
        finally:
            self._overview_syncing_widths = False

    def _on_overview_header_section_resized(self, logical_index: int, old_size: int, new_size: int) -> None:
        """非内部同步且列宽实际变化时记录手动调整状态，使后续布局保留用户列宽。"""
        if self._overview_syncing_widths:
            return
        if old_size == new_size:
            return
        self._overview_user_resized = True

    def _on_table_selection_changed(self) -> None:
        """鼠标、方向键和全选共用 Qt 选区，剪切/删除目标始终与行高亮一致。"""
        selected = self._table.selectionModel().selectedRows()
        self._selected_ids = {index.data(Qt.ItemDataRole.UserRole) for index in selected}
        self._update_card_selection()
        if len(selected) == 1:
            self._last_clicked_index = selected[0].row()
        self._show_selection_panel()

    def _on_table_double_clicked(self, row: int, col: int) -> None:
        """列表双击 → 类别=进入，Mod=切换"""
        item = self._table.item(row, 0)
        if not item:
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        item_type = item.data(Qt.ItemDataRole.UserRole + 1) or "category"
        if item_type == "category":
            self._navigate_to(item_id)
        elif item_id not in self._deploying_frac:
            self.mod_toggle_requested.emit(item_id)

    def _show_table_context_menu(self, pos) -> None:
        """按命中行与多选范围构建分类或 Mod 菜单，分发导航、剪贴板、启用、编辑、导出和删除请求。"""
        item = self._table.itemAt(pos)
        if not item:
            self._show_empty_area_context_menu(self._table.viewport().mapToGlobal(pos))
            return
        row = item.row()
        type_item = self._table.item(row, 0)
        if not type_item:
            return
        item_id = type_item.data(Qt.ItemDataRole.UserRole)
        item_type = type_item.data(Qt.ItemDataRole.UserRole + 1) or "category"

        # 右键未选中行时先选中它（同步行高亮）
        self._select_for_context_menu(item_id)
        multi = len(self._selected_ids) > 1 and item_id in self._selected_ids
        del_label = tr("删除选中的 {count} 项", count=len(self._selected_ids)) if multi else t("删除")

        menu = QMenu(self)
        if item_type == "category":
            self._add_item_clipboard_actions(menu)
            menu.addSeparator()
            enter_act = menu.addAction(icons.get_icon("actions/folder-open"), tr("进入"))
            menu.addSeparator()
            open_dir_act = menu.addAction(icons.get_icon("actions/folder"), tr("打开源目录"))
            online_act = menu.addAction(icons.get_icon("actions/search", "success"), tr("在线查找"))
            menu.addSeparator()
            edit_act = menu.addAction(icons.get_icon("actions/edit"), t("编辑"))
            export_act = menu.addAction(icons.get_icon("actions/export"), tr("导出"))
            delete_act = menu.addAction(icons.get_icon("actions/remove"), del_label)
            action = menu.exec(self._table.viewport().mapToGlobal(pos))
            if action == enter_act:
                self._navigate_to(item_id)
            elif action == open_dir_act:
                self._open_category_folder(item_id)
            elif action == online_act:
                self._online_search_by_cat_id(item_id)
            elif action == edit_act:
                self._on_edit_character(item_id)
            elif action == export_act:
                self._export_from_context(item_id, is_mod=False)
            elif action == delete_act:
                if multi:
                    self._delete_selected()
                else:
                    self.character_delete_requested.emit(item_id)
        else:
            # 获取Mod对象以确定启用/禁用状态
            self._add_item_clipboard_actions(menu)
            menu.addSeparator()
            mod = self._mod_by_id(item_id)
            toggle_label = tr("禁用") if (mod and mod.is_enabled) else tr("启用")
            toggle_act = menu.addAction(
                icons.get_icon("status/disabled" if (mod and mod.is_enabled) else "status/enabled"), toggle_label
            )
            if item_id in self._deploying_frac:
                toggle_act.setText(tr("正在启用…"))
                toggle_act.setEnabled(False)
            edit_act = menu.addAction(icons.get_icon("actions/edit"), t("编辑"))
            open_folder_act = menu.addAction(icons.get_icon("actions/folder-open"), tr("打开源目录"))
            export_act = menu.addAction(icons.get_icon("actions/export"), tr("导出"))
            menu.addSeparator()
            delete_act = menu.addAction(icons.get_icon("actions/remove"), del_label)
            action = menu.exec(self._table.viewport().mapToGlobal(pos))
            if action == toggle_act:
                self.mod_toggle_requested.emit(item_id)
            elif action == edit_act:
                self.mod_edit_requested.emit(item_id)
            elif action == open_folder_act:
                self._open_mod_folder(item_id)
            elif action == export_act:
                self._export_from_context(item_id, is_mod=True)
            elif action == delete_act:
                if multi:
                    self._delete_selected()
                else:
                    self.mod_delete_requested.emit(item_id)

    def _add_item_clipboard_actions(self, menu: QMenu, *, include_cut: bool = True) -> None:
        """各项目菜单共用键盘剪贴板操作。"""
        if include_cut:
            cut = menu.addAction(icons.get_icon("actions/cut"), tr("剪切"))
            cut.setEnabled(bool(self._selected_ids))
            cut.triggered.connect(self._cut_selected)
        paste = menu.addAction(icons.get_icon("actions/paste"), tr("粘贴"))
        paste.setEnabled(
            bool(self._clipboard_cat_ids or self._clipboard_mod_ids)
            and self._clipboard_source_parent != self._current_parent_id
        )
        paste.setToolTip(tr("将剪切的项目移动到当前目录"))
        paste.triggered.connect(self._paste_items)

    def _show_empty_area_context_menu(self, global_pos) -> None:
        """空白处右键菜单：返回上一级、导入Mod、新建类别、切换视图"""
        menu = QMenu(self)
        at_sub = bool(self._current_parent_id)  # 仅在子层级（非根目录）这两项才可用
        # 顶层目录下「返回上一级 / 导入 Mod」不可用：除 setEnabled 外，图标也染成 disabled 灰，
        # 否则只有文字变灰、SVG 图标仍是亮色，视觉上看不出禁用。
        back_act = menu.addAction(
            icons.get_icon("actions/up", "text.primary" if at_sub else "text.disabled"), tr("返回上一级")
        )
        back_act.setEnabled(at_sub)
        menu.addSeparator()
        self._add_item_clipboard_actions(menu, include_cut=False)
        menu.addSeparator()
        import_act = menu.addAction(
            icons.get_icon("actions/import", "text.primary" if at_sub else "text.disabled"), tr("导入 Mod")
        )
        import_act.setEnabled(at_sub)
        add_cat_act = menu.addAction(icons.get_icon("actions/new-folder"), tr("新建类别"))
        menu.addSeparator()
        if self._view_mode == "grid":
            switch_view_act = menu.addAction(icons.get_icon("actions/list-view"), tr("进入列表视图"))
        else:
            switch_view_act = menu.addAction(icons.get_icon("actions/card-view"), tr("进入卡片视图"))
        action = menu.exec(global_pos)
        if action == back_act:
            self._navigate_back()
        elif action == import_act:
            self.mod_import_requested.emit()
        elif action == add_cat_act:
            self._on_add_character()
        elif action == switch_view_act:
            if self._view_mode == "grid":
                self._set_view("list")
            else:
                self._set_view("grid")

    def hideEvent(self, event) -> None:
        """页面隐藏时结束卡片缩放状态并取消目录出现效果。"""
        self._end_card_scale()
        self._cancel_directory_intro()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        """确保后台详情/图片线程先退出，再允许页面析构。"""
        self._end_card_scale()
        self._invalidate_view_position(clear=True)
        self._cancel_directory_intro()
        self._vs_population_timer.stop()
        self._vs_scroll_timer.stop()
        self._vs_pixmap_timer.stop()
        for attr in (
            "_pixmap_worker",
            "_panel_preview_worker",
            "_key_bindings_worker",
        ):
            worker = getattr(self, attr, None)
            if worker is not None and worker.isRunning():
                worker.stop()
                worker.wait(1000)
        super().closeEvent(event)

    def _navigate_back(self) -> None:
        """返回上一级目录"""
        if not self._current_parent_id:
            return
        # 找到当前目录的parent_id
        parent_id = ""
        for c in self._all_characters:
            if c["id"] == self._current_parent_id:
                parent_id = c.get("parent_id") or ""
                break
        self._navigate_to(parent_id)

    def _open_mod_folder(self, mod_id: str) -> None:
        """通过mod_id打开源文件夹"""
        import os
        import subprocess

        mod = self._mod_by_id(mod_id)
        if mod is None:
            return
        path = Path(mod.path)
        if fs.exists(path):
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])

    def _add_previews_to_mod(self, mod_id: str, paths: list[str]) -> int:
        """给某个 Mod 追加预览图（走既有 preview_image_dropped 写入磁盘链，最后一张成为主图）。"""
        added = 0
        for path in paths:
            if path and fs.exists(path):
                self.preview_image_dropped.emit(mod_id, path)
                added += 1
        if added:
            # 新图已被落库提成主图，会话内的「显示第 N 张」记忆必须作废，
            # 否则视图重建后卡片又切回旧图（串图 bug 的另一面）。
            self._preview_override_paths.pop(mod_id, None)
        return added

    def _paste_preview_onto_mod(self, mod_id: str) -> bool:
        """把剪贴板里的图片贴成该 Mod 的预览图（快捷上图 / 菜单项共用）。"""
        path = clipboard_image_path()
        if not path:
            return False
        return self._add_previews_to_mod(mod_id, [path]) > 0

    def _browse_previews_for_mod(self, mod_id: str) -> None:
        """文件对话框选图，追加为该 Mod 的预览图。"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            tr("选择预览图"),
            "",
            tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)"),
        )
        if files:
            self._add_previews_to_mod(mod_id, list(files))

    def _show_mod_card_context_menu(self, card, mod_id: str, pos) -> None:
        """显示 Mod 卡片的操作菜单。

        右键未选中卡片时先选中，已多选时保留选择以支持批量操作。
        Ctrl 或 Shift 加右键直接粘贴预览，普通右键显示菜单。
        上下文菜单信号同步分发，可读取当时的键盘修饰状态。
        """
        self._select_for_context_menu(mod_id)
        quick = QApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        )
        if quick:
            self._paste_preview_onto_mod(mod_id)
            return
        mod = self._mod_by_id(mod_id)
        menu = QMenu(self)
        toggle_label = tr("禁用") if (mod and mod.is_enabled) else tr("启用")
        toggle_act = menu.addAction(
            icons.get_icon("status/disabled" if (mod and mod.is_enabled) else "status/enabled"), toggle_label
        )
        if mod_id in self._deploying_frac:
            toggle_act.setText(tr("正在启用…"))
            toggle_act.setEnabled(False)
        edit_act = menu.addAction(icons.get_icon("actions/edit"), t("编辑"))
        open_folder_act = menu.addAction(icons.get_icon("actions/folder-open"), tr("打开源目录"))
        export_act = menu.addAction(icons.get_icon("actions/export"), tr("导出"))
        menu.addSeparator()
        self._add_item_clipboard_actions(menu)
        menu.addSeparator()
        paste_act = menu.addAction(icons.get_icon("actions/image"), tr("粘贴预览图"))
        paste_act.setEnabled(clipboard_has_image())
        browse_act = menu.addAction(icons.get_icon("actions/add"), tr("添加预览图"))
        menu.addSeparator()
        multi = len(self._selected_ids) > 1 and mod_id in self._selected_ids
        delete_act = menu.addAction(
            icons.get_icon("actions/remove"),
            tr("删除选中的 {count} 项", count=len(self._selected_ids)) if multi else t("删除"),
        )

        # menus：把插件 mod-context 贡献点追加到菜单末尾
        from src.ui import plugin_menu

        plugin_actions = plugin_menu.menu_actions("mod-context", mod_id)
        plugin_act_map = {}
        if plugin_actions:
            menu.addSeparator()
            for _title, _cb in plugin_actions:
                _a = menu.addAction(_title)
                plugin_act_map[_a] = _cb

        action = menu.exec(card.mapToGlobal(pos))
        if action in plugin_act_map:
            plugin_act_map[action]()
            return
        if action == toggle_act:
            self.mod_toggle_requested.emit(mod_id)
        elif action == edit_act:
            self.mod_edit_requested.emit(mod_id)
        elif action == paste_act:
            self._paste_preview_onto_mod(mod_id)
        elif action == browse_act:
            self._browse_previews_for_mod(mod_id)
        elif action == open_folder_act:
            self._open_mod_folder(mod_id)
        elif action == export_act:
            self._export_from_context(mod_id, is_mod=True)
        elif action == delete_act:
            if multi:
                self._delete_selected()
            else:
                self.mod_delete_requested.emit(mod_id)
