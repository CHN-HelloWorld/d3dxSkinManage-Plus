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

    def next_issue(self):
        """按快照顺序循环定位冲突节点，没有冲突时不改变选择。"""
        issues = [row.path for row in self.rows if row.status == "conflict"]
        if not issues:
            return
        current = self.tree.currentItem()
        path = current.data(0, Qt.ItemDataRole.UserRole).path if current else ""
        index = (issues.index(path) + 1) % len(issues) if path in issues else 0
        self.reveal(issues[index])

    def reveal(self, path):
        """清除搜索并展开目标祖先、选中和滚动到指定路径；路径不存在时保留当前选择。"""
        self.search.clear()
        item = self.items.get(path)
        if item is None:
            return
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.tree.setFocus()

    def _selected(self, current, previous):
        """将选中项保存的完整记录通过信号交给详情面板，空选择不发送信号。"""
        if current is not None:
            self.node_selected.emit(current.data(0, Qt.ItemDataRole.UserRole))


class _ImageSignals(QObject):
    """承载后台解码结果及请求序号，将 QImage 交回界面线程。"""

    ready = Signal(int, QImage)


_preview_pool = None


def _image_pool():
    """惰性创建模块持有的单线程图片解码池，限制并发解码内存且隔离其他图像任务。"""
    global _preview_pool
    if _preview_pool is None:
        # 独立串行队列限制原生解码内存，不占用其他缩略图和背景线程。
        _preview_pool = QThreadPool()
        _preview_pool.setMaxThreadCount(1)
    return _preview_pool


class _ImageJobs:
    """由普通 Python 对象管理生命周期，销毁回调不访问 Qt 对象。"""

    def __init__(self):
        """创建图片任务引用集合，供控件销毁后仍可安全发送取消请求。"""
        self.jobs = []

    def cancel(self):
        """对所有持有的图片任务设置取消事件，不阻塞等待解码结束。"""
        for job in self.jobs:
            job.cancelled.set()


class _ImageTask(QRunnable):
    """在专用线程池中解码有尺寸上限的目录图片，以事件标记取消和读取结束。"""

    def __init__(self, sequence, path):
        """保存请求序号和源路径，创建独立信号对象及取消、完成事件。"""
        super().__init__()
        self.signals = _ImageSignals()
        self.sequence, self.path = sequence, path
        self.cancelled, self.done = Event(), Event()

    def run(self):
        """后台读取不超过四千万像素的图片并缩放到预览尺寸。

        取消后不发送结果；正常读取失败时发送空图像。无论是否取消或异常，均释放读取器并设置完成事件。
        """
        try:
            if self.cancelled.is_set():
                return
            image = QImage()
            reader = QImageReader(fs.native_path(self.path))
            try:
                reader.setDecideFormatFromContent(True)
                reader.setAutoTransform(True)
                size = reader.size()
                if not self.cancelled.is_set() and size.isValid() and size.width() * size.height() <= 40_000_000:
                    reader.setScaledSize(size.scaled(QSize(440, 280), Qt.AspectRatioMode.KeepAspectRatio))
                    image = reader.read()
                    if not image.isNull():
                        image = image.scaled(
                            440, 280, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                        )
            finally:
                del reader
            if not self.cancelled.is_set():
                self.signals.ready.emit(self.sequence, image)
        finally:
            # 移除解码源前在后台等待读取结束。
            self.done.set()


class PresetInspector(QFrame):
    """展示选中分类的来源、保留策略与图片，并隔离过期的异步图片结果。"""

    def __init__(self, parent=None):
        """创建详情控件及图片任务生命周期容器，控件销毁时仅发送非阻塞取消请求。"""
        super().__init__(parent)
        self.setProperty("presetRole", "panel")
        self.setMinimumWidth(220)
        self._sequence = 0
        self._image_lifetime = _ImageJobs()
        self._image_jobs = self._image_lifetime.jobs
        self.destroyed.connect(self._image_lifetime.cancel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label(tr("目录详情"), role="muted"))
        top.addStretch()
        self.status = label(role="strong")
        top.addWidget(self.status)
        layout.addLayout(top)
        self.image = label(tr("暂无目录图片"), role="image")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(140)
        self.image.setMaximumHeight(190)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.image)
        self.name = label(role="subtitle", wrap=True)
        self.english = label(role="muted", wrap=True)
        self.path = label(role="muted", wrap=True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.kind = label(wrap=True)
        self.image_action = label(wrap=True)
        self.detail = label(role="notice", wrap=True)
        for widget in (self.name, self.english, self.path, self.kind, self.image_action, self.detail):
            layout.addWidget(widget)
        layout.addStretch()

    def set_row(self, row):
        """更新分类详情并取消旧图片请求；以递增序号提交新预览，优先使用预设图片路径。"""
        self.stop_loading()
        self._image_jobs[:] = [job for job in self._image_jobs if not job.done.is_set()]
        self._sequence += 1
        self.status.setText(tr(STATUS_TEXT.get(row.status, "")))
        token = {"new": "success", "conflict": "danger"}.get(row.status, "text.secondary")
        self.status.setStyleSheet(f"color: {theme.color(token)};")
        self.name.setText(row.local_name or row.name)
        self.english.setText(row.name_en)
        self.english.setVisible(bool(row.name_en))
        self.path.setText(row.path.replace("/", " / "))
        self.kind.setText(tr("预设类型\u3000") + (tr("角色 / 对象") if row.node_type == "character" else tr("类别")))
        behavior = tr("不设置图片")
        if row.local_image_path:
            behavior = tr("保留本地图片")
        elif row.fill_image:
            behavior = tr("补充目录图片")
        elif row.has_image:
            behavior = tr("随新增目录导入") if row.status == "new" else tr("保留已有图片引用")
        self.image_action.setText(tr("目录图片\u3000") + behavior)
        details = [row.detail] if row.detail else []
        if row.local_name and (row.local_name != row.name or row.local_node_type != row.node_type):
            local_type = tr("角色 / 对象") if row.local_node_type == "character" else tr("类别")
            details.append(
                tr(
                    "预设名称：{name}\n本地名称：{local_name}\n本地类型：{local_type}\n将保留本地设置。",
                    name=row.name,
                    local_name=row.local_name,
                    local_type=local_type,
                )
            )
        self.detail.setText("\n".join(details))
        self.detail.setVisible(bool(details))
        self.image.setPixmap(QPixmap())
        image_path = row.image_path or row.local_image_path
        self.image.setText(tr("正在加载图片…") if image_path else tr("暂无目录图片"))
        if image_path:
            task = _ImageTask(self._sequence, image_path)
            self._image_jobs.append(task)
            task.signals.ready.connect(self._image_ready)
            _image_pool().start(task)

    def stop_loading(self):
        """取消持有的解码任务，返回未完成任务的事件，供后台清理等待源文件释放。"""
        self._image_lifetime.cancel()
        return tuple(job.done for job in self._image_jobs if not job.done.is_set())

    def _image_ready(self, sequence, image):
        """在界面线程接受当前序号的图像并创建 QPixmap，空图像显示失败提示。"""
        if sequence != self._sequence:
            return
        if image.isNull():
            self.image.setText(tr("图片无法显示"))
        else:
            self.image.setPixmap(
                QPixmap.fromImage(image).scaled(
                    self.image.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
            )
