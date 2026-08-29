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
        """生成并缓存文本排序键，优先拼音并以原文区分同音名称，转换器变化时重建缓存。"""
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
                py = "".join(self._lazy_pinyin(value, errors="ignore"))
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
            lo = 0 if self._sort_key == "status_desc" else category_count
            hi = len(desired) - category_count if self._sort_key == "status_desc" else len(desired)
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
