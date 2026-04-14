"""收集批量 Mod 导入来源、年龄分级和预览选择，并把确认后的参数交给宿主执行导入。"""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.services.archive import ArchiveService
from src.ui import acrylic, anim, icons, theme
from src.ui.widgets.image_utils import load_scaled_qimage_file
from src.ui.widgets.preview import ImageViewerDialog
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils.constants import ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS
from src.utils.i18n import N_, t
from src.utils.i18n import t as tr
from src.utils.paths import get_data_dir
from src.utils.qt_timers import call_later


class _PreviewSignals(QObject):
    """将请求序号、源路径和后台解码的 QImage 投递给界面线程。"""

    ready = Signal(int, str, QImage)


class _PreviewTask(QRunnable):
    """在工作线程解码和缩放，已取消的排队任务不访问文件。"""

    def __init__(self, request_id: int, path: str, size: QSize, cancelled: Event):
        """保存预览请求身份、目标像素尺寸及共享取消事件，创建独立的结果信号对象。"""
        super().__init__()
        self.signals = _PreviewSignals()
        self.request_id, self.path, self.size = request_id, path, size
        self.cancelled = cancelled

    def run(self) -> None:
        """后台解码并等比缩放预览，读取异常转为空图像；取消任务在读取前或发送结果前退出。"""
        if self.cancelled.is_set():
            return
        try:
            image = load_scaled_qimage_file(self.path, self.size, auto_transform=True)
            if not image.isNull():
                image = image.scaled(
                    self.size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
        except Exception:
            image = QImage()
        if not self.cancelled.is_set():
            self.signals.ready.emit(self.request_id, self.path, image)


def _detect_keys(folder: Path) -> list[str]:
    """复用 INI 解析服务提取目录中的按键名称，检测失败时返回空列表。"""
    from src.core.ini_keys import extract_key_names, parse_mod_keys

    try:
        return extract_key_names(parse_mod_keys(str(folder)))
    except Exception:
        return []


def _detect_previews(folder: Path) -> list[str]:
    """单次遍历目录收集最多十张支持的非 DDS 图片，遍历失败时返回已收集结果。"""
    ext_set = {e.lower() for e in IMAGE_EXTENSIONS} - {".dds"}
    images: list[str] = []
    try:
        for root, _dirs, files in os.walk(str(folder)):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in ext_set:
                    images.append(os.path.join(root, f))
                    if len(images) >= 10:
                        return images
    except Exception:
        pass
    return images


def _detect_keys_from_archive(archive_path: Path) -> list[str]:
    """读取归档内前二十个 INI 文件并汇总按键名称，读取失败时保留已解析结果。"""
    from src.core.ini_keys import extract_key_names, parse_ini_text

    bindings = []
    try:
        svc = ArchiveService()
        contents = svc.list_contents(archive_path)
        ini_files = [n for n in contents if n.lower().endswith(".ini")][:20]
        for ini_name in ini_files:
            data = svc.read_file(archive_path, ini_name)
            if data is None:
                continue
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                continue
            bindings.extend(parse_ini_text(text, ini_name))
    except Exception:
        pass
    return extract_key_names(bindings)


def _detect_previews_from_archive(archive_path: Path) -> list[str]:
    """将归档内前十张支持的非 DDS 图片提取到扫描缓存，返回存在的路径，异常时返回空列表。"""
    ext_set = {e.lower() for e in IMAGE_EXTENSIONS} - {".dds"}
    try:
        svc = ArchiveService()
        contents = svc.list_contents(archive_path)
        image_files = [n for n in contents if os.path.splitext(n)[1].lower() in ext_set][:10]
        if not image_files:
            return []
        temp_dir = get_data_dir() / "temp" / "preview_scan" / archive_path.stem
        temp_dir.mkdir(parents=True, exist_ok=True)
        extracted = svc.extract_files(archive_path, image_files, temp_dir)
        return [str(p) for p in extracted if p.exists()]
    except Exception:
        pass
    return []


class _ImportEntry:
    """保存单个输入来源的扫描缓存、年龄分级及预览选择，编辑选择不修改源文件。"""

    def __init__(self, path: str):
        """从路径派生初始名称及归档类型，初始化尚未扫描且默认全年龄的导入项。"""
        self.source_path = path
        self.name = Path(path).stem
        self.keys: list[str] = []
        self.previews: list[str] = []
        self.excluded_previews: list[str] = []
        self.is_archive = Path(path).suffix.lower() in ARCHIVE_EXTENSIONS
        self.selected_preview_index: int = 0
        self.selected_preview_path: str = ""
        self.age_rating: str = "G"  # 默认全年龄
        self._scanned = False

    def preview_name(self, preview_path: str) -> str:
        """返回相对于来源目录或归档扫描缓存的预览身份，路径不在对应根目录内时抛出 ValueError。"""
        source = Path(self.source_path)
        root = source if source.is_dir() else get_data_dir() / "temp" / "preview_scan" / source.stem
        return Path(preview_path).relative_to(root).as_posix()

    def ensure_scanned(self) -> None:
        """首次调用时同步检测按键和预览并选中首图，后续调用复用条目缓存，不在构造时执行文件读取。"""
        if self._scanned:
            return
        self._scanned = True
        p = Path(self.source_path)
        if p.is_dir():
            self.keys = _detect_keys(p)
            self.previews = _detect_previews(p)
            if self.previews:
                self.selected_preview_path = self.previews[0]
        elif self.is_archive:
            self.keys = _detect_keys_from_archive(p)
            self.previews = _detect_previews_from_archive(p)
            if self.previews:
                self.selected_preview_path = self.previews[0]


class _ScanWorker(QThread):
    """后台顺序扫描输入的按键和预览，结果经信号交回界面线程更新条目。"""

    entry_scanned = Signal(int, list, list)  # row, keys, previews

    def __init__(self, entries: list[tuple[int, _ImportEntry]]):
        """保存行索引与条目快照列表，创建由导入窗口保存引用的无父对象扫描线程。"""
        super().__init__()
        self._entries = entries
        self._cancelled = False

    def cancel(self) -> None:
        """请求扫描在条目间或按键、预览阶段边界停止，不中断当前归档调用。"""
        self._cancelled = True

    def run(self) -> None:
        """顺序检测未扫描输入的按键和预览，通过原行索引回传结果；取消后停止后续扫描及结果发送。"""
        for idx, entry in self._entries:
            if self._cancelled:
                break
            if entry._scanned:
                continue
            p = Path(entry.source_path)
            if p.is_dir():
                keys = _detect_keys(p)
                if self._cancelled:
                    break
                previews = _detect_previews(p)
                if not self._cancelled:
                    self.entry_scanned.emit(idx, keys, previews)
            elif entry.is_archive:
                keys = _detect_keys_from_archive(p)
                if self._cancelled:
                    break
                previews = _detect_previews_from_archive(p)
                if not self._cancelled:
                    self.entry_scanned.emit(idx, keys, previews)


_AGE_RATING_OPTIONS: list[tuple[str, str]] = [
    ("G", N_("G - 全年龄")),
    ("P", N_("P - 轻微敏感")),
    ("R", N_("R - 限制级")),
    ("X", N_("X · 18+ 内容")),
]


def _normalize_age_rating(value: str) -> str:
    """提取首个非空白字符并转为合法分级代码，空值或未知代码统一使用 G。"""
    if not value:
        return "G"
    code = value.strip().upper()[:1]
    return code if code in {"G", "P", "R", "X"} else "G"


class _DropdownBelowStyle(QProxyStyle):
    """将 QComboBox 面板固定为向下展开的列表。

    应用级 QSS 可使 QStyleSheetStyle 将 SH_ComboBox_Popup 设为 1，使用菜单式弹出，
    导致当前项与收起状态的文本重叠。将该样式提示设为 0 可使用下拉列表布局。
    其余绘制由外层 QStyleSheetStyle 接管，保留 QSS 的 SVG 下拉箭头。
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        """强制组合框使用列表式下拉而非菜单式弹出，其他样式提示沿用基类。"""
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


class _NoWheelComboBox(QComboBox):
    """年龄分级下拉框在收起时忽略滚轮，避免滚动表格时意外修改分级。

    wheelEvent 将事件交给父级滚动区处理；展开后的列表由弹出视图处理滚轮事件。
    """

    def wheelEvent(self, e):
        """忽略收起状态的滚轮输入，使父级滚动区域继续处理而不改变年龄分级。"""
        e.ignore()


class _PathElideDelegate(QStyledItemDelegate):
    """来源路径列使用尾部省略，优先保留盘符与上级目录，便于区分输入来源。"""

    def initStyleOption(self, option, index):
        """在默认单元格样式基础上改用尾部省略，优先保留来源路径的前部信息。"""
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideRight


class ModImportDialog(QDialog):
    """维护导入候选列表及预览选择，确认后以信号交出参数；目标节点由构造参数固定。"""

    import_confirmed = Signal(list, str)  # [(path, name, selected_preview_name, age_rating), ...], node_id

    def __init__(
        self,
        node_id: str = "",
        initial_paths: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """保存目标节点和候选来源，初始化扫描及预览取消状态，加入初始路径后选中最后一项。"""
        super().__init__(parent)
        # 原生表面准备完成后再处理可能影响整张表单的属性。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        anim.install_intro_fade(self)
        self._character_id = node_id
        self._entries: list[_ImportEntry] = []
        self._current_preview_index = 0
        self._current_preview_path = ""
        self._preview_request_id = 0
        self._preview_cancelled = Event()
        self._scan_worker: _ScanWorker | None = None
        self._import_col_ratios = (0.16, 0.56, 0.18, 0.10)
        self._import_col_min_widths = (110, 280, 150, 86)
        self._import_syncing_widths = False
        self._import_header_dragging = False
        self._import_user_resized = False
        self.setAcceptDrops(True)
        self._setup_ui()
        acrylic.apply_dialog_acrylic(self)
        if initial_paths:
            self._add_entries_batch(initial_paths)
            # 自动选中最后添加的Mod并显示预览
            if self._entries:
                self._table.setCurrentCell(len(self._entries) - 1, 0)

    def _setup_ui(self) -> None:
        """构建来源表格、分级控件、预览管理区和确认操作，并安装列宽拖动及空白选择事件过滤。"""
        self.setWindowTitle(t("导入 Mod"))
        self.setMinimumSize(680, 540)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 添加文件按钮
        btn_row = QHBoxLayout()
        # 先挂接嵌套布局再添加内容，避免 PySide 更换间距项所有者后重复释放。
        layout.addLayout(btn_row)
        add_file_btn = QPushButton(t("添加文件"))
        add_file_btn.clicked.connect(self._browse_files)
        btn_row.addWidget(add_file_btn)

        add_folder_btn = QPushButton(t("添加文件夹"))
        add_folder_btn.clicked.connect(self._browse_folder)
        btn_row.addWidget(add_folder_btn)

        btn_row.addStretch()

        remove_btn = QPushButton(t("移除选中"))
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(remove_btn)

        # 文件列表
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([tr("名称"), tr("来源路径"), tr("年龄分级"), tr("预览图")])
        # 所有列均可调整宽度
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(60)  # 禁止把列拖到 0 宽度导致内容不可见
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setColumnWidth(0, 160)
        self._table.setColumnWidth(1, 340)
        self._table.setColumnWidth(2, 170)
        self._table.setColumnWidth(3, 100)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        # 表格行高须容纳下拉框边框与内边距，同时清除单元格垂直内边距，
        # 避免控件超出当前行；水平边距与文字列保持一致。
        self._table.verticalHeader().setDefaultSectionSize(36)
        self._table.setStyleSheet("QTableWidget::item { padding: 0px 6px; }")
        self._table.setAlternatingRowColors(True)
        self._table.setItemDelegateForColumn(1, _PathElideDelegate(self._table))
        header.sectionResized.connect(self._on_import_header_section_resized)
        header.viewport().installEventFilter(self)

        content_splitter = QSplitter(Qt.Orientation.Vertical)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(self._table)

        # 预览区
        self._preview_label = QLabel()
        self._preview_label.setMinimumHeight(120)
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setStyleSheet(
            f"border: 1px dashed {theme.color('stroke.control')}; border-radius: {theme.radius('control')}px;"
        )
        self._preview_label.setText(t("无预览图"))
        # 仅实际图片范围显示可点击光标，预览区留白保持普通箭头。
        self._preview_label.setMouseTracking(True)
        self._preview_label.mousePressEvent = self._on_preview_label_clicked
        self._preview_label.mouseMoveEvent = self._on_preview_label_moved

        preview_area = QWidget()
        preview_area_layout = QVBoxLayout(preview_area)
        preview_area_layout.setContentsMargins(0, 0, 0, 0)
        preview_area_layout.setSpacing(8)
        preview_area_layout.addWidget(self._preview_label)

        # 预览图导航 - 居中
        preview_nav = QHBoxLayout()
        preview_area_layout.addLayout(preview_nav)
        preview_nav.setSpacing(8)

        preview_nav.addStretch()

        self._prev_preview_btn = QPushButton()
        icons.apply_icon(self._prev_preview_btn, "actions/chevron-left")
        self._prev_preview_btn.setIconSize(QSize(16, 16))
        self._prev_preview_btn.setFixedSize(32, 32)
        self._prev_preview_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._prev_preview_btn.setToolTip(tr("上一张预览图"))
        self._prev_preview_btn.setAccessibleName(tr("上一张预览图"))
        self._prev_preview_btn.clicked.connect(self._show_prev_preview)
        preview_nav.addWidget(self._prev_preview_btn)

        self._preview_page_label = QLabel("0 / 0")
        self._preview_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_nav.addWidget(self._preview_page_label)

        self._next_preview_btn = QPushButton()
        icons.apply_icon(self._next_preview_btn, "actions/chevron-right")
        self._next_preview_btn.setIconSize(QSize(16, 16))
        self._next_preview_btn.setFixedSize(32, 32)
        self._next_preview_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next_preview_btn.setToolTip(tr("下一张预览图"))
        self._next_preview_btn.setAccessibleName(tr("下一张预览图"))
        self._next_preview_btn.clicked.connect(self._show_next_preview)
        preview_nav.addWidget(self._next_preview_btn)

        preview_nav.addStretch()

        # 预览图管理按钮区 - 居中显示
        preview_mgmt_row = TagFlowLayout(spacing=6)
        preview_area_layout.addLayout(preview_mgmt_row)
        preview_mgmt_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        preview_mgmt_row.setContentsMargins(0, 8, 0, 8)

        self._delete_preview_btn = QPushButton(tr("移除"))
        icons.apply_icon(self._delete_preview_btn, "actions/remove")
        self._delete_preview_btn.setIconSize(QSize(16, 16))
        self._delete_preview_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._delete_preview_btn.setToolTip(tr("从本次导入中移除此预览图，保留原始文件"))
        self._delete_preview_btn.clicked.connect(self._delete_current_preview)
        preview_mgmt_row.addWidget(self._delete_preview_btn)

        self._delete_all_previews_btn = QPushButton(tr("全部移除"))
        icons.apply_icon(self._delete_all_previews_btn, "actions/remove")
        self._delete_all_previews_btn.setIconSize(QSize(16, 16))
        self._delete_all_previews_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._delete_all_previews_btn.setToolTip(tr("从本次导入中移除全部预览图，保留原始文件"))
        self._delete_all_previews_btn.clicked.connect(self._delete_all_previews)
        preview_mgmt_row.addWidget(self._delete_all_previews_btn)

        content_splitter.addWidget(preview_area)
        content_splitter.setSizes([360, 200])
        layout.addWidget(content_splitter, 1)

        self._update_preview_nav()

        self._table.currentCellChanged.connect(self._on_selection_changed)
        # 安装事件过滤器 - 点击空白处取消选中
        self._table.viewport().installEventFilter(self)
        self._apply_import_column_widths(force_ratio=not self._import_user_resized)

        # 底部按钮
        bottom = QHBoxLayout()
        layout.addLayout(bottom)
        bottom.addStretch()

        cancel_btn = QPushButton(t("取消"))
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(cancel_btn)

        import_btn = QPushButton(t("确认导入"))
        import_btn.setProperty("accent", True)
        import_btn.clicked.connect(self._on_import)
        bottom.addWidget(import_btn)

    def _add_entry(self, path: str, auto_select: bool = False) -> None:
        """按来源路径字符串去重后添加一项并重建表格，可自动选择新加入项。"""
        # 避免重复
        for e in self._entries:
            if e.source_path == path:
                return
        entry = _ImportEntry(path)
        self._entries.append(entry)
        self._refresh_table()
        if auto_select:
            new_row = len(self._entries) - 1
            self._table.setCurrentCell(new_row, 0)
        self._update_preview_nav()

    def _add_entries_batch(self, paths: list[str], auto_select_last: bool = False) -> None:
        """批量添加条目，仅在全部添加完后刷新一次表格（避免 O(n²) 重建）"""
        existing = {e.source_path for e in self._entries}
        added = False
        for p in paths:
            if p not in existing:
                entry = _ImportEntry(p)
                self._entries.append(entry)
                existing.add(p)
                added = True
        if added:
            self._refresh_table()
            if auto_select_last and self._entries:
                self._table.setCurrentCell(len(self._entries) - 1, 0)
            self._update_preview_nav()

    def _refresh_table(self) -> None:
        """取消旧扫描并最多等待二百毫秒，然后屏蔽重绘和信号重建各行，为未扫描项目启动新线程。"""
        # 取消进行中的后台扫描
        if self._scan_worker is not None:
            self._scan_worker.cancel()
            self._scan_worker.wait(200)
            self._scan_worker = None

        # 禁用 UI 更新 + 信号，避免每行设置时触发重绘
        self._table.setUpdatesEnabled(False)
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(len(self._entries))
            unscanned: list[tuple[int, _ImportEntry]] = []
            for i, entry in enumerate(self._entries):
                name_item = QTableWidgetItem(entry.name)
                self._table.setItem(i, 0, name_item)

                src_item = QTableWidgetItem(entry.source_path)
                src_item.setToolTip(entry.source_path)
                src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, 1, src_item)

                if not entry._scanned:
                    unscanned.append((i, entry))

                # 年龄分级列嵌入下拉框，避免单元格文本与委托编辑器重叠或样式不一致。
                rating_code = _normalize_age_rating(entry.age_rating)
                self._table.setCellWidget(i, 2, self._make_age_combo(i, rating_code))

                # 预览图列
                if entry._scanned:
                    preview_str = str(len(entry.previews)) + " " + t("张图片") if entry.previews else "-"
                else:
                    preview_str = tr("扫描中…")
                prev_item = QTableWidgetItem(preview_str)
                prev_item.setFlags(prev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, 3, prev_item)
        finally:
            self._table.blockSignals(False)
            self._table.setUpdatesEnabled(True)

        # 启动后台线程扫描未扫描的条目
        if unscanned:
            self._scan_worker = _ScanWorker(unscanned)
            self._scan_worker.entry_scanned.connect(self._on_entry_scanned)
            self._scan_worker.start()

        self._apply_import_column_widths(force_ratio=not self._import_user_resized)

    def _on_entry_scanned(self, row: int, keys: list, previews: list) -> None:
        """按原行索引写回扫描结果并过滤已排除图片，选中行完成时刷新预览和导航。"""
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        entry._scanned = True
        entry.keys = keys
        previews = [path for path in previews if entry.preview_name(path) not in entry.excluded_previews]
        entry.previews = previews
        if previews and not entry.selected_preview_path:
            entry.selected_preview_path = previews[0]

        # 更新预览图列
        preview_str = str(len(previews)) + " " + t("张图片") if previews else "-"
        prev_item = QTableWidgetItem(preview_str)
        prev_item.setFlags(prev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 3, prev_item)

        # 若当前选中的就是这一行，刷新预览区
        if self._table.currentRow() == row:
            self._show_current_preview(row)
            self._update_preview_nav()
        self._apply_import_column_widths(force_ratio=False)

    def _make_age_combo(self, row: int, code: str) -> QComboBox:
        """创建不会随滚轮误改的年龄分级组合框。

        代理样式强制使用下拉列表并归属组合框，绘制仍由全局样式处理；初始化选中项后才连接写回信号。
        """
        combo = _NoWheelComboBox()
        proxy = _DropdownBelowStyle()
        proxy.setParent(combo)
        combo.setStyle(proxy)
        for c, text in _AGE_RATING_OPTIONS:
            combo.addItem(tr(text), c)
        sel = next((k for k in range(combo.count()) if combo.itemData(k) == code), 0)
        combo.setCurrentIndex(sel)
        # 连接放在 setCurrentIndex 之后，避免初始化时误触发写回
        combo.currentIndexChanged.connect(lambda _i, cb=combo, r=row: self._on_age_combo_changed(r, cb))
        return combo

    def _on_age_combo_changed(self, row: int, combo: QComboBox) -> None:
        """将合法行对应组合框的选中数据规范化后写回导入项，忽略失效行索引。"""
        if 0 <= row < len(self._entries):
            self._entries[row].age_rating = _normalize_age_rating(str(combo.currentData() or "G"))

    def _browse_files(self) -> None:
        """允许多选支持的归档文件，并逐项加入待导入列表。"""
        exts = " ".join(f"*{e}" for e in ARCHIVE_EXTENSIONS)
        files, _ = QFileDialog.getOpenFileNames(self, t("选择 Mod 文件"), "", f"{t('压缩包文件')} ({exts})")
        for f in files:
            self._add_entry(f)

    def _browse_folder(self) -> None:
        """选择单个文件夹作为导入来源，选定后添加并自动聚焦新行。"""
        folder = QFileDialog.getExistingDirectory(self, t("选择文件夹..."))
        if folder:
            self._add_entry(folder, auto_select=True)

    def _remove_selected(self) -> None:
        """仅从本次导入列表移除选中行并重置预览，无选择时提示；不删除输入文件。"""
        rows = set()
        for item in self._table.selectedItems():
            rows.add(item.row())
        # 没有选中项目时提示选择所需范围。
        if not rows:
            QMessageBox.information(self, t("导入 Mod"), tr("请先选中要取消导入的Mod"))
            return
        for row in sorted(rows, reverse=True):
            if 0 <= row < len(self._entries):
                del self._entries[row]
        self._refresh_table()
        # 移除后主动刷新预览区，否则被删项的预览图残留在预览框
        self._current_preview_index = 0
        self._show_current_preview(self._table.currentRow())
        self._update_preview_nav()

    def _on_selection_changed(self, row: int, col: int, prev_row: int, _prev_col: int) -> None:
        """保存上一条目的预览页码并恢复新条目的页码，再异步刷新图片及导航按钮。"""
        # 保存上一个条目的预览图索引
        if 0 <= prev_row < len(self._entries):
            self._entries[prev_row].selected_preview_index = self._current_preview_index
        # 恢复新条目的预览图索引
        if 0 <= row < len(self._entries):
            self._current_preview_index = self._entries[row].selected_preview_index
        else:
            self._current_preview_index = 0
        self._show_current_preview(row)
        self._update_preview_nav()

    def _show_current_preview(self, row: int = -1) -> None:
        """取消旧预览请求并按当前条目页码异步加载图片，同时记录本次导入的默认预览路径。"""
        self._cancel_preview_request()
        if row < 0:
            row = self._table.currentRow()
        if 0 <= row < len(self._entries):
            entry = self._entries[row]
            if entry.previews and 0 <= self._current_preview_index < len(entry.previews):
                # 记录用户当前查看的预览图路径（用于导入时确定默认预览图）
                entry.selected_preview_path = entry.previews[self._current_preview_index]
                self._current_preview_path = entry.selected_preview_path
                self._preview_label.clear()
                self._preview_label.setText(tr("加载预览图…"))
                self._preview_cancelled = Event()
                dpr = self._preview_label.devicePixelRatioF()
                task = _PreviewTask(
                    self._preview_request_id,
                    self._current_preview_path,
                    self._preview_label.size() * dpr,
                    self._preview_cancelled,
                )
                task.signals.ready.connect(self._on_preview_ready)
                QThreadPool.globalInstance().start(task)
                return
        self._current_preview_path = ""
        self._preview_label.clear()
        self._preview_label.setText(t("无预览图"))

    def _cancel_preview_request(self) -> None:
        """使已有请求序号失效并设置取消事件，阻止旧任务更新当前预览。"""
        self._preview_request_id += 1
        self._preview_cancelled.set()

    @Slot(int, str, QImage)
    def _on_preview_ready(self, request_id: int, path: str, image: QImage) -> None:
        """仅接受序号和路径均匹配的解码结果，在界面线程创建带设备像素比的 QPixmap。"""
        if request_id != self._preview_request_id or path != self._current_preview_path:
            return
        if image.isNull():
            self._preview_label.setText(t("无预览图"))
            return
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(self._preview_label.devicePixelRatioF())
        self._preview_label.setPixmap(pixmap)

    def done(self, result: int) -> None:
        """在任何对话框结束路径使异步预览请求失效，再交由 Qt 设置结果和隐藏窗口。"""
        self._cancel_preview_request()
        super().done(result)

    def _apply_import_column_widths(self, force_ratio: bool = False, preferred_col: int = 1) -> None:
        """按可用宽度分配或补齐列宽，保留分级文本最小宽度；手动调整后允许水平滚动而不强制收缩。"""
        if self._import_header_dragging:
            return
        if self._import_syncing_widths:
            return
        self._import_syncing_widths = True
        try:
            viewport_w = max(420, self._table.viewport().width())
            minimum_widths = list(self._import_col_min_widths)
            combo = self._table.cellWidget(0, 2)
            if combo is not None:
                option = QStyleOptionViewItem()
                self._table.initViewItemOption(option)
                option.rect = self._table.visualRect(self._table.model().index(0, 2))
                content = self._table.style().subElementRect(
                    QStyle.SubElement.SE_ItemViewItemText,
                    option,
                    self._table,
                )
                # 单元格、箭头和文字边距共同占用宽度，需保证各语言分级文本可读。
                padding = max(0, self._table.columnWidth(2) - content.width())
                minimum_widths[2] = max(minimum_widths[2], combo.sizeHint().width() + padding)
            if force_ratio:
                used = 0
                for idx in range(3):
                    w = max(minimum_widths[idx], int(viewport_w * self._import_col_ratios[idx]))
                    self._table.setColumnWidth(idx, w)
                    used += w
                self._table.setColumnWidth(3, max(minimum_widths[3], viewport_w - used))

            widths = [self._table.columnWidth(i) for i in range(4)]
            total_w = sum(widths)

            if total_w < viewport_w:
                target_col = preferred_col if 0 <= preferred_col < 4 else 1
                self._table.setColumnWidth(target_col, widths[target_col] + (viewport_w - total_w))
                return

            if not force_ratio:
                # 用户已手动调整列宽，不强制收缩，让水平滚动条自然出现
                return

            overflow = total_w - viewport_w
            # 来源路径列(1)信息量最大，放到最后才收缩；优先压缩预览图/年龄分级/名称
            shrink_order = [3, 2, 0, 1]
            if 0 <= preferred_col < 4 and preferred_col in shrink_order:
                shrink_order = [idx for idx in shrink_order if idx != preferred_col] + [preferred_col]

            for idx in shrink_order:
                if overflow <= 0:
                    break
                cur = self._table.columnWidth(idx)
                min_w = minimum_widths[idx]
                can_shrink = max(0, cur - min_w)
                if can_shrink <= 0:
                    continue
                cut = min(overflow, can_shrink)
                self._table.setColumnWidth(idx, cur - cut)
                overflow -= cut
        finally:
            self._import_syncing_widths = False

    def _on_import_header_section_resized(self, _logical_index: int, old_size: int, new_size: int) -> None:
        """记录非程序同步引起的列宽变化，使后续窗口调整保留手动设置。"""
        if self._import_syncing_widths:
            return
        if old_size != new_size:
            self._import_user_resized = True

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化后更新列宽，表头正在拖动时暂缓，避免与用户操作竞争。"""
        super().resizeEvent(event)
        if self._import_header_dragging:
            return
        self._apply_import_column_widths(force_ratio=not self._import_user_resized)

    def _preview_image_rect(self):
        """预览图在 label 内的实际居中矩形；无图时返回 None。"""
        pm = self._preview_label.pixmap()
        if pm is None or pm.isNull():
            return None
        dpr = pm.devicePixelRatio() or 1.0
        pw = int(round(pm.width() / dpr))
        ph = int(round(pm.height() / dpr))
        x0 = (self._preview_label.width() - pw) // 2
        y0 = (self._preview_label.height() - ph) // 2
        return QRect(x0, y0, pw, ph)

    def _on_preview_label_clicked(self, event) -> None:
        """仅在点击实际图片矩形时打开查看器，居中图片周围的留白不触发操作。"""
        r = self._preview_image_rect()
        if r is not None and r.contains(event.pos()):
            self._open_current_preview_viewer()

    def _on_preview_label_moved(self, event) -> None:
        """指针位于实际图片范围时显示可点击光标，在留白区域恢复箭头。"""
        r = self._preview_image_rect()
        inside = r is not None and r.contains(event.pos())
        self._preview_label.setCursor(Qt.CursorShape.PointingHandCursor if inside else Qt.CursorShape.ArrowCursor)
