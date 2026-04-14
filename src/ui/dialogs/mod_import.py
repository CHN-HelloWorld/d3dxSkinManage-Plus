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
