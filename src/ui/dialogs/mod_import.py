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
