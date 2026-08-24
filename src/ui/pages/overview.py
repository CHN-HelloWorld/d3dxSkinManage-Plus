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
