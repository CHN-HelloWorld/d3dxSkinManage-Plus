"""分类预设的只读树与共用展示组件。"""

from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from src.core.category_preset import CategoryPresetError
from src.core.category_preset_service import category_preset_filename
from src.core.library_schema import directory_key
from src.ui import icons, theme
from src.utils import fs
from src.utils.i18n import N_
from src.utils.i18n import t as tr

FILE_FILTER = N_("分类预设文件 (*.d3dxcp)")
STATUS_TEXT = {"new": N_("新增"), "existing": N_("已存在"), "conflict": N_("冲突")}


def preset_filename(name: str) -> str:
    """返回经服务校验的预设文件名；名称不合法时返回空字符串供界面禁用导出。"""
    try:
        return category_preset_filename(name)
    except CategoryPresetError:
        return ""


def preset_name_error(name: str) -> str:
    """返回预设名称校验错误的说明，合法名称返回空字符串。"""
    try:
        category_preset_filename(name)
    except CategoryPresetError as error:
        return str(error)
    return ""


def label(text: str = "", *, role: str = "", wrap: bool = False) -> QLabel:
    """创建可指定样式角色和换行策略的纯文本标签，由布局接管所有权。"""
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty("presetRole", role)
    return widget


def button(text, callback, *, icon="", primary=False, quiet=False, name="") -> QPushButton:
    """创建并连接预设操作按钮，按主操作或轻量操作选择样式和图标颜色。"""
    widget = QPushButton(text)
    widget.setAutoDefault(False)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setMinimumHeight(34)
    if name:
        widget.setObjectName(name)
    if primary:
        widget.setProperty("presetPrimary", True)
    if quiet:
        widget.setProperty("presetQuiet", True)
    if icon:
        icons.apply_icon(widget, icon, "text.on_accent" if primary else "accent" if quiet else "text.primary")
        widget.setIconSize(QSize(16, 16))
    widget.clicked.connect(callback)
    return widget


def panel() -> QFrame:
    """创建带预设面板样式角色的容器，供调用方填入内容并加入布局。"""
    frame = QFrame()
    frame.setProperty("presetRole", "panel")
    return frame


def stat_strip(entries: list[tuple[str, str, str]]) -> QFrame:
    """将数值、标题和可选主题色三元组排成等宽统计栏，并提供可访问名称。"""
    frame = panel()
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(18, 12, 18, 12)
    layout.setSpacing(20)
    for value, title, token in entries:
        column = QVBoxLayout()
        column.setSpacing(3)
        number = label(str(value), role="stat")
        if token:
            number.setStyleSheet(f"color: {theme.color(token)};")
        number.setAccessibleName(f"{title}：{value}")
        column.addWidget(number)
        column.addWidget(label(title, role="muted"))
        layout.addLayout(column, 1)
    return frame


