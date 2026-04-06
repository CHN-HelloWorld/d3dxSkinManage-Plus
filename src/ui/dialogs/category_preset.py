"""使用可取消服务操作完成分类预设流程。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import QEvent, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui.acrylic import apply_dialog_acrylic
from src.ui.dialog_chrome import center_dialog
from src.ui.dialogs.category_preset_style import stylesheet
from src.ui.dialogs.category_preset_widgets import (
    FILE_FILTER,
    PresetInspector,
    PresetTree,
    button,
    label,
    panel,
    preset_filename,
    preset_name_error,
    stat_strip,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _discard_plans(service, pending):
    """对话框串行调度任务，清理成功前保留所有权。"""
    while pending:
        service.discard(pending[0])
        del pending[0]


class _PresetTask(QThread):
    """串行执行可取消的预设服务调用，将结果、取消状态和异常留给界面线程处理。"""

    progress = Signal(str, int)

    def __init__(self, work, parent):
        """保存接受 cancelled 与 progress 关键字的工作函数，并由父窗口持有线程。"""
        super().__init__(parent)
        self.work = work
        self.cancel_event = Event()
        self.result = None
        self.error = None
        self.was_cancelled = False

    def run(self):
        """在后台注入取消检查和进度信号；区分服务主动取消与一般异常，异常记录日志供界面展示。"""
        from src.core.category_preset import CategoryPresetCancelled

        try:
            self.result = self.work(cancelled=self.cancel_event.is_set, progress=self.progress.emit)
        except CategoryPresetCancelled:
            self.was_cancelled = True
        except Exception as error:
            self.error = error
            logger.exception("分类预设操作失败")

    def request_cancel(self):
        """设置线程安全的取消标记，由服务在允许取消的检查点响应。"""
        self.cancel_event.set()


class CategoryPresetDialog(QDialog):
    """管理分类预设的预览、导入、导出及暂存资源，关闭前串行完成取消和清理。"""

    import_completed = Signal(str)
    view_categories_requested = Signal(str)
    games_requested = Signal()

    def __init__(self, manager, game_id, parent=None, *, start_import=False, start_export=False):
        """固定目标游戏并读取分类数量，按入口选择页面；窗口持有计划、暂存清理队列和后台任务。"""
        super().__init__(parent)
        self.setObjectName("categoryPresetDialog")
        self.setModal(True)
        self.setAcceptDrops(True)
        self._manager = manager
        self.game_id = game_id
        self._service_instance = None
        self._worker = None
        self._task_kind = ""
        self._task_success = None
        self._close_requested = False
        self._committed = False
        self._import_plan = None
        self._export_plan = None
        self._pending_discards = []
        self._source = ""
        self._destination = ""
        self._include_images = True
        self._omitted_images = set()
        self._page = None
        self._image_waiters = []
        self._mode = ""
        self._theme = theme.current_theme()
        self._game = manager._game_service.get_game(game_id) if game_id else None
        self._game_name = self._game.name if self._game else ""
        self._export_name = tr("{game_name}-分类预设", game_name=self._game_name)
        self._root = manager._index.paths.roots.get(game_id) if self._game else None
        self._category_count = 0
        if self._game:
            self._category_count = (
                manager._index._db().execute("SELECT COUNT(*) FROM nodes WHERE game_id=?", (game_id,)).fetchone()[0]
            )
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self.setStyleSheet(stylesheet())
        if not self._game:
            self._show_no_game()
        elif start_import:
            self._show_source()
        elif start_export and self._category_count:
            self._prepare_export()
        else:
            self._show_manage()
        # 分类树和检查区使用稳定底色，外壳保留适度透明并安装统一边框。
        apply_dialog_acrylic(self, tint_alpha=0.86)
        self._center_on_parent()

    @property
    def busy(self):
        """返回是否仍持有未结算的后台任务，包括已结束但尚未处理完成信号的线程。"""
        return self._worker is not None

    @property
    def _service(self):
        """首次使用时为当前管理器创建预设服务，并在窗口生命周期内复用。"""
        if self._service_instance is None:
            from src.core.category_preset_service import CategoryPresetService

            self._service_instance = CategoryPresetService(self._manager)
        return self._service_instance
