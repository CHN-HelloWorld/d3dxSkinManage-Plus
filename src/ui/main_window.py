"""主窗口 - 信号编排、页面切换、业务逻辑连接"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from src.core.archive_concurrency import effective_archive_workers
from src.core.config import Config
from src.core.deploy_concurrency import effective_deploy_concurrency
from src.core.game_config_store import GameConfigStore
from src.core.game_service import GameConfig, GameService
from src.core.migoto import MigotoIntegration
from src.core.mod_exporter import ExportItem
from src.core.mod_intake import ModIntakeService
from src.core.mod_manager import ModManager, ResourceDirectoryMigrationError
from src.core.mod_package import share_meta_from_mod
from src.core.watcher import FileWatcher
from src.models.mod import AgeRating, Mod
from src.ui import anim, icons
from src.ui.dialogs.delete_progress import DeleteProgressDialog, delete_mod_with_fallback
from src.ui.dialogs.export_progress import ExportProgressDialog
from src.ui.dialogs.mod_edit import ModEditDialog
from src.ui.dialogs.mod_import import ModImportDialog
from src.ui.intake_worker import IntakeBatchResult, IntakeWorker
from src.ui.library_reconcile_worker import LibraryReconcileController
from src.ui.pages.about import AboutPage
from src.ui.pages.gallery import GalleryPage
from src.ui.pages.game import GamePage
from src.ui.pages.overview import CharacterCard, OverviewPage
from src.ui.pages.plugins import PluginsPage
from src.ui.pages.settings import SettingsPage
from src.ui.pages.tools import ToolsPage
from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings
from src.ui.widgets.background_overlay import BackgroundOverlay
from src.ui.widgets.lock_overlay import LockOverlay
from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_ROLLED_BACK,
    ST_RUNNING,
    TaskCenterPanel,
    TaskCenterStack,
)
from src.utils import fs
from src.utils.constants import (
    APP_DISPLAY_NAME,
    MIGOTO_LOADER_NAME,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    MOD_CONTAINER_MARKER,
)
from src.utils.i18n import t
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir
from src.utils.qt_timers import call_later


def _plugin_operation_busy(window) -> bool:
    """返回插件文件操作服务是否正在执行任务，服务尚未创建时视为空闲。"""
    operations = getattr(window, "_plugin_operations", None)
    return operations is not None and operations.busy


def _deployment_operation_busy(window, *, include_publication=True) -> bool:
    """汇总部署激活、启用发布、变体、预设和停用任务的忙碌状态，可选择忽略发布队列。"""
    activation = getattr(window, "_deployment_activation", None)
    publication = getattr(window, "_enable_publication", None)
    variants = getattr(window, "_variant_controller", None)
    return bool(
        (activation is not None and activation.busy)
        or (include_publication and publication is not None and publication.busy)
        or (variants is not None and variants.busy)
        or getattr(window, "_preset_delete_preparing", False)
        or getattr(window, "_preset_delete_worker", None)
        or getattr(window, "_preset_apply_preparing", False)
        or getattr(window, "_preset_apply_worker", None)
        or getattr(window, "_disable_worker", None)
    )


def _invalidates_library_probe(method):
    """操作结算后标记失效，在途写入由核心修改范围保护。"""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        """调用被装饰的窗口操作，并在成功或异常退出后使已有库探测结果失效。"""
        probe = getattr(self, "_library_probe", None)
        try:
            return method(self, *args, **kwargs)
        finally:
            if probe is not None:
                probe.invalidate()

    return wrapped


class _NavItemDelegate(QStyledItemDelegate):
    """在折叠导航栏中按控件中线居中绘制 SVG 图标。

    图标位置独立于样式内边距和平台焦点边距，背景及选中状态仍交由当前样式绘制。
    展开时使用默认代理绘制图标和文字。
    """

    def __init__(self, parent=None):
        """初始化导航项代理，默认使用包含图标与文字的展开绘制模式。"""
        super().__init__(parent)
        self.collapsed = False

    def paint(self, painter, option, index):
        """展开时使用默认绘制，折叠时保留选中背景并将图标居中而不绘制文字。"""
        if not self.collapsed:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # 先复制 QIcon，再清空样式选项中的图标，避免引用同一内部对象导致原图也被清空。
        icon = QIcon(opt.icon)
        # 先让样式画背景/hover/selected（图标与文字都清掉，避免它按左对齐再画一次）
        opt.icon = QIcon()
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if icon.isNull():
            return
        size = opt.decorationSize
        rect = QRect(0, 0, size.width(), size.height())
        # 视口坐标 → 控件中线：viewport.x 就是 QSS padding 造成的左侧偏移量
        view = widget
        cx = option.rect.center().x()
        if isinstance(view, QListWidget):
            cx = view.width() // 2 - view.viewport().x()
        rect.moveCenter(QPoint(cx, option.rect.center().y()))
        icon.paint(painter, rect, Qt.AlignmentFlag.AlignCenter)