class PresetTree(QFrame):
    """搜索仅影响可见性，不修改完整记录快照。"""

    node_selected = Signal(object)

    def __init__(self, parent=None, *, export_mode=False):
        """构建只读单选分类树及搜索工具栏；导出模式隐藏导入处理结果列。"""
        super().__init__(parent)
        self.setProperty("presetRole", "panel")
        self.export_mode = export_mode
        self.rows = ()
        self.items: dict[str, QTreeWidgetItem] = {}
        self._collapsed_before_search: set[str] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 10, 12, 10)
        toolbar.setSpacing(6)
        toolbar.addWidget(label(tr("完整分类树") if export_mode else tr("预设分类树"), role="strong"))
        self.count = label("0", role="muted")
        toolbar.addWidget(self.count)
        toolbar.addWidget(button(tr("展开"), self.tree_expand, quiet=True))
        toolbar.addWidget(button(tr("折叠"), self.tree_collapse, quiet=True))
        self.search = QLineEdit()
        self.search.setObjectName("categoryPresetSearch")
        self.search.setPlaceholderText(tr("搜索分类"))
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(tr("搜索分类，仅影响显示"))
        self.search.setMinimumWidth(110)
        toolbar.addWidget(self.search, 1)
        layout.addLayout(toolbar)
        self.tree = QTreeWidget()
        self.tree.setObjectName("categoryPresetTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(
            [tr("分类目录"), tr("类型") if export_mode else tr("预设类型"), tr("处理结果"), tr("目录图片")]
        )
        self.tree.setColumnHidden(2, export_mode)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAlternatingRowColors(False)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 94), (2, 75), (3, 125)):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.tree.setColumnWidth(column, width)
        layout.addWidget(self.tree, 1)
        self.empty = label(tr("没有匹配的分类。"), role="muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(12, 16, 12, 16)
        self.empty.hide()
        layout.addWidget(self.empty)
        self.search.textChanged.connect(self._filter)
        self.tree.currentItemChanged.connect(self._selected)

    def set_rows(self, rows, *, image_issues=None):
        """替换完整记录快照并按规范化目录键连接父子节点，恢复可用选中项后应用搜索条件。"""
        previous = self.tree.currentItem()
        selected = previous.data(0, Qt.ItemDataRole.UserRole).path if previous else ""
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            self.rows = tuple(rows)
            self.items = {}
            canonical_items = {}
            self.tree.clear()
            self._collapsed_before_search = None
            image_issues = image_issues or {}
            for row in self.rows:
                name = row.local_name or row.name if not self.export_mode else row.name
                image_text = self._image_text(row)
                if row.path in image_issues:
                    image_text = tr("图片无法读取")
                item = QTreeWidgetItem(
                    [
                        name,
                        tr("角色 / 对象") if row.node_type == "character" else tr("类别"),
                        tr(STATUS_TEXT.get(row.status, "")),
                        image_text,
                    ]
                )
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setData(0, Qt.ItemDataRole.UserRole, row)
                item.setSizeHint(0, QSize(0, 37))
                item.setIcon(
                    0, icons.get_icon("meta/character" if row.node_type == "character" else "actions/folder", "accent")
                )
                for column in range(4):
                    item.setToolTip(column, "\n".join(part for part in (row.path, row.detail) if part))
                token = {"new": "success", "conflict": "danger"}.get(row.status, "text.secondary")
                item.setForeground(2, QColor(theme.color(token)))
                if row.fill_image:
                    item.setForeground(3, QColor(theme.color("accent")))
                self.items[row.path] = item
                canonical_items[directory_key(row.path)] = item
            for row in self.rows:
                item = self.items[row.path]
                parent_path = row.path.rpartition("/")[0]
                parent = canonical_items.get(directory_key(parent_path))
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
            self.tree.expandAll()
            self.count.setText(str(len(self.rows)))
        finally:
            self.tree.blockSignals(False)
            self.tree.setUpdatesEnabled(True)
        self._filter(self.search.text())
        if self.rows:
            self.tree.setCurrentItem(self.items.get(selected) or self.items[self.rows[0].path])

    def _image_text(self, row):
        """根据导入或导出模式及本地图片状态返回该行的图片处理说明。"""
        if self.export_mode:
            return tr("包含") if row.has_image else "—"
        if row.fill_image:
            return tr("补充目录图片")
        if row.local_image_path:
            return tr("保留本地")
        if row.has_image:
            return tr("随目录导入") if row.status == "new" else tr("保留本地")
        return "—"

    def _filter(self, text):
        """搜索路径及中英文名称，同时保留匹配节点的祖先；清空搜索时恢复此前折叠状态。"""
        query = text.strip().casefold()
        if query and self._collapsed_before_search is None:
            self._collapsed_before_search = {path for path, item in self.items.items() if not item.isExpanded()}
        keep = set()
        for row in self.rows:
            haystack = " ".join((row.path, row.name, row.name_en, row.local_name)).casefold()
            if not query or query in haystack:
                path = row.path
                while path:
                    keep.add(directory_key(path))
                    path = path.rpartition("/")[0]
        for path, item in self.items.items():
            key = directory_key(path)
            item.setHidden(key not in keep)
            if query and key in keep:
                item.setExpanded(True)
            elif not query and self._collapsed_before_search is not None:
                item.setExpanded(path not in self._collapsed_before_search)
        if not query:
            self._collapsed_before_search = None
        self.empty.setVisible(not keep)

    def tree_expand(self):
        """清除搜索条件并展开完整分类树。"""
        self.search.clear()
        self.tree.expandAll()

    def tree_collapse(self):
        """清除搜索条件并折叠完整分类树。"""
        self.search.clear()
        self.tree.collapseAll()
