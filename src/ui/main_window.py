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


class MainWindow(QMainWindow):
    """应用主窗口"""

    # 页面索引
    PAGE_OVERVIEW = 0
    PAGE_GALLERY = 1
    PAGE_SETTINGS = 2
    PAGE_TOOLS = 3
    PAGE_STORE = 4
    PAGE_GAME = 5
    PAGE_PLUGINS = 6
    PAGE_ABOUT = 7

    # ini「部署→源」同步在 watchdog 后台线程触发，经此信号（queued）marshal 回主线程，
    # 刷新按键面板——修「从部署目录改 ini 后，软件内按键显示残留、需重选卡片才更新」。
    ini_synced_from_deploy = Signal(str)
    # 3DMigoto 写入磁盘 d3dx_user.ini（按 F10 / 游戏退出）→ watchdog 线程发此信号，
    # 主线程再采集变体。**必须**经信号 marshal：采集要读线程绑定的 sqlite。
    user_ini_changed = Signal(object)
    variant_event_received = Signal(str, object)
    operation_failed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建主窗口服务、页面及后台事务控制器，并连接文件监控、部署和库刷新生命周期。

        界面构建后加载游戏数据、背景与插件，预热常用交互资源；后台任务结果通过所属线程信号与控制器发布。"""
        super().__init__(parent)
        self._config = Config()
        from src.ui.image_memory_runtime import ImageMemoryMonitor

        self._image_memory_monitor = ImageMemoryMonitor(self._config, self)
        self._shortcut_settings = ShortcutSettings(self, self._config)
        self._game_store = GameConfigStore()
        self._game_service = GameService()
        self._game_settings_saving = False
        self._mod_manager = ModManager()
        self._intake_service = ModIntakeService(self._mod_manager)
        self._intake_worker: IntakeWorker | None = None
        # 结算对话框可能继续运行嵌套事件循环，须在工作线程释放后保留结算标志，
        # 阻止目录监控或手动刷新启动新的纳管批次。
        self._intake_settling = False
        self._intake_rescan_pending = False
        self._intake_rescan_manual = False
        self._manual_refresh_active = False
        self._intake_item_names: list[str] = []
        # 本批已提交的 Mod（供「取消全部纳管」整批回滚）与用户的终止决策
        self._intake_committed_mods: list = []
        self._intake_cancel_all = False
        # 持有非模态导入控制器，确保异步导入期间实例存活。
        self._import_controller = None
        # 商店安装请求按批次排队，避免同时完成的下载替换正在使用的导入控制器。
        self._pending_gb_installs: list = []
        self._preset_apply_worker = None
        self._preset_apply_progress = None
        self._preset_apply_preparing = False
        self._preset_apply_game_id = ""
        self._preset_apply_token = None
        self._preset_apply_cancelled = False
        self._preset_journal_error = None
        self._preset_apply_plan = None
        self._preset_preparation = None
        self._deploy_workers = {}
        self._deferred_enable_ids = {}
        self._deploy_conflict_mod_id = ""
        self._pending_deploy_ids: deque[str] = deque()
        self._deploy_active: set[str] = set()
        self._deploy_stopping = False
        self._deploy_close_pending = False
        self._deploy_game_id = ""
        self._deploy_environment_dirty = False
        # 批量禁用后台线程（同时只允许一批；见 _on_overview_batch_disable）
        self._disable_worker = None
        self._disable_entries: tuple = ()
        self._disable_done = 0
        self._disable_game_id = ""
        # 持有非模态导出控制器，确保实例存活，并限制同时仅运行一个导出批次。
        self._export_controller = None
        self._file_watcher = FileWatcher(self)
        self._deploy_watcher = FileWatcher(self, track_containers=False)
        self._migoto = MigotoIntegration(self)
        # 文件监控防抖定时器
        self._watcher_debounce = QTimer(self)
        self._watcher_debounce.setSingleShot(True)
        self._watcher_debounce.setInterval(0)
        self._watcher_debounce.timeout.connect(self._refresh_from_watcher)
        # 合并防抖期间的变化路径以限定对账范围；被跳过轮次的路径保留到下次处理。
        self._watcher_changed_paths: set[Path] = set()
        self._watcher_events = []
        self._deploy_changed_paths = set()
        # 部署目录只关心“顶层 Mod 被外部删除”。预设切换也会产生合法的临时改名事件，
        # debounce 和周期轮询都必须服从预设事务锁，不能靠固定延迟猜测提交已经完成。
        self._deploy_watcher_debounce = QTimer(self)
        self._deploy_watcher_debounce.setSingleShot(True)
        self._deploy_watcher_debounce.setInterval(0)
        self._deploy_watcher_debounce.timeout.connect(self._refresh_deploy_changes)
        # Windows/网络盘上的 watchdog 偶尔会漏掉顶层目录删除事件。轮询只读取启用记录并
        # stat 已记录路径，成本很低，保证外删后无需 F5 也会在约 1 秒内恢复禁用态。
        self._deploy_state_poll = QTimer(self)
        self._deploy_state_poll.setInterval(1000)
        self._deploy_state_poll.timeout.connect(self._reconcile_deploy_state)
        # 源库同样不能只依赖 watchdog：Windows 递归删除目录时可能把目录误报为普通文件，
        # 网络盘/杀毒也可能漏事件。周期对账只做 DB 路径 exists 检查，有变化才刷新视图。
        self._source_state_poll = QTimer(self)
        self._source_state_poll.setInterval(1500)
        self._source_state_poll.timeout.connect(self._reconcile_source_state)
        # 外部删除源容器后，若游戏/杀毒暂时占用部署文件，ModManager 会保留索引记录作为
        # 耐久清理凭据。本定时器只重试清理，不做无意义的整页周期刷新。
        self._source_cleanup_retry = QTimer(self)
        self._source_cleanup_retry.setSingleShot(True)
        self._source_cleanup_retry.timeout.connect(self._retry_pending_source_cleanup)
        self._source_cleanup_retry_delays = (3000, 10000, 30000, 60000)
        self._source_cleanup_retry_step = 0
        self._source_cleanup_notified = False
        # 本程序自身写盘（导入 / 新建 / 删除类别等）也会触发 watcher，与紧随其后的直接刷新
        # 重复重建卡片。抑制窗口内的事件由该次程序内操作直接刷新覆盖，不能再延后做全库
        # 扫描；延后扫描会在主线程打断卡片逐项淡入和对话框收尾。
        self._watcher_suppress_until = 0.0
        # 导入期间暂停 watcher 触发的 _refresh_current，避免对账将待压缩或删除的
        # 中间目录登记为独立 Mod。异步导入期间去抖定时器仍会触发，因此需要显式状态保护。
        self._import_active = False
        self._setup_ui()
        from src.ui.deployment_activation import DeploymentActivationController

        self._deployment_activation = DeploymentActivationController(self._mod_manager, self)
        self._deployment_activation.settled.connect(self._on_deployment_activation_settled)
        self._deployment_activation.failed.connect(self._on_deployment_activation_failed)
        self._deployment_activation.discarded.connect(self._on_deployment_activation_discarded)
        self._deployment_activation.stopped.connect(self._on_deployment_activation_stopped)
        self._deployment_activation_close_pending = False
        from src.ui.deployment_enable import EnablePublicationController

        self._enable_publication = EnablePublicationController(self._mod_manager, self._on_async_deploy_finished, self)
        self._enable_publication.idle.connect(self._on_enable_publication_idle)
        self._enable_publication.stopped.connect(self._on_enable_publication_stopped)
        self._enable_publication_close_pending = False
        self._library_probe = LibraryReconcileController(
            self._mod_manager, busy=lambda: _plugin_operation_busy(self) or self._host_library_busy(), parent=self
        )
        self._library_probe_metrics = self._library_probe.metrics
        self._library_probe.changed.connect(self._refresh_after_source_reconcile)
        self._library_probe.source_observed.connect(self._on_source_observed)
        self._library_probe.settled.connect(self._schedule_source_cleanup_retry)
        self._library_probe.stopped.connect(self._on_library_probe_stopped)
        self._library_probe_close_pending = False
        from src.ui.library_discovery import LibraryDiscoveryController

        self._library_discovery = LibraryDiscoveryController(
            self._mod_manager,
            self._library_probe,
            busy=lambda: self._host_library_busy() or _plugin_operation_busy(self),
            consume=self._consume_library_discovery,
            parent=self,
        )
        self._library_discovery.stopped.connect(self._on_library_probe_stopped)
        self._library_discovery.failed.connect(self._on_library_discovery_failed)
        self._library_local_discovery = LibraryDiscoveryController(
            self._mod_manager,
            self._library_probe,
            busy=lambda: False,
            consume=self._consume_library_discovery,
            parent=self,
        )
        self._library_local_discovery.stopped.connect(self._on_library_probe_stopped)
        from src.ui.overview_refresh import OverviewRefreshController

        self._overview_refresh = OverviewRefreshController(
            self._mod_manager,
            current=self._overview_query_key,
            recursive=lambda: bool(
                self._overview_page._search_input.text().strip() or self._overview_page._tag_filter.selected_ids
            ),
            cache_levels=4,
            on_loading=lambda _game: self._overview_page.begin_loading(),
            on_error=self._overview_page.loading_failed,
            apply=self._apply_overview_mods,
            prepare_item=lambda mod: self._overview_page._sort_text_key(mod.display_name),
            parent=self,
        )
        self._overview_refresh.BUDGET_MS = 8
        self._overview_page.set_mod_loader(self._overview_refresh.schedule)
        self._overview_page.set_enabled_mod_ids_loader(self._mod_manager.get_enabled_mod_ids)
        self._gallery_refresh = OverviewRefreshController(
            self._mod_manager,
            current=lambda: (self._game_service.current_game_id if self._game_service.has_active_game() else "", ""),
            apply=lambda _direct, mods: self._gallery_page.set_mods(mods),
            on_loading=self._gallery_page.set_loading,
            on_error=self._gallery_page.set_load_failed,
            on_progress=self._gallery_page.set_loading_progress,
            parent=self,
        )
        from src.ui.deployment_root_watch import DeploymentRootWatch

        self._deployment_root_watch = DeploymentRootWatch(
            self._mod_manager,
            self._game_service,
            busy=lambda: (
                self._host_library_busy() or _plugin_operation_busy(self) or self._deployment_activation.closing
            ),
            parent=self,
        )
        self._deployment_root_watch.relocated.connect(self._on_deployment_root_relocated)
        self._deployment_root_watch.invalid.connect(self._on_deployment_root_invalid)
        self._deployment_root_watch.available.connect(self._on_deployment_root_available)
        self._deployment_root_watch.failed.connect(self._on_deployment_root_failed)
        self._connect_signals()
        self._setup_shortcuts()
        self._init_data()
        self.apply_background()  # 应用启动时加载自定义背景。

        # 插件系统：发现 + 激活（在数据/游戏就绪后，使插件激活时只读门面可用）
        self._init_plugins()
        self._prepare_interaction_dialogs()
        self._directory_cards_prepared = False
        self._directory_card_prepare_timer = QTimer(self)
        self._directory_card_prepare_timer.setSingleShot(True)
        self._directory_card_prepare_timer.timeout.connect(self._prepare_directory_cards)
        self._prepare_directory_cards()
        if getattr(self._mod_manager, "category_preset_recovery_errors", ()):
            self._category_recovery_notice_timer = QTimer(self)
            self._category_recovery_notice_timer.setSingleShot(True)
            self._category_recovery_notice_timer.timeout.connect(self._show_category_preset_recovery_notice)
            self._category_recovery_notice_timer.start(0)

    def _prepare_interaction_dialogs(self) -> None:
        """主窗口接受输入前准备常用静态表单。"""
        from src.models.mod import Mod
        from src.ui.win_effects import disable_open_close_transition

        self._mod_edit_dialog = ModEditDialog(Mod(), parent=self)
        self._mod_edit_dialog._fit_to_screen()
        self._mod_updates_cached_dialog = MainWindow._create_mod_updates_dialog(self)
        for dialog in (
            self._mod_edit_dialog,
            self._mod_updates_cached_dialog,
            self._overview_page.prepare_batch_add_dialog(),
        ):
            dialog.ensurePolished()
            dialog.layout().activate()
            dialog.winId()
            disable_open_close_transition(dialog)

    def _prepare_directory_cards(self) -> None:
        """在背景材质就绪后按预计可视区域预热总览卡片池，避免首次导航集中创建控件。"""
        if getattr(self, "_directory_cards_prepared", False) or getattr(self, "_appearance_disposed", False):
            return
        background = getattr(self, "_background_controller", None)
        surfaces = getattr(QApplication.instance(), "_background_style", None)
        if background is not None and (
            background.state == "loading" or (surfaces is not None and surfaces.enabled != background.active)
        ):
            # 首帧异步确定背景材质，等待其就绪再准备表单，避免隐藏控件重复处理样式。
            return
        viewport_size = self.size()
        screen = QApplication.primaryScreen()
        if getattr(self, "_restore_maximized", False) and screen is not None:
            viewport_size = screen.availableGeometry().size()
        self._overview_page.prepare_visible_card_pool(
            QSize(max(1, viewport_size.width() - self._nav_container.width()), max(1, viewport_size.height() - 120))
        )
        self._directory_cards_prepared = True

    def _schedule_directory_card_preparation(self) -> None:
        """尚未预热目录卡片时安排下一轮事件循环执行准备。"""
        timer = getattr(self, "_directory_card_prepare_timer", None)
        if timer is not None and not self._directory_cards_prepared:
            timer.start(0)

    def _plugin_host_busy(self) -> bool:
        """宿主文件工作和提交完成前保留插件写入边界。"""
        probe = getattr(self, "_library_probe", None)
        return bool(probe and probe.applying) or MainWindow._host_library_busy(self)

    def _host_library_busy(self) -> bool:
        """判断文件任务、跨游戏更新或宿主模态窗口是否需要阻止库审计与游戏环境变更。"""
        if getattr(self, "_game_settings_saving", False):
            return True
        if _deployment_operation_busy(self):
            return True
        if any(
            bool(getattr(self, name, None))
            for name in (
                "_deploy_workers",
                "_deploy_active",
                "_preset_apply_worker",
                "_disable_worker",
                "_intake_worker",
                "_intake_settling",
                "_import_active",
            )
        ):
            return True
        updates = getattr(self, "_mod_update_service", None)
        if updates is not None and updates.active_update_ids(all_games=True):
            return True
        from PySide6.QtWidgets import QApplication

        # 插件可以打开模态窗口，宿主编辑和确认仍保护各自尚未提交的修改。
        return any(
            widget.isVisible() and widget.isModal() and widget.objectName() != "pluginDialogSurface"
            for widget in QApplication.topLevelWidgets()
        )

    @_invalidates_library_probe
    def _on_plugin_activity_changed(self, busy: bool) -> None:
        """插件任务期间锁定游戏设置，结束后恢复延后的部署、安装、来源监控与变体操作。"""
        host_deploying = bool(getattr(self, "_deploy_workers", None) or getattr(self, "_deploy_active", None))
        self._game_page.setEnabled(not (busy or host_deploying))
        self._game_page.setToolTip(tr("插件操作完成后可切换游戏或修改部署目录。") if busy else "")
        if not busy:
            if self._deploy_environment_dirty:
                self._deploy_environment_dirty = False
                call_later(0, self, self._apply_migoto_for_current_game)
            if self._pending_gb_installs:
                call_later(0, self, self._drain_gb_installs)
            if self._watcher_changed_paths:
                self._plugin_watcher_pending = True
                self._watcher_debounce.start()
            if getattr(self, "_plugin_intake_pending", None) is not None:
                call_later(0, self, self._resume_plugin_deferred_intake)
            if getattr(self, "_plugin_variant_pending", False):
                call_later(0, self, self._resume_pending_variant_capture)
            if getattr(self, "_deferred_enable_ids", {}):
                self._schedule_deferred_enables()

    def _on_plugin_operation_finished(self, result) -> None:
        """汇总插件操作结果，并仅为当前游戏刷新受影响页面、商店与预设状态。"""
        panel = getattr(self, "_plugin_progress", None)
        if panel is not None:
            panel.finish(
                tr("插件操作已完成") if result.ok else tr("插件操作未完成"),
                result.error
                or tr(
                    "成功 {count} 项，失败 {count_2} 项，取消 {count_3} 项",
                    count=len(result.succeeded_ids),
                    count_2=len(result.failed_ids),
                    count_3=len(result.cancelled_ids),
                ),
                ok=result.ok,
            )
        if result.game_id == self._mod_manager.current_game_id and result.succeeded_ids:
            self._suppress_watcher()
            self._reload_overview_level(result.game_id)
            if self._stack.currentIndex() == self.PAGE_GALLERY:
                self._refresh_gallery()
            self._refresh_store_installed_state()
        if result.game_id == self._mod_manager.current_game_id and (result.succeeded_ids or result.preset_id):
            self._refresh_preset_combo()
        if not result.ok and result.error:
            self._ensure_toast().done(result.error, ok=False, hold_ms=2500)

    def _on_plugin_operation_started(self, operation: str, ids) -> None:
        """创建或复用插件任务面板，按操作类型初始化批次进度并连接停止请求。"""
        if getattr(self, "_plugin_progress", None) is None:
            self._plugin_progress = TaskCenterPanel(self._stack, stack=self._panel_stack)
            self._plugin_progress.stop_requested.connect(self._plugin_operations.cancel_all)
        labels = {
            "disable_all": tr("禁用 Mod"),
            "delete_mod": tr("删除 Mod"),
            "set_enabled": tr("切换 Mod 状态"),
            "toggle_mod": tr("切换 Mod 状态"),
        }
        self._plugin_progress.start_batch(
            len(ids),
            labels.get(operation, tr("插件操作")),
            stop_tooltip=tr("停止后续插件操作"),
            cancelled_title=tr("插件操作已停止"),
            done_text=tr("已完成"),
        )

    def _on_plugin_operation_progress(self, mod_id: str, fraction: float) -> None:
        """把插件操作的零到一进度转换为任务面板百分比。"""
        self._plugin_progress.update_progress(fraction * 100, tr("正在处理 Mod"), "")

    def _init_plugins(self) -> None:
        """初始化插件运行时及界面扩展。"""
        try:
            from src.core.mod_operations import ModOperationService
            from src.plugins import PluginManager

            self._plugin_operations = ModOperationService(
                self._mod_manager,
                self,
                host_busy=self._plugin_host_busy,
                error_sink=lambda owner, error: self._plugin_manager._on_plugin_error(
                    owner, "operation callback", error
                ),
            )
            self._plugin_operations.activity_changed.connect(self._on_plugin_activity_changed)
            self._plugin_operations.finished.connect(self._on_plugin_operation_finished)
            self._plugin_operations.started.connect(self._on_plugin_operation_started)
            self._plugin_operations.progress.connect(self._on_plugin_operation_progress)
            self._plugin_manager = PluginManager(self._mod_manager, operations=self._plugin_operations)
            from src.plugins.runtime.tasks import BackgroundTaskRunner
            from src.ui.plugin_tasks import PluginTaskDispatcher

            self._plugin_task_dispatcher = PluginTaskDispatcher(self)
            self._plugin_manager.set_event_dispatcher(self._plugin_task_dispatcher.post)
            self._plugin_manager.set_task_runner(
                BackgroundTaskRunner(
                    self._plugin_task_dispatcher.post, error_sink=self._plugin_manager._on_plugin_error
                )
            )
            from src.plugins.runtime.shortcuts import PluginShortcutHost

            self._plugin_manager.set_shortcut_host(
                PluginShortcutHost(self._shortcut_settings, self._shortcut_dispatcher)
            )
            # 预设应用是 Qt 信号（非 ModManager 动作点），在此桥接进插件事件总线
            self._mod_manager.preset_manager.preset_applied.connect(
                lambda ids: self._plugin_manager.emit(
                    "preset_applied", affected_ids=list(ids), game_id=self._mod_manager.current_game_id
                )
            )
            # 插件变化后刷新工具页扩展按钮。
            self._plugin_manager.add_change_listener(self._refresh_plugin_tools)
            # 注册 Mod 卡片右键菜单提供者。
            from src.ui import plugin_menu

            plugin_menu.set_provider(self._build_mod_context_actions)
            # 加载插件前注入页面、对话框及通知的界面宿主。
            from src.ui.plugin_ui_host import PluginUIHost

            self._plugin_ui_host = PluginUIHost(self)
            self._plugin_manager.set_ui_host(self._plugin_ui_host)
            self._plugins_page.set_manager(self._plugin_manager)
            self._plugin_manager.load_all()
        except Exception:
            logger.exception("插件系统初始化失败（不影响主程序）")
            self._plugin_manager = None

    def _build_mod_context_actions(self, location: str, context_id: str) -> list:
        """按位置收集插件菜单项和回调。

        Mod 菜单传入 mod_id，类别菜单传入 node_id，全局菜单不传上下文对象。
        """
        pm = getattr(self, "_plugin_manager", None)
        if pm is None:
            return []
        out: list = []
        if location == "global":
            # 全局菜单 = 所有插件命令（命令面板式入口）
            for c in pm.contributions.commands():
                cmd = c.command
                if cmd:
                    out.append(
                        (
                            tr("{plugin_name}：{title}", plugin_name=c.plugin_name, title=c.title),
                            lambda _cmd=cmd: pm.execute_command(_cmd),
                        )
                    )
            return out

        kwarg = {"mod-context": "mod_id", "character-context": "node_id"}.get(location)
        for c in pm.contributions.menus(location):
            when = str(c.data.get("when", "")).strip()
            if when and when not in ("always", "modSelected", "characterSelected"):
                continue
            cmd = c.command
            if not cmd:
                continue
            title = c.title if c.title else cmd
            if kwarg:
                out.append((title, lambda _cmd=cmd, _v=context_id, _k=kwarg: pm.execute_command(_cmd, **{_k: _v})))
            else:
                out.append((title, lambda _cmd=cmd: pm.execute_command(_cmd)))
        return out

    def _refresh_plugin_tools(self) -> None:
        """将插件工具页扩展同步到界面。"""
        pm = getattr(self, "_plugin_manager", None)
        if pm is None:
            return
        try:
            self._tools_page.refresh_plugin_tools(pm.contributions.tools_page(), pm.execute_command)
        except Exception:
            logger.exception("刷新插件工具按钮失败")
        # 存在插件命令时显示导航入口。
        try:
            has_cmds = bool(pm.contributions.commands())
            self._plugin_cmd_btn.setVisible(has_cmds)
        except Exception:
            pass

    def _show_plugin_command_menu(self) -> None:
        """导航「插件命令」按钮：弹出所有插件命令（global 菜单位置）。"""
        from PySide6.QtWidgets import QMenu

        actions = self._build_mod_context_actions("global", "")
        if not actions:
            return
        menu = QMenu(self)
        act_map = {}
        for title, cb in actions:
            act_map[menu.addAction(title)] = cb
        try:
            from src.ui import win_effects

            menu.winId()
            win_effects.apply_rounded_corners(menu, small=True)
        except Exception:
            pass
        btn = self._plugin_cmd_btn
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen in act_map:
            act_map[chosen]()

    # UI 构建

    def _setup_ui(self) -> None:
        """构建导航、页面栈、背景和任务浮层，并恢复窗口与预览设置。

        同时初始化共享商店服务、Mod 更新服务和程序更新管理器，连接页面切换、部署同步与导航尺寸持久化。"""
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        # 恢复窗口尺寸时遵守最小尺寸；最大化状态在首次显示后单独恢复。
        _ww = int(self._config.get("ui.window_width", 0) or 0)
        _wh = int(self._config.get("ui.window_height", 0) or 0)
        if _ww >= MIN_WINDOW_WIDTH and _wh >= MIN_WINDOW_HEIGHT:
            self.resize(_ww, _wh)
        self._restore_maximized = bool(self._config.get("ui.window_maximized", False))

        central = QWidget()
        self.setCentralWidget(central)
        self._central = central
        # 背景位于中央控件底层，忽略鼠标事件并随窗口更新几何。
        self._bg_overlay = BackgroundOverlay(central)
        from src.ui.backgrounds.controller import BackgroundController

        self._background_controller = BackgroundController(self._bg_overlay, self)
        self._background_controller.state_changed.connect(self._on_bg_state_changed)
        self._background_controller.preview_ready.connect(self._on_bg_preview_ready)
        self._background_controller.active_changed.connect(self._on_bg_active_changed)
        self._bg_overlay.lower()
        # 背景模糊滑块拖动的节流器（模糊重算偏重，~40ms 合并一次，防逐帧高斯模糊卡顿）
        self._pending_bg_blur = 0
        self._bg_blur_throttle = QTimer(self)
        self._bg_blur_throttle.setSingleShot(True)
        self._bg_blur_throttle.timeout.connect(self._flush_bg_blur)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 左侧导航容器
        self._nav_container = QWidget()
        self._nav_container.setObjectName("navContainer")
        nav_container_layout = QVBoxLayout(self._nav_container)
        nav_container_layout.setContentsMargins(0, 0, 0, 0)
        nav_container_layout.setSpacing(0)

        self._nav = QListWidget()
        self._nav.setObjectName("navList")
        self._nav.setIconSize(QSize(20, 20))
        self._nav.setSpacing(2)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._nav_expanded_width = max(80, int(Config.instance().get("ui.nav_expanded_width", 160)))
        self._nav_collapsed = False
        self._nav_animating = False

        # 导航文本和图标（图标为 Fluent SVG 的语义名）
        self._nav_items_data = [
            ("actions/game", t("游戏"), self.PAGE_GAME),
            ("navigation/overview", t("类别总览"), self.PAGE_OVERVIEW),
            ("navigation/gallery", t("画廊"), self.PAGE_GALLERY),
            ("navigation/store", t("在线 Mod"), self.PAGE_STORE),
            ("navigation/settings", t("设置"), self.PAGE_SETTINGS),
            ("navigation/tools", t("工具箱"), self.PAGE_TOOLS),
            ("navigation/plugins", t("插件"), self.PAGE_PLUGINS),
        ]
        for icon_name, label, idx in self._nav_items_data:
            item = QListWidgetItem(label)
            icons.apply_icon(item, icon_name)
            item.setData(Qt.ItemDataRole.UserRole, idx)
            item.setData(Qt.ItemDataRole.UserRole + 1, icon_name)  # 保存图标名
            item.setData(Qt.ItemDataRole.UserRole + 2, label)  # 保存完整文本
            item.setSizeHint(QSize(140, 40))
            self._nav.addItem(item)

        self._nav.currentRowChanged.connect(self._on_nav_changed)
        # 折叠态图标居中（详见 _NavItemDelegate）
        self._nav_delegate = _NavItemDelegate(self._nav)
        self._nav.setItemDelegate(self._nav_delegate)
        # 导航指示条使用视口坐标跟随选中项移动，并在布局或尺寸变化后重新定位。
        self._nav_indicator = QWidget(self._nav.viewport())
        self._nav_indicator.setObjectName("navIndicator")
        self._nav_indicator.setFixedWidth(3)
        self._nav_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._nav_indicator.hide()
        self._nav.viewport().installEventFilter(self)
        nav_container_layout.addWidget(self._nav, 1)

        # 底部快捷启动按钮
        # 「启动Mod加载器」「启动游戏」均使用当前游戏在「游戏管理」中的配置，
        # 随游戏环境切换自动指向新游戏（见 _apply_migoto_for_current_game →
        # _update_launch_buttons）。折叠菜单时只显示图标。
        self._launch_buttons: list[tuple[QPushButton, str, str]] = []

        # 关于按钮（导航底部独立入口）
        # 关于页低频，放在主导航列表「下方」（列表 stretch=1 撑开，按钮被推到底部，
        # 与高频导航项以空白分隔）。复用 navLaunchBtn 外观/折叠/图标自动重涂机制；
        # 选中态由动态属性 current 驱动（见 fluent.qss #navLaunchBtn[current="true"]）。
        self._about_btn = QPushButton(t("关于"))
        self._about_btn.setObjectName("navLaunchBtn")
        self._about_btn.setProperty("current", False)
        icons.apply_icon(self._about_btn, "status/info")
        self._about_btn.setIconSize(QSize(18, 18))
        self._about_btn.clicked.connect(self._on_about_clicked)
        nav_container_layout.addWidget(self._about_btn)
        self._launch_buttons.append((self._about_btn, t("关于"), "status/info"))

        # 全局命令按钮展示已注册插件命令；无命令时隐藏，导航折叠时仅显示图标。
        self._plugin_cmd_btn = QPushButton(t("插件命令"))
        self._plugin_cmd_btn.setObjectName("navLaunchBtn")
        icons.apply_icon(self._plugin_cmd_btn, "navigation/plugins")
        self._plugin_cmd_btn.setIconSize(QSize(18, 18))
        self._plugin_cmd_btn.clicked.connect(self._show_plugin_command_menu)
        self._plugin_cmd_btn.hide()
        nav_container_layout.addWidget(self._plugin_cmd_btn)
        self._launch_buttons.append((self._plugin_cmd_btn, t("插件命令"), "navigation/plugins"))

        self._launch_loader_btn = QPushButton(t("启动Mod加载器"))
        self._launch_loader_btn.setObjectName("navLaunchBtn")
        icons.apply_icon(self._launch_loader_btn, "actions/launch")
        self._launch_loader_btn.setIconSize(QSize(18, 18))
        self._launch_loader_btn.clicked.connect(self._on_launch_migoto)
        nav_container_layout.addWidget(self._launch_loader_btn)
        self._launch_buttons.append((self._launch_loader_btn, t("启动Mod加载器"), "actions/launch"))

        self._launch_game_btn = QPushButton(t("启动游戏"))
        self._launch_game_btn.setObjectName("navLaunchBtn")
        icons.apply_icon(self._launch_game_btn, "actions/game")
        self._launch_game_btn.setIconSize(QSize(18, 18))
        self._launch_game_btn.clicked.connect(self._on_launch_game)
        nav_container_layout.addWidget(self._launch_game_btn)
        self._launch_buttons.append((self._launch_game_btn, t("启动游戏"), "actions/game"))

        # 折叠按钮
        self._collapse_btn = QPushButton()
        self._collapse_btn.setObjectName("navCollapseBtn")
        icons.apply_icon(self._collapse_btn, "actions/menu")
        self._collapse_btn.setIconSize(QSize(18, 18))
        self._collapse_btn.setFixedHeight(32)
        self._collapse_btn.setToolTip(tr("折叠/展开菜单"))
        self._collapse_btn.clicked.connect(self._toggle_nav_collapse)
        nav_container_layout.addWidget(self._collapse_btn)

        # 使用 QSplitter 让导航栏可调宽度
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._nav_container)

        # 右侧页面栈
        self._stack = QStackedWidget()

        self._overview_page = OverviewPage()
        self._overview_page._mod_manager = self._mod_manager  # 注入，供按键面板/override 编辑
        self._stack.addWidget(self._overview_page)  # 0

        # 部署 INI 同步可能来自工作线程，仅发出信号，由主线程刷新按键面板。
        self.ini_synced_from_deploy.connect(self._on_ini_synced_from_deploy)
        self._mod_manager.set_ini_sync_notify(self.ini_synced_from_deploy.emit)

        self._connect_variant_controller()

        self._gallery_page = GalleryPage()
        self._stack.addWidget(self._gallery_page)  # 1

        # 恢复上次的成人级Mod预览模式（show|hide|blur），两页保持一致
        _adult_mode = self._config.get("ui.adult_preview_mode", "show")
        self._overview_page.set_adult_mode(_adult_mode)
        self._gallery_page.set_adult_mode(_adult_mode)

        self._settings_page = SettingsPage(shortcuts=self._shortcut_settings)
        self._stack.addWidget(self._settings_page)  # 2

        self._tools_page = ToolsPage()
        self._stack.addWidget(self._tools_page)  # 3

        # GameBanana Mod Store 页
        self._gb_store_page = self._build_gb_store_page()
        self._stack.addWidget(self._gb_store_page)  # 4

        # 游戏管理页（独立顶级页，PAGE_GAME）
        self._game_page = GamePage(save_context=self._game_settings_save)
        self._stack.addWidget(self._game_page)  # 5
        self._game_page.game_changed.connect(self._on_game_changed)
        self._game_page.games_changed.connect(self._on_games_changed)
        self._game_page.deployment_directory_selected.connect(self._on_deployment_directory_selected)
        self._game_page.deploy_dir_cleared.connect(self._on_deploy_dir_cleared)

        # 插件管理页（PAGE_PLUGINS）
        self._plugins_page = PluginsPage()
        self._stack.addWidget(self._plugins_page)  # 6

        # 关于页（PAGE_ABOUT，导航底部入口）
        self._about_page = AboutPage()
        self._stack.addWidget(self._about_page)  # 7

        # 无激活游戏时仍允许进入内容页，由 _stack 子控件覆盖显示激活提示。
        # _update_lock_overlay 按当前页和激活状态控制显隐，随 _stack 尺寸变化同步几何。
        self._lock_overlay = LockOverlay(self._stack)
        self._lock_overlay.go_to_game_requested.connect(lambda: self._select_nav_page(self.PAGE_GAME))
        self._lock_overlay.hide()
        self._stack.installEventFilter(self)

        self._main_splitter.addWidget(self._stack)
        self._nav_expanded_width = max(self._nav_expanded_width, self._nav_container.minimumSizeHint().width())
        self._main_splitter.setSizes([self._nav_expanded_width, 800])
        # 用户拖动导航分隔条时保存展开宽度，程序调用 setSizes 不触发该信号。
        self._main_splitter.splitterMoved.connect(self._on_nav_splitter_moved)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        main_layout.addWidget(self._main_splitter, 1)

        # 任务浮层按进入顺序由下向上排列；某层收起时，其上方的可见面板下移补位。
        self._panel_stack = TaskCenterStack()
        self._intake_progress = TaskCenterPanel(self._stack, stack=self._panel_stack)
        self._intake_progress.stop_requested.connect(self._on_panel_stop_requested)
        self._export_progress = TaskCenterPanel(self._stack, stack=self._panel_stack)
        # 任务中心与页面、锁定覆盖层同属堆栈。切页及过渡快照会提升页面层级，
        # 因此监听 currentChanged 恢复任务中心层级，并在过渡结束后再次确认。
        self._stack.currentChanged.connect(self._on_stack_page_changed)

        # Mod 自动更新服务（复用共享 GB 客户端 + 商店页的长寿命 GB loop）
        from src.core.mod_update_service import ModUpdateService

        self._mod_update_service = ModUpdateService(
            self._mod_manager,
            self._config,
            self._gb_client,
            parent=self,
            # 更新检查与商店共享长期事件循环，确保 HTTP 连接池始终在其所属循环使用。
            submit=self._gb_store_page.submit_coro,
        )
        try:
            self._mod_update_service.set_download_manager(self._gb_store_page._download_manager)
        except Exception:
            pass
        self._connect_mod_update_service()
        self._mod_update_service.load_status()  # 恢复上次保存的更新检查结果。
        self._apply_periodic_update_timer()  # 按设置启动定时检查（若开启）

        # 程序自身自动更新管理器（区别于上面的「Mod」更新服务）
        from src.services.app_update import AppUpdateManager

        self._app_update_manager = AppUpdateManager(self, self._config)

    def _build_gb_store_page(self) -> QWidget:
        """构建 GameBanana Mod Store 页面（懒加载 services 资源）"""
        from src.services.gamebanana.cache import ImageCache
        from src.services.gamebanana.client import GameBananaClient
        from src.services.gamebanana.image_loader import ImageLoader
        from src.ui.pages.gamebanana_store import GameBananaStorePage
        from src.utils.proxy import get_active_proxy_url

        self._gb_client = GameBananaClient(
            max_concurrency=int(self._config.get("gamebanana.max_concurrency", 8)),
            request_interval_ms=int(self._config.get("gamebanana.request_interval_ms", 200)),
            proxy=get_active_proxy_url(self._config),
        )
        self._gb_image_cache = ImageCache()
        self._gb_image_loader = ImageLoader(self._gb_image_cache, parent=self)
        page = GameBananaStorePage(
            client=self._gb_client,
            image_loader=self._gb_image_loader,
            local_game_id=self._game_service.current_game_id,
            mod_index=self._mod_manager,
            parent=self,
        )
        page.open_settings_requested.connect(self._open_game_settings)
        page.open_local_mod_in_overview_requested.connect(self._open_local_mod_in_overview)
        # 下载完成后的安装：走与手动导入同一条后台流水线（见 _on_gb_install_requested）
        page.mod_install_requested.connect(self._on_gb_install_requested)
        return page

    # 信号连接

    def _connect_signals(self) -> None:
        # 变体持久化事件经 Qt 信号回到主线程，仅更新当前预览和配置写入提示。
        """将页面操作连接到主窗口事务入口，并桥接核心事件、文件监控、外观设置与预设通知。"""
        self._mod_manager.add_event_listener(self._on_core_event)
        self.operation_failed.connect(self._show_operation_failure, Qt.ConnectionType.QueuedConnection)
        from src.ui.operation_check import run_file_check

        self._mod_manager.operation_check_runner = lambda work: run_file_check(work, self)
        # 总览页。
        self._overview_page.game_changed.connect(self._on_game_changed)
        self._overview_page.character_add_requested.connect(self._on_character_add)
        self._overview_page.character_edit_requested.connect(self._on_character_edit)
        self._overview_page.character_delete_requested.connect(self._on_character_delete)
        self._overview_page.character_batch_add_requested.connect(self._on_character_batch_add)
        self._overview_page.character_batch_delete_requested.connect(self._on_character_batch_delete)
        self._overview_page.category_preset_import_requested.connect(
            lambda: self._open_category_presets(start_import=True)
        )
        # 混合视图 Mod 操作
        self._overview_page.mod_toggle_requested.connect(self._on_overview_mod_toggle)
        self._overview_page.mod_delete_requested.connect(self._on_overview_mod_delete)
        self._overview_page.mod_batch_delete_requested.connect(self._on_overview_mod_batch_delete)
        self._overview_page.batch_disable_requested.connect(self._on_overview_batch_disable)
        self._overview_page.items_export_requested.connect(self._on_overview_items_export)
        self._overview_page.mod_import_requested.connect(self._on_overview_import_requested)
        self._overview_page.files_dropped_for_import.connect(self._on_overview_files_dropped)
        # 导航
        self._overview_page.category_navigated.connect(self._on_overview_navigated)
        # 剪切/粘贴
        self._overview_page.items_move_requested.connect(self._on_items_move)
        # 总览页预览面板信号。
        self._overview_page.preview_image_dropped.connect(self._on_overview_preview_dropped)
        self._overview_page.category_preview_dropped.connect(self._on_category_preview_dropped)
        self._overview_page.mod_edit_requested.connect(self._on_mod_edit)
        self._overview_page.mod_preview_delete_requested.connect(self._on_overview_preview_delete)
        self._overview_page.mod_preview_delete_all_requested.connect(self._on_overview_preview_delete_all)
        self._overview_page.mod_preview_reorder_requested.connect(self._on_overview_preview_reorder)

        # Mod 更新
        self._overview_page.check_updates_requested.connect(lambda: self._on_check_mod_updates(force=True))

        # 图库页。
        self._gallery_page.mod_selected.connect(self._on_gallery_mod_selected)
        self._gallery_page.mod_toggle_requested.connect(self._on_gallery_mod_toggle)
        # 同步成人级Mod预览模式（画廊 ↔ 总览），并持久化到 config
        self._gallery_page.adult_mode_changed.connect(self._overview_page.set_adult_mode)
        self._overview_page.adult_mode_changed.connect(self._gallery_page.set_adult_mode)
        self._gallery_page.adult_mode_changed.connect(self._persist_adult_mode)
        self._overview_page.adult_mode_changed.connect(self._persist_adult_mode)

        # 设置页。
        self._settings_page.settings_changed.connect(self._on_settings_changed)
        self._settings_page.theme_changed.connect(self._on_theme_changed)
        self._settings_page.font_changed.connect(self._on_font_changed)
        self._settings_page.language_changed.connect(self._on_language_changed)
        self._settings_page.background_changed.connect(self._on_background_changed)
        self._settings_page.bg_overlay_refresh.connect(self._refresh_bg_overlay)
        self._settings_page.bg_playback_requested.connect(self._background_controller.playback)
        self._settings_page.bg_scrim_live.connect(self._on_bg_scrim_live)
        self._settings_page.bg_blur_live.connect(self._on_bg_blur_live)
        self._settings_page.app_update_check_requested.connect(self._on_app_update_check_requested)
        self._wire_settings_downloads(self._settings_page)

        # 工具页。
        self._tools_page.rebuild_index_requested.connect(self._on_rebuild_index)
        self._tools_page.view_logs_requested.connect(self._on_view_logs)
        self._tools_page.category_preset_import_requested.connect(
            lambda: self._open_category_presets(start_import=True)
        )
        self._tools_page.category_preset_export_requested.connect(
            lambda: self._open_category_presets(start_export=True)
        )
        self._tools_page.manage_plugins_requested.connect(lambda: self._select_nav_page(self.PAGE_PLUGINS))

        # 同时接收具体路径和通用变化信号，分别支持局部对账及范围未知时的检查。
        self._file_watcher.path_changed.connect(self._on_source_change)
        self._deploy_watcher.path_changed.connect(self._on_deployment_change)

        # 预设
        self._overview_page.preset_apply_requested.connect(self._on_preset_apply)
        self._overview_page.preset_rename_requested.connect(self._on_preset_rename)
        self._overview_page.preset_create_requested.connect(self._on_preset_create)
        self._overview_page.preset_delete_requested.connect(self._on_preset_delete_batch)
        self._overview_page.preset_duplicate_requested.connect(self._on_preset_duplicate_batch)
        self._mod_manager.preset_manager.preset_list_changed.connect(
            self._refresh_preset_combo,
        )
        self._mod_manager.preset_manager.preset_applied.connect(self._on_preset_applied)

    # 键盘快捷键

    def _setup_shortcuts(self) -> None:
        """创建窗口快捷键分发器，注册导入、刷新和页面上下文动作，并绑定 Escape 处理。"""
        from src.ui.window_shortcuts import install_window_shortcuts

        self._shortcut_dispatcher = ShortcutDispatcher(self._shortcut_settings, self)
        self._shortcut_dispatcher.register_window_action("mod.import", self._on_overview_import_requested)
        self._shortcut_dispatcher.register_window_action("view.refresh", lambda: self._refresh_current(manual=True))
        self._overview_page.install_shortcuts(self._shortcut_dispatcher)
        install_window_shortcuts(self, self._shortcut_dispatcher)
        QShortcut(QKeySequence("Escape"), self, self._on_escape)

    # 初始化数据

    def _init_data(self) -> None:
        """初始化游戏列表和角色数据"""
        # 索引自检须先于事务恢复、变体采集和后台对账，
        # 使损坏索引能够进入恢复流程，而不在读取事务日志时中断初始化。
        self._maybe_recover_index()
        index_ready = self._mod_manager.index_healthy
        # 获取可恢复的游戏列表
        games = self._game_service.get_game_list_for_ui()

        # 加载游戏：仅恢复上次「已激活」的游戏环境。无有效上次激活
        # （首次使用 / 该游戏被删 / 从未激活）→ 不自动激活；门禁锁住画廊/总览/商店，
        # 引导用户先到「游戏」页双击/启用一个游戏（激活是显式动作）。
        ids = [g["id"] for g in games]
        last_id = self._game_store.get_last_game_id()
        if index_ready and last_id and last_id in ids:
            target_game_id = last_id
            self._game_service.current_game_id = target_game_id
            self._game_store.set_last_game_id(target_game_id)
            self._mod_manager.set_game(target_game_id)
            self._apply_migoto_for_current_game()
            # 启动时先回滚未完成的预设事务，包括部署步骤和运行时变体修改，
            # 再允许新的预设切换与 Mod 启停。
            self._recover_pending_preset_applies(target_game_id)
            # 冷启动必须**先采集再清理部署记录**：软件关闭期间用户在游戏里改的变体只存在
            # d3dx_user.ini 里，先对账会让部署记录失效、归属索引认不出那些键，最终被旧
            # 快照覆盖。启动只采集当前文件，绝不主动写回历史快照。
            self._collect_startup_variants()
            # 启动时核对程序关闭期间的删除和新增，再将新增内容交给后台纳管。
            self._start_file_watcher(target_game_id)
            self._schedule_activation_reconcile(target_game_id, "startup")
            self._overview_page.set_current_game(target_game_id)
            self._load_characters(target_game_id)
            # 同步商店的目标游戏，网络加载延迟至商店首次可见。
            store = getattr(self, "_gb_store_page", None)
            if store is not None:
                store.prime_initial_game(target_game_id)
            # 初始化预设下拉框
            self._refresh_preset_combo()
        else:
            self._game_service.current_game_id = ""
            # 拒绝恢复或恢复失败时保留上次游戏配置，但不启动任何依赖索引的业务。
            if index_ready:
                self._apply_migoto_for_current_game()  # 无游戏→禁用启动按钮

        # 同步门禁 + 游戏卡片激活态 + 落点页
        self._apply_nav_gating()
        self._game_page.reload()
        self._select_nav_page(self.PAGE_OVERVIEW if self._game_service.has_active_game() else self.PAGE_GAME)

        # 启动后空闲触发一次后台更新检查（带节流）
        self._maybe_startup_update_check()

    def _load_characters(self, game_id: str, force_root: bool = False) -> None:
        """将当前游戏的分类加载到总览，无激活游戏时清空。

        底层空游戏编号表示查询全部数据，因此在界面入口拦截空值，避免显示其他游戏内容。
        """
        if not game_id or not self._game_service.has_active_game():
            self._overview_page.set_current_game("")
            self._overview_page.set_characters([], force_root=True)
            return
        self._overview_page.set_current_game(game_id)
        chars = self._mod_manager.get_characters_for_ui(game_id)
        self._overview_page.set_characters(chars, force_root=force_root)
        if force_root:
            # 分类卡片已经显示，后代记录只补充搜索数据，不重复重建根视图。
            self._overview_root_presented = True
            self._overview_refresh.schedule()
        self._overview_refresh.prefetch(
            game_id,
            [
                node["id"]
                for node in sorted(chars, key=lambda item: item.get("mod_count", 0), reverse=True)
                if node.get("mod_count", 0) >= 256
            ],
        )

    # Mod 自动更新接线

    def _maybe_startup_update_check(self) -> None:
        """启动后空闲触发一次后台检查（带 24h 节流），仅在开关开启时。"""
        from PySide6.QtCore import QTimer

        # 程序自身更新：启动后静默检查（管理器内部判总开关/启动开关/24h 节流）。
        # 用父子绑定的一次性定时器，避免裸 singleShot 在窗口早销毁后悬空触发。
        self._app_update_startup_timer = QTimer(self)
        self._app_update_startup_timer.setSingleShot(True)
        self._app_update_startup_timer.timeout.connect(
            lambda: getattr(self, "_app_update_manager", None) and self._app_update_manager.check_on_startup()
        )
        self._app_update_startup_timer.start(5000)

        if not self._config.get("gamebanana.enabled", True):
            return
        uc = "gamebanana.update_check"
        if not self._config.get(f"{uc}.enabled", True):
            return
        if not self._config.get(f"{uc}.on_startup", True):
            return
        # 父子绑定的一次性定时器，不能用裸 QTimer.singleShot：后者不归 window
        # 所有，window 在 3s 内被销毁后定时器仍挂在全局事件循环上，之后触发会调到
        # 已析构的 ModUpdateService（C++ 对象已删）→ RuntimeError。父子绑定后定时器
        # 随 window 一起销毁、不会悬空触发。与 _apply_periodic_update_timer 同一模式。
        self._startup_update_timer = QTimer(self)
        self._startup_update_timer.setSingleShot(True)
        self._startup_update_timer.timeout.connect(lambda: self._on_check_mod_updates(force=False))
        self._startup_update_timer.start(3000)

    def _connect_mod_update_service(self) -> None:
        """初始化更新通知状态，并订阅检测进度、任务变化和安装结果信号。"""
        svc = self._mod_update_service
        self._mod_update_background_games: set[str] = set()
        self._mod_update_progress = None
        svc.status_changed.connect(self._on_update_status_changed)
        svc.check_started.connect(self._on_update_check_started)
        svc.progress.connect(self._on_update_check_progress)
        svc.check_finished.connect(self._on_update_check_finished)
        svc.update_task_changed.connect(self._on_update_task_changed)
        svc.update_applied.connect(self._on_update_applied)
        svc.update_failed.connect(self._on_update_failed)
        svc.update_cancelled.connect(self._on_update_cancelled)
        svc.update_deferred.connect(self._on_update_deferred)

    def _on_check_mod_updates(self, *, force: bool, only_failed: bool = False) -> None:
        """启动或接续更新检测，手动请求打开更新中心；调度拒绝时恢复此前检测状态。"""
        svc = getattr(self, "_mod_update_service", None)
        if svc is None:
            return
        was_manual = getattr(self, "_update_check_is_manual", False)
        if svc.is_checking():
            if force:
                self._update_check_is_manual = True
                self._open_mod_updates_center()
            return
        self._update_check_is_manual = force
        if force:
            # 先显示窗口，再收集任务或等待网络，避免嵌套事件循环。
            self._open_mod_updates_center()
            self._mod_updates_dialog.set_checking(True)
        schedule = getattr(svc, "schedule_check_updates", svc.check_updates)
        if schedule(force=force, only_failed=only_failed) is False:
            self._update_check_is_manual = was_manual
            dlg = getattr(self, "_mod_updates_dialog", None)
            if dlg is not None:
                dlg.set_check_snapshot(svc.check_snapshot())

    # 程序自身更新接线

    def _on_app_update_check_requested(self) -> None:
        """设置页「立即检查程序更新」——非静默，会反馈「已是最新/失败」。"""
        mgr = getattr(self, "_app_update_manager", None)
        if mgr is not None:
            mgr.check_for_updates(silent=False)

    def _apply_periodic_update_timer(self) -> None:
        """按 periodic_hours 设置启停后台定时检查（force=False，带节流）。"""
        from PySide6.QtCore import QTimer

        hours = int(self._config.get("gamebanana.update_check.periodic_hours", 0))
        if not hasattr(self, "_periodic_update_timer"):
            self._periodic_update_timer = QTimer(self)
            self._periodic_update_timer.timeout.connect(lambda: self._on_check_mod_updates(force=False))
        self._periodic_update_timer.stop()
        if hours > 0 and self._config.get("gamebanana.update_check.enabled", True):
            self._periodic_update_timer.start(hours * 3600 * 1000)

    def _on_update_check_started(self, total: int) -> None:
        """初始化检测进度并同步更新中心快照，有检测对象时显示总览状态文字。"""
        self._update_check_progress = (0, total)
        dlg = getattr(self, "_mod_updates_dialog", None)
        if dlg is not None:
            dlg.set_check_snapshot(self._mod_update_service.check_snapshot())
        if total > 0:
            self._overview_page.set_update_status_text(tr("正在检查 {total} 个 Mod 的更新…", total=total))

    def _on_update_check_progress(self, done: int, total: int) -> None:
        """保存当前检测完成数与总数，并把服务快照同步到已打开的更新中心。"""
        self._update_check_progress = (done, total)
        dlg = getattr(self, "_mod_updates_dialog", None)
        if dlg is not None:
            dlg.set_check_snapshot(self._mod_update_service.check_snapshot())

    def _on_update_check_finished(self, counts: dict) -> None:
        """依据检测结果和通知设置更新状态文字，并将已完成结果、失败项和快照交给更新中心。"""
        found = counts.get("found", 0)
        failed = counts.get("failed", 0)
        snapshot = self._mod_update_service.check_snapshot()
        # 后台徽章模式只更新标记；手动检查或徽章加提示模式同时显示状态文字。
        toast = self._config.get("gamebanana.update_check.notify_style", "badge") == "badge_toast"
        manual = getattr(self, "_update_check_is_manual", False)
        self._update_check_is_manual = False
        show_text = toast or manual
        if counts.get("error"):
            if show_text:
                self._overview_page.set_update_status_text(tr("检查准备失败：{value}", value=counts["error"]))
        elif counts.get("cancelled"):
            if show_text:
                self._overview_page.set_update_status_text(tr("已取消检测，上次检测结果已保留"))
        elif found > 0:
            if show_text:
                self._overview_page.set_update_status_text(tr("{found} 个 Mod 有可用更新", found=found))
        elif failed:
            if show_text:
                self._overview_page.set_update_status_text(tr("{failed} 个 Mod 检查失败，请重试", failed=failed))
        else:
            if show_text:
                self._overview_page.set_update_status_text(
                    tr("都是最新版") if snapshot.get("total", 0) else tr("暂无可检测的 Mod")
                )
        dlg = getattr(self, "_mod_updates_dialog", None)
        if dlg is not None:
            svc = self._mod_update_service
            dlg.set_updates(list(svc.get_update_status(completed_only=True).values()), failures=svc.failures())
            dlg.set_check_snapshot(snapshot)

    def _on_update_status_changed(self) -> None:
        """同步总览更新标记和后台通知；更新中心游戏上下文变化时重建对应内容。"""
        svc = self._mod_update_service
        ids = svc.updatable_ids()
        self._overview_page.set_updatable(ids)
        self._set_overview_update_count(len(ids))
        self._sync_mod_update_notification()
        dlg = getattr(self, "_mod_updates_dialog", None)
        if dlg is not None:
            if dlg.property("gameId") != self._mod_manager.current_game_id:
                dlg.force_close()
                self._open_mod_updates_center()
                return
            snapshot = svc.check_snapshot()
            if not snapshot["checking"]:
                dlg.set_updates(list(svc.get_update_status(completed_only=True).values()), failures=svc.failures())
            dlg.set_check_snapshot(snapshot)

    def _set_overview_update_count(self, n: int) -> None:
        """在导航「总览」项文字后挂一个计数（无现成 badge 机制时的最简实现）。"""
        for i, (icon_name, label, page_idx) in enumerate(self._nav_items_data):
            if page_idx == self.PAGE_OVERVIEW:
                item = self._nav.item(i)
                if item is None:
                    return
                if getattr(self, "_nav_collapsed", False):
                    item.setText("")  # 折叠态只显示图标
                    return
                base = item.data(Qt.ItemDataRole.UserRole + 2) or label
                item.setText(f"{base}  ({n})" if n > 0 else base)
                return

    def _open_mod_updates_center(self) -> None:
        """复用当前游戏的更新中心，合并可用更新与任务快照，恢复后台任务的可见操作入口。"""
        background_games = getattr(self, "_mod_update_background_games", set())
        background_games.discard(self._mod_manager.current_game_id)
        panel = getattr(self, "_mod_update_progress", None)
        if panel is not None:
            panel.dismiss()
        existing = getattr(self, "_mod_updates_dialog", None)
        if existing is not None:
            if existing.property("gameId") == self._mod_manager.current_game_id:
                existing.raise_()
                existing.activateWindow()
                return
            existing.force_close()
        svc = self._mod_update_service
        available = svc.get_update_status(completed_only=True)
        task_infos = svc.update_task_infos()
        infos = {**task_infos, **available}
        dlg = getattr(self, "_mod_updates_cached_dialog", None)
        # 关闭确认中可能切换游戏，旧对话框保留到其关闭处理返回。
        if dlg is not None and dlg.property("gameId") not in (None, "", self._mod_manager.current_game_id):
            dlg.deleteLater()
            dlg = None
        if dlg is None:
            dlg = MainWindow._create_mod_updates_dialog(self)
            self._mod_updates_cached_dialog = dlg
        dlg.setProperty("gameId", self._mod_manager.current_game_id)
        self._mod_updates_dialog = dlg
        dlg.prepare_for_open(list(infos.values()), failures=svc.failures())
        dlg.set_check_snapshot(svc.check_snapshot())
        active = set(svc.active_update_ids())
        for mid, snapshot in svc.update_task_snapshots().items():
            # 完成任务后可能发现更新的文件版本，保留新操作入口，不恢复旧历史状态。
            if (
                mid not in active
                and mid in available
                and mid in task_infos
                and available[mid].latest_file.file_id != task_infos[mid].latest_file.file_id
            ):
                continue
            dlg.set_task_snapshot(mid, snapshot)
        dlg.open()

    def _create_mod_updates_dialog(self):
        """创建更新中心并连接检测、忽略、安装和暂停停止动作，设置关闭决策回调。"""
        from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog

        svc = self._mod_update_service
        dlg = ModUpdatesDialog([], enable_actions=True, config=self._config, parent=self)
        dlg.set_close_handler(lambda: self._request_mod_updates_close(dlg))
        dlg.recheck_requested.connect(lambda: self._on_check_mod_updates(force=True))
        dlg.retry_failed_requested.connect(lambda: self._on_check_mod_updates(force=True, only_failed=True))
        dlg.cancel_check_requested.connect(svc.cancel_check)
        dlg.ignore_version_requested.connect(lambda mid: self._on_ignore_mod_update(mid, version=True))
        dlg.ignore_mod_requested.connect(lambda mid: self._on_ignore_mod_update(mid, version=False))
        dlg.open_gb_requested.connect(self._open_gb_for_local_mod)
        dlg.update_requested.connect(self._on_apply_update)
        dlg.update_all_requested.connect(self._on_update_all)
        dlg.pause_update_requested.connect(svc.pause_update)
        dlg.resume_update_requested.connect(svc.resume_update)
        dlg.stop_update_requested.connect(self._on_stop_mod_updates)
        dlg.pause_all_requested.connect(svc.pause_all_updates)
        dlg.resume_all_requested.connect(svc.resume_all_updates)
        dlg.stop_all_requested.connect(self._on_stop_mod_updates)

        def dismissed(_result: int) -> None:
            """仅在关闭的是当前更新中心时清除活动对话框引用。"""
            if getattr(self, "_mod_updates_dialog", None) is dlg:
                self._mod_updates_dialog = None

        dlg.finished.connect(dismissed)
        return dlg

    def _on_ignore_mod_update(self, local_mod_id: str, *, version: bool) -> None:
        """忽略指定版本或整个 Mod，并在更新中心提供可撤销提示。"""
        svc = self._mod_update_service
        token = svc.ignore_version(local_mod_id) if version else svc.ignore_mod(local_mod_id)
        dlg = getattr(self, "_mod_updates_dialog", None)
        if token is not None and dlg is not None:
            text = tr("已忽略此版本，发布新版本时仍会提醒。") if version else tr("已忽略此 Mod 的更新。")
            dlg.show_message(text, undo=lambda: svc.undo_ignore(token))

    def _open_gb_for_local_mod(self, local_mod_id: str) -> None:
        """读取本地 Mod 的 GameBanana 来源标识，并在浏览器打开来源页面。"""
        mod = self._mod_manager.get_mod(local_mod_id)
        if not mod:
            return
        gb_id = self._extract_gb_mod_id(mod)
        if gb_id:
            import webbrowser

            url = ((mod.info.custom_data or {}).get("gamebanana") or {}).get(
                "page_url"
            ) or f"https://gamebanana.com/mods/{gb_id}"
            webbrowser.open(url)

    @_invalidates_library_probe
    def _on_apply_update(self, local_mod_id: str) -> None:
        """为未运行的单项更新按需选择候选文件，然后转交统一更新启动入口。"""
        svc = self._mod_update_service
        if local_mod_id in svc.active_update_ids():
            return
        info = svc.get_update_status().get(local_mod_id)
        if info is None:
            return
        chosen = None
        # 多文件变体 → 弹选择器（场景：丰满版/标准版，不能闭眼下第一个）
        if info is not None and len(info.candidate_files) > 1:
            from src.ui.dialogs.mod_update_file_dialog import ModUpdateFileDialog

            mod = self._mod_manager.get_mod(local_mod_id)
            original = mod.folder_name if mod else ""
            picker = ModUpdateFileDialog(
                info.candidate_files,
                local_original_name=original,
                parent=getattr(self, "_mod_updates_dialog", None) or self,
            )
            if not picker.exec():
                return
            chosen = picker.selected_file()
        self._start_mod_update(local_mod_id, chosen_file=chosen)

    def _start_mod_update(self, local_mod_id: str, *, chosen_file=None) -> None:
        """检查插件、部署及更新冲突后启动单项更新，正在部署的 Mod 给出延后提示。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        svc = self._mod_update_service
        if local_mod_id in svc.active_update_ids() or local_mod_id not in svc.updatable_ids():
            return
        # 完成信号排队和最终发布期间继续持锁，不能只按线程运行状态判断。
        if local_mod_id in self._deploy_workers or local_mod_id in self._deploy_active:
            self._ensure_toast().done(tr("该 Mod 正在部署，请在部署完成后更新。"), ok=False, hold_ms=2500)
            return
        self._overview_page.set_update_status_text(tr("正在更新…"))
        svc.apply_update(local_mod_id, chosen_file=chosen_file)

    def _on_update_all(self) -> None:
        """收集待更新条目并统一确认多文件候选，重新核验游戏和候选有效性后逐项启动。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        svc = self._mod_update_service
        dlg = getattr(self, "_mod_updates_dialog", None)
        pending = dlg.pending_ids() if dlg is not None else list(svc.updatable_ids())
        available = svc.get_update_status()
        active = set(svc.active_update_ids())
        infos = [available[mid] for mid in pending if mid in available and mid not in active]
        if not infos:
            return
        game_id = self._mod_manager.current_game_id
        chosen = {}
        if any(len(info.candidate_files) > 1 for info in infos):
            from src.ui.dialogs.mod_update_file_dialog import ModUpdateBatchFileDialog

            original_names = {}
            for info in infos:
                mod = self._mod_manager.get_mod(info.local_mod_id)
                original_names[info.local_mod_id] = mod.folder_name if mod is not None else ""
            picker = ModUpdateBatchFileDialog(infos, local_original_names=original_names, parent=dlg or self)
            if not picker.exec():
                return
            chosen = picker.selected_files()
        # 选择文件期间可能刷新或切换游戏，不能将旧选择用于新版本，也不能启动不完整批次。
        if game_id != self._mod_manager.current_game_id:
            return
        current = svc.get_update_status()
        for mid, file in chosen.items():
            info = current.get(mid)
            candidates = (info.candidate_files or [info.latest_file]) if info is not None else []
            if file not in candidates:
                self._ensure_toast().done(tr("可用更新已变化，请重新确认更新文件。"), ok=False, hold_ms=3500)
                return
        for info in infos:
            self._start_mod_update(info.local_mod_id, chosen_file=chosen.get(info.local_mod_id))

    def _request_mod_updates_close(self, dlg) -> bool:
        """依据活动任务决定阻止关闭或转入后台；批次完成时确认结果并清除总览状态文字。"""
        svc = self._mod_update_service
        game_id = self._mod_manager.current_game_id
        if dlg.property("gameId") != game_id:
            return True
        if svc.active_update_ids():
            from src.ui.mod_update_actions import confirm_close_updates

            choice = confirm_close_updates(dlg, svc)
            if game_id != self._mod_manager.current_game_id:
                return True
            if choice == "back":
                return False
            if choice == "background":
                self._mod_update_background_games.add(self._mod_manager.current_game_id)
                self._sync_mod_update_notification()
            return True
        if dlg.is_batch_complete() and svc.acknowledge_completed_updates():
            self._overview_page.set_update_status_text("")
        return True

    def _ensure_mod_update_notification(self):
        """创建或复用后台更新通知面板，并连接查看、隐藏、暂停、继续与停止动作。"""
        panel = getattr(self, "_mod_update_progress", None)
        if panel is None:
            from src.ui.widgets.mod_update_notification import ModUpdateNotification

            if getattr(self, "_panel_stack", None) is None:
                self._panel_stack = TaskCenterStack()
            panel = ModUpdateNotification(self._stack, stack=self._panel_stack)
            self._mod_update_progress = panel
            panel.view_requested.connect(self._open_mod_updates_center)
            panel.dismiss_requested.connect(self._dismiss_mod_update_notification)
            panel.pause_requested.connect(lambda: self._control_mod_update_notification("pause"))
            panel.resume_requested.connect(lambda: self._control_mod_update_notification("resume"))
            panel.stop_requested.connect(lambda: self._control_mod_update_notification("stop"))
        return panel

    def _sync_mod_update_notification(self) -> None:
        """仅为当前游戏已转入后台的更新呈现任务快照，无快照或上下文不匹配时收起通知。"""
        game_id = self._mod_manager.current_game_id
        panel = getattr(self, "_mod_update_progress", None)
        if game_id not in getattr(self, "_mod_update_background_games", set()):
            if panel is not None and panel.isVisible() and panel.game_id != game_id:
                panel.dismiss(animate=False)
            return
        snapshots = self._mod_update_service.update_task_snapshots()
        if not snapshots:
            if panel is not None:
                panel.dismiss()
            return
        infos = self._mod_update_service.update_task_infos()
        names = {}
        for mid in snapshots:
            info = infos.get(mid)
            mod = self._mod_manager.get_mod(mid)
            names[mid] = (
                info.local_name
                if info is not None and info.local_name
                else mod.display_name
                if mod is not None
                else mid
            )
        self._ensure_mod_update_notification().present(game_id, snapshots, names)

    def _control_mod_update_notification(self, action: str) -> None:
        """校验通知所属游戏后执行全部暂停、继续或停止，再刷新通知状态。"""
        panel = getattr(self, "_mod_update_progress", None)
        if panel is None or panel.game_id != self._mod_manager.current_game_id:
            return
        svc = self._mod_update_service
        if action == "pause":
            svc.pause_all_updates()
        elif action == "resume":
            svc.resume_all_updates()
        elif action == "stop":
            self._on_stop_mod_updates()
        self._sync_mod_update_notification()

    def _dismiss_mod_update_notification(self) -> None:
        """仅在当前游戏更新全部结束后确认批次结果并收起后台通知。"""
        panel = getattr(self, "_mod_update_progress", None)
        svc = self._mod_update_service
        if panel is None or panel.game_id != self._mod_manager.current_game_id or svc.active_update_ids():
            return
        self._mod_update_background_games.discard(panel.game_id)
        if svc.acknowledge_completed_updates():
            self._overview_page.set_update_status_text("")
        panel.dismiss()

    def _on_stop_mod_updates(self, local_mod_id: str | None = None) -> None:
        """为单个或全部更新打开停止确认，确认动作由更新服务处理。"""
        from src.ui.mod_update_actions import confirm_stop_updates

        confirm_stop_updates(
            getattr(self, "_mod_updates_dialog", None) or self,
            self._mod_update_service,
            [local_mod_id] if local_mod_id is not None else None,
        )

    def _is_current_mod_update(self, local_mod_id: str) -> bool:
        """检查 Mod 是否仍存在于当前游戏的任务、更新结果或本地库中。"""
        svc = self._mod_update_service
        return (
            local_mod_id in svc.update_task_snapshots()
            or local_mod_id in svc.get_update_status()
            or any(mod.mod_id == local_mod_id for mod in self._mod_manager.get_mods())
        )

    def _restore_mod_update_task(self, local_mod_id: str) -> dict | None:
        """读取任务快照并同步给当前游戏的更新中心，返回快照供结果处理复用。"""
        snapshot = self._mod_update_service.update_task_snapshots().get(local_mod_id)
        dlg = getattr(self, "_mod_updates_dialog", None)
        if snapshot is not None and dlg is not None and dlg.property("gameId") == self._mod_manager.current_game_id:
            dlg.set_task_snapshot(local_mod_id, snapshot)
        return snapshot

    @_invalidates_library_probe
    def _on_update_task_changed(self, local_mod_id: str, snapshot: dict) -> None:
        """仅处理当前游戏的任务变化，同步更新中心与后台通知并使库探测失效。"""
        if snapshot.get("game_id") != self._mod_manager.current_game_id:
            return
        # 排队信号可能早于暂停、重试或游戏切换。
        self._restore_mod_update_task(local_mod_id)
        self._sync_mod_update_notification()

    @_invalidates_library_probe
    def _on_update_applied(self, local_mod_id: str, mod) -> None:
        """为当前 Mod 恢复更新快照、显示完成状态，并尝试局部更新总览条目。"""
        if not self._is_current_mod_update(local_mod_id):
            return
        self._restore_mod_update_task(local_mod_id)
        self._overview_page.set_update_status_text(tr("更新完成"))
        # 刷新该卡片：去角标 + 同步新内容/版本（复用现有就地刷新）
        if hasattr(self._overview_page, "update_mod_in_view"):
            try:
                self._overview_page.update_mod_in_view(mod)
            except Exception:
                pass
        # status_changed 已由 service 发出 → 角标/计数自动刷新

    @_invalidates_library_probe
    def _on_update_failed(self, local_mod_id: str, err: str) -> None:
        """为当前 Mod 显示最新失败快照或错误文案，并记录更新失败日志。"""
        if not self._is_current_mod_update(local_mod_id):
            return
        snapshot = self._restore_mod_update_task(local_mod_id)
        dlg = getattr(self, "_mod_updates_dialog", None)
        if snapshot is None and dlg is not None and dlg.property("gameId") == self._mod_manager.current_game_id:
            dlg.set_update_failed(local_mod_id, err)
        self._overview_page.set_update_status_text(tr("更新失败：{err}", err=err))
        logger.warning("Mod 更新失败 %s: %s", local_mod_id, err)

    @_invalidates_library_probe
    def _on_update_cancelled(self, local_mod_id: str) -> None:
        """同步已取消任务快照，并提示更新终止且保留旧版内容。"""
        if not self._is_current_mod_update(local_mod_id):
            return
        self._restore_mod_update_task(local_mod_id)
        self._overview_page.set_update_status_text(tr("已终止更新，旧版 Mod 已保留"))

    @_invalidates_library_probe
    def _on_update_deferred(self, local_mod_id: str) -> None:
        """恢复当前任务状态，并提示更新文件已保留、需返回原游戏后重试。"""
        if not self._is_current_mod_update(local_mod_id):
            return
        self._restore_mod_update_task(local_mod_id)
        mod = self._mod_manager.get_mod(local_mod_id)
        name = mod.display_name if mod is not None else "Mod"
        self._ensure_toast().done(
            tr("{name} 的更新文件已保留，请切回原游戏后重试。", name=name), ok=False, hold_ms=5000
        )

    # 导航

    def _on_nav_splitter_moved(self, pos: int, index: int) -> None:
        """拖动导航分隔条时保存有效的展开宽度，折叠状态不写入。"""
        if self._nav_collapsed or self._nav_animating:
            return
        sizes = self._main_splitter.sizes()
        if sizes and sizes[0] > 60:  # >60 视作真实展开宽度（排除折叠态的 52）
            self._nav_expanded_width = int(sizes[0])
            Config.instance().set("ui.nav_expanded_width", int(sizes[0]))

    def _toggle_nav_collapse(self) -> None:
        """折叠/展开导航栏（宽度平滑过渡）"""
        self._nav_collapsed = not self._nav_collapsed
        # 动画起点 = 当前实际宽度（即便上一轮过渡未完，width 也是当前帧值 → 平滑接力）
        start_w = self._nav_container.width()
        # 折叠态下让导航项/启动按钮的 SVG 图标水平居中（QSS 据 collapsed 属性切换对齐）
        for w in (self._nav, self._launch_loader_btn, self._launch_game_btn, self._about_btn, self._plugin_cmd_btn):
            w.setProperty("collapsed", self._nav_collapsed)
            w.style().unpolish(w)
            w.style().polish(w)
        # 导航项的图标居中由自绘代理负责（QSS 的 padding 管不到样式自带的图标边距）
        self._nav_delegate.collapsed = self._nav_collapsed
        self._nav.viewport().update()
        if self._nav_collapsed:
            # 记录展开宽度以便恢复
            if not self._nav_animating:
                self._nav_expanded_width = start_w
                Config.instance().set("ui.nav_expanded_width", int(start_w))
            self._nav_animating = True
            # 只显示图标（清空文字，SVG 图标保留）
            for i in range(self._nav.count()):
                item = self._nav.item(i)
                item.setText("")
                # 34 = 52(折叠宽) - 1(右边框) - 12(QSS padding) - 4(spacing)：让行盒放得进
                # 视口，hover/选中的圆角底块才不会被右边缘裁成平口（图标居中另由代理保证）
                item.setSizeHint(QSize(34, 40))
            # 底部启动按钮同步只显示图标
            for btn, _full, _icon in self._launch_buttons:
                btn.setText("")
            icons.apply_icon(self._collapse_btn, "actions/chevron-right")

            def _after_collapse() -> None:
                """在折叠动画结束后固定导航占用宽度，并解除动画状态。"""
                self._nav_animating = False
                # 收起后把余量给右侧栈（动画期间 min/max 已驱动宽度，这里回退处理一次）
                self._main_splitter.setSizes([52, max(0, self._main_splitter.width() - 52)])

            # 宽度 start_w → 52 平滑过渡（同时动 min/max 以驱动 QSplitter 内的子控件）
            anim.animate_fixed_width(self._nav_container, start_w, 52, on_finished=_after_collapse)
        else:
            # 恢复完整文本
            for i in range(self._nav.count()):
                item = self._nav.item(i)
                full_text = item.data(Qt.ItemDataRole.UserRole + 2) or ""
                item.setText(full_text)
                item.setSizeHint(QSize(140, 40))
            # 底部启动按钮恢复完整文本
            for btn, full, _icon in self._launch_buttons:
                btn.setText(full)
            icons.apply_icon(self._collapse_btn, "actions/menu")
            target = max(self._nav_expanded_width, self._nav_container.minimumSizeHint().width())
            self._nav_animating = True

            def _after_expand() -> None:
                # 展开完成后恢复原尺寸约束，让文字和按钮决定拖动下限。
                """在展开动画结束后恢复导航可调宽度，并提交目标分隔比例。"""
                self._nav_container.setMinimumWidth(0)
                self._nav_container.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX
                self._main_splitter.setSizes([target, max(0, self._main_splitter.width() - target)])
                self._nav_animating = False

            # 宽度 52 → 展开宽度 平滑过渡
            anim.animate_fixed_width(self._nav_container, start_w, target, on_finished=_after_expand)
        # 重设导航文字后，恢复总览更新计数
        if hasattr(self, "_mod_update_service"):
            self._set_overview_update_count(len(self._mod_update_service.updatable_ids()))
        # 折叠/展开后导航项尺寸变化 → 重定位选中指示条（不动画，跟随宽度过渡）
        self._update_nav_indicator(animate=False)

    def _select_nav_page(self, page_index: int) -> None:
        """选中导航中映射到 page_index 的行。"""
        for i in range(self._nav.count()):
            if self._nav.item(i).data(Qt.ItemDataRole.UserRole) == page_index:
                self._nav.setCurrentRow(i)
                return

    def _restore_nav_selection(self, page_index: int) -> None:
        """取消切页时把导航选中态静默恢复到当前页面，避免递归触发切页。"""
        self._nav.blockSignals(True)
        try:
            for i in range(self._nav.count()):
                if self._nav.item(i).data(Qt.ItemDataRole.UserRole) == page_index:
                    self._nav.setCurrentRow(i)
                    break
        finally:
            self._nav.blockSignals(False)
        self._update_nav_indicator(animate=False)

    def _apply_nav_gating(self) -> None:
        """同步「无激活游戏」状态：刷新启动按钮 + 内容区提示遮罩。

        无激活游戏时仍允许进入画廊、总览和 Mod 商店，由 _update_lock_overlay
        显示激活提示，引导用户前往游戏页。游戏、设置和工具页始终可用且无遮罩。
        """
        if hasattr(self, "_launch_loader_btn"):
            self._update_launch_buttons()
        self._update_lock_overlay()

    def _raise_task_center(self) -> None:
        """切页或显示锁定遮罩后，将任务中心浮层提升至同级栈顶。

        导入和导出面板可同时存在，由堆叠器统一调整层级和位置。
        """
        stack = getattr(self, "_panel_stack", None)
        if stack is None:
            return
        try:
            stack.raise_all()
            stack.relayout()
        except RuntimeError:
            pass

    def _on_stack_page_changed(self, _index: int) -> None:
        """切页后重排浮层 Z 序：立刻抬一次 + 过渡动画结束后再抬一次。"""
        self._raise_task_center()
        call_later(220, self, self._raise_task_center)

    def _update_lock_overlay(self) -> None:
        """无激活游戏且当前在 总览/画廊/商店 时显示提示遮罩；否则隐藏。"""
        overlay = getattr(self, "_lock_overlay", None)
        if overlay is None:
            return
        gated = {self.PAGE_OVERVIEW, self.PAGE_GALLERY, self.PAGE_STORE}
        show = not self._game_service.has_active_game() and self._stack.currentIndex() in gated
        if show:
            was_hidden = not overlay.isVisible()
            overlay.setGeometry(self._stack.rect())
            overlay.raise_()
            overlay.show()
            # 仅在「隐藏→显示」时淡入；重复调用（切页/缩放）不重复闪动
            if was_hidden:
                anim.fade_in(overlay)
        else:
            overlay.hide()
        # 遮罩刚 raise 过 → 任务中心必须再抬一次，否则「请先激活」会盖住进度浮层
        self._raise_task_center()

    def eventFilter(self, obj, event):
        # 注意：nav viewport 的过滤器在 _setup_ui 早段就装上了，此时 _stack 尚未创建，
        # 故用 getattr 守卫，避免构造期 viewport resize 事件触发 AttributeError。
        """随页面栈和导航视口缩放同步锁定层、任务浮层与导航指示条的位置。"""
        if event.type() == QEvent.Type.Resize:
            stack = getattr(self, "_stack", None)
            if stack is not None and obj is stack:
                # _stack 改变大小时同步提示遮罩几何，使其始终铺满内容区。
                overlay = getattr(self, "_lock_overlay", None)
                if overlay is not None and overlay.isVisible():
                    overlay.setGeometry(stack.rect())
                # 任务中心是 absolute 定位的浮层（不进布局），内容区改尺寸后
                # 必须重算右下角锚点，否则窗口缩小会把它留在视口外。多张浮层由
                # 堆叠器统一重排（每张的 y 偏移取决于它下面那几张的高度）。
                panel_stack = getattr(self, "_panel_stack", None)
                if panel_stack is not None:
                    panel_stack.relayout(animate=False)
            elif hasattr(self, "_nav") and obj is self._nav.viewport():
                # 导航 viewport 尺寸变化（首次布局/折叠/缩放）→ 重定位选中指示条
                self._update_nav_indicator(animate=False)
        return super().eventFilter(obj, event)

    def _update_nav_indicator(self, animate: bool = True) -> None:
        """把选中指示条定位/滑动到当前导航项左侧（Fluent 竖条）。

        几何用 ``visualItemRect``（viewport 坐标）；首次显示直接落位，后续选中变化
        用几何动画平滑滑动（``animate=True``）。无选中时隐藏。
        """
        ind = getattr(self, "_nav_indicator", None)
        if ind is None:
            return
        # 直接定位、隐藏或返回当前位置时也须取消旧目标动画。
        previous = getattr(ind, "_geom_anim", None)
        if previous is not None:
            try:
                previous.stop()
            except RuntimeError:
                pass
            ind._geom_anim = None
        row = self._nav.currentRow()
        item = self._nav.item(row) if row >= 0 else None
        if item is None:
            ind.hide()
            return
        rect = self._nav.visualItemRect(item)
        if rect.height() <= 0:
            return
        bar_h = max(14, int(rect.height() * 0.55))
        y = rect.y() + (rect.height() - bar_h) // 2
        target = QRect(3, y, 3, bar_h)
        if not ind.isVisible():
            ind.setGeometry(target)
            ind.show()
            ind.raise_()
            return
        if animate:
            anim.animate_geometry(ind, target)
        else:
            ind.setGeometry(target)
        ind.raise_()

    def _clear_game_views(self) -> None:
        """取消激活/无激活游戏时清空 总览/画廊/预设/商店 的残留数据。

        页面显示空状态并叠加激活提示，清除上个游戏的角色树、Mod 和预设。
        数据源已由 _mod_manager.set_game("") 复位，后续 F5、切页和 watcher 刷新均获取空数据。
        """
        try:
            self._file_watcher.stop()
            self._deploy_watcher.stop()
            self._deploy_state_poll.stop()
            self._source_state_poll.stop()
        except Exception:
            pass
        ov = self._overview_page
        ov.set_current_game("")
        ov._selected_mod = None
        ov._selected_category = None
        ov._selected_ids.clear()
        ov.set_characters([], force_root=True)
        ov.set_current_mods([], [])
        ov.set_presets([], "")
        ov._hide_panel()
        self._gallery_refresh.cancel()
        self._gallery_page.set_mods([])
        # Mod 商店：隐藏详情浮窗 + 列表回到空态（安全调用，失败不影响主流程）
        store = getattr(self, "_gb_store_page", None)
        if store is not None:
            try:
                # 清空网格、_all_records 和 _gb_game_id，避免 show_grid 重新显示上个游戏的卡片。
                store.reset_to_empty()
            except Exception:
                pass

    def _on_nav_changed(self, row: int) -> None:
        """校验游戏页待保存设置后切换导航页面，补做延迟主题更新并同步锁定覆盖层。"""
        if row < 0:
            return
        item = self._nav.item(row)
        if not item:
            return
        # 切到顶部导航项 → 取消底部「关于」按钮的选中态
        page_index = item.data(Qt.ItemDataRole.UserRole)
        current_page = self._stack.currentIndex()
        if (
            current_page == self.PAGE_GAME
            and page_index != self.PAGE_GAME
            and not self._game_page.confirm_pending_changes()
        ):
            self._restore_nav_selection(self.PAGE_GAME)
            return
        self._set_about_current(False)
        # 显示前更新失效的主题样式。
        pending = getattr(self, "_pending_retheme", None)
        if pending and page_index in pending:
            pending.discard(page_index)
            self._retheme_one_page(page_index)
        if page_index == self.PAGE_GALLERY:
            self._refresh_gallery()
        # 页面同步切换，旧页快照淡出以展示新页；动画不参与页面状态更新。
        anim.cross_fade(self._stack, lambda: self._stack.setCurrentIndex(page_index))
        # 选中指示条滑到新菜单项（菜单项之间的过渡动画）
        self._update_nav_indicator(animate=True)
        self._update_lock_overlay()

    def _on_about_clicked(self) -> None:
        """底部「关于」按钮：切到关于页，清掉顶部导航选中态。"""
        if self._stack.currentIndex() == self.PAGE_GAME and not self._game_page.confirm_pending_changes():
            return
        self._set_about_current(True)
        # 清掉顶部列表选中（setCurrentRow(-1) 触发 _on_nav_changed(row<0) 直接返回）
        self._nav.blockSignals(True)
        self._nav.setCurrentRow(-1)
        self._nav.blockSignals(False)
        self._update_nav_indicator(animate=False)  # 无选中 → 指示条隐藏
        # 显示前按需刷新主题样式。
        pending = getattr(self, "_pending_retheme", None)
        if pending and self.PAGE_ABOUT in pending:
            pending.discard(self.PAGE_ABOUT)
            self._retheme_one_page(self.PAGE_ABOUT)
        anim.cross_fade(self._stack, lambda: self._stack.setCurrentIndex(self.PAGE_ABOUT))
        self._about_page.play_intro()  # 分区交错淡入
        self._update_lock_overlay()

    def _set_about_current(self, on: bool) -> None:
        """切换底部「关于」按钮的选中态（QSS current 属性）。"""
        btn = getattr(self, "_about_btn", None)
        if btn is None:
            return
        if bool(btn.property("current")) == bool(on):
            return
        btn.setProperty("current", bool(on))
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def switch_to_mod_store_with_query(self, query: str) -> None:
        """切换至 Mod 商店并预填搜索词，供类别页入口调用。"""
        if self._stack.currentIndex() == self.PAGE_GAME and not self._game_page.confirm_pending_changes():
            return
        self._stack.setCurrentIndex(self.PAGE_STORE)
        # 同步 nav 选中状态
        for i in range(self._nav.count()):
            if self._nav.item(i).data(Qt.ItemDataRole.UserRole) == self.PAGE_STORE:
                self._nav.setCurrentRow(i)
                break
        # 预填搜索词
        if query and hasattr(self._gb_store_page, "filter_bar"):
            self._gb_store_page.filter_bar.search_box.setText(query)
            # 立即执行搜索，不等待输入防抖。
            if hasattr(self._gb_store_page, "_on_search"):
                self._gb_store_page._on_search(query)

    def _on_escape(self) -> None:
        # 先让 OverviewPage 处理 Esc（取消剪切等），再处理导航回退。
        """在总览存在剪切选择时取消剪切状态。"""
        if self._stack.currentIndex() == self.PAGE_OVERVIEW:
            if self._overview_page._clipboard_cat_ids or self._overview_page._clipboard_mod_ids:
                self._overview_page._cancel_cut()
                return

    # 游戏切换

    def _open_game_settings(self) -> None:
        """通过导航项的 UserRole，从商店的未收录游戏提示跳转到游戏管理页。"""
        self._select_nav_page(self.PAGE_GAME)

    def _open_local_mod_in_overview(self, local_mod_id: str) -> None:
        """定位下载历史中选定的实际安装文件。"""
        mod = self._mod_manager.get_mod(local_mod_id)
        if mod is None or not Path(mod.path).exists():
            return
        self._select_nav_page(self.PAGE_OVERVIEW)
        if mod.node_id and hasattr(self._overview_page, "_navigate_to"):
            self._overview_page._navigate_to(mod.node_id)

    @_invalidates_library_probe
    def _on_game_changed(self, game_id: str) -> None:
        """在部署、变体、插件或预设事务期间恢复原游戏选择，空闲时切换管理器和页面上下文。

        索引不可用时先尝试恢复；有效游戏切换后重新绑定部署环境、文件监控和审计，取消激活时清空相关视图。"""
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and publication.busy:
            # 游戏页先修改共享服务再通知，配置回滚结束前仍保留原操作所有者。
            self._game_service.current_game_id = self._deploy_game_id
            self._game_page.setEnabled(False)
            self._game_page.reload()
            self._ensure_toast().done(tr("Mod 部署完成后可切换游戏"), ok=False, hold_ms=2500)
            return
        variants = getattr(self, "_variant_controller", None)
        if variants is not None and variants.busy:
            self._game_service.current_game_id = variants.game_id
            self._game_page.reload()
            return
        if getattr(self, "_preset_delete_preparing", False) or getattr(self, "_preset_delete_worker", None) is not None:
            self._game_service.current_game_id = self._preset_delete_game_id
            self._game_page.reload()
            return
        if _plugin_operation_busy(self):
            self._game_service.current_game_id = self._plugin_operations.game_id
            self._game_page.reload()
            return
        if (
            self._preset_apply_worker is not None
            or getattr(self, "_preset_apply_preparing", False)
            or getattr(self, "_disable_worker", None) is not None
        ):
            owner = getattr(self, "_preset_apply_game_id", "") or getattr(self, "_disable_game_id", "")
            if owner:
                self._game_service.current_game_id = owner
                self._game_page.reload()
            return
        if self._deploy_workers or self._deploy_active:
            # GamePage 会先改共享 GameService 再发信号。晚到的激活事件必须还原，
            # 否则队列里的旧游戏 Mod 会写进新目录，并以新 game_id 提交到索引。
            self._game_service.current_game_id = self._deploy_game_id
            if game_id != self._deploy_game_id:
                self._game_page.reload()
                self._ensure_toast().done(tr("Mod 部署完成后可切换游戏"), ok=False, hold_ms=2500)
            return
        cleanup = getattr(self, "_library_source_cleanup", None)
        if cleanup is not None:
            cleanup.stop()
        if game_id and not self._mod_manager.index_healthy:
            self._maybe_recover_index()
            if not self._mod_manager.index_healthy:
                self._game_service.current_game_id = ""
                self._game_page.reload()
                self._apply_nav_gating()
                return
        if not game_id:
            # 取消激活（如删除了当前激活的游戏）→ 复位数据源 + 清空残留视图 + 提示遮罩
            self._game_service.current_game_id = ""
            self._game_store.set_last_game_id("")
            self._mod_manager.set_game("")  # 复位索引指向，后续刷新只会获取空数据
            self._apply_migoto_for_current_game()
            self._clear_game_views()
            self._apply_nav_gating()
            logger.info("已取消游戏激活")
            return
        self._game_service.current_game_id = game_id
        # 记住当前环境，下次启动自动恢复
        self._game_store.set_last_game_id(game_id)
        self._mod_manager.set_game(game_id)
        self._apply_migoto_for_current_game()
        # 先显示目标游戏索引，再异步检查路径和新增内容，覆盖程序未运行期间的外部变更。
        self._start_file_watcher(game_id)
        self._schedule_activation_reconcile(game_id, "game_switch")
        self._load_characters(game_id, force_root=True)
        self._refresh_preset_combo()
        # 切换 Mod Store 面向的游戏（已知 → 重拉分类；未知 → 切空态视图）
        if hasattr(self, "_gb_store_page") and self._gb_store_page is not None:
            self._gb_store_page.set_game(game_id)
        self._apply_nav_gating()
        logger.info("已切换到游戏: %s", game_id)

    @contextmanager
    def _game_settings_save(self, old_game: GameConfig | None, new_game: GameConfig) -> Iterator[None]:
        """路径变更期间暂停当前环境，配置提交或取消后按实际保存的路径恢复监控。"""
        if old_game is None or (
            (GameService._sanitize_id(new_game.name_en, new_game.name) or new_game.id) == old_game.resource_dir
            and new_game.mods_path == old_game.mods_path
            and new_game.migoto_path == old_game.migoto_path
        ):
            yield
            return
        if self._plugin_host_busy() or _plugin_operation_busy(self) or self._deployment_activation.running:
            raise RuntimeError(tr("当前任务完成后可保存游戏路径设置。"))
        if old_game.id != self._game_service.current_game_id:
            yield
            return

        self._game_settings_saving = True
        try:
            self._library_probe.pause()
            self._library_discovery.reset_scope()
            self._library_local_discovery.reset_scope()
            self._overview_refresh.cancel()
            self._gallery_refresh.cancel()
            self._deployment_activation.cancel()
            for timer in (
                self._source_state_poll,
                self._deploy_state_poll,
                self._watcher_debounce,
                self._deploy_watcher_debounce,
                self._source_cleanup_retry,
            ):
                timer.stop()
            cleanup = getattr(self, "_library_source_cleanup", None)
            if cleanup is not None:
                cleanup.stop()
            self._file_watcher.stop()
            self._deploy_watcher.stop()
            self._deploy_changed_paths.clear()
            self._deployment_events = []
            self._mod_manager.variants.stop_watch()
            if self._mod_manager._ini_sync is not None:
                self._mod_manager._ini_sync.reconcile_all()
            self._mod_manager.stop_ini_sync()
            yield
        except ResourceDirectoryMigrationError:
            # 未完整恢复的资源只保留实际位置；取消激活，避免旧配置触发自动修复或重新建库。
            self._game_service.current_game_id = ""
            self._game_store.set_last_game_id("")
            self._mod_manager.set_game("")
            self._clear_game_views()
            self._apply_nav_gating()
            self._update_launch_buttons()
            raise
        finally:
            self._game_settings_saving = False
            if self._game_service.current_game_id == old_game.id:
                self._mod_manager.variants.invalidate()
                self._library_probe.reset_failure()
                self._deployment_root_watch.sync_games()
                self._start_file_watcher(old_game.id)
                self._apply_migoto_for_current_game()
                self._load_characters(old_game.id)
                self._refresh_gallery()
                self._schedule_source_cleanup_retry()

    @_invalidates_library_probe
    def _on_games_changed(self) -> None:
        """游戏列表被增删改/排序：刷新数据与门禁；激活由「游戏」页显式决定，不自动激活。"""
        self._game_service.reload_games()
        self._deployment_root_watch.sync_games()
        # 游戏配置保存后同步新的部署与加载器路径，使后续操作使用最新目录。
        self._apply_migoto_for_current_game()
        # 配置变化可能包含英文名修改，须重新匹配当前游戏的在线商店信息。
        gid = self._game_service.current_game_id
        if gid and getattr(self, "_gb_store_page", None) is not None:
            self._gb_store_page.set_game(gid, force=True)
        self._apply_nav_gating()

    def _on_deployment_directory_selected(self, game_id: str) -> None:
        """更新选中游戏的部署根跟踪，并使当前游戏的激活请求与错误提示失效。"""
        self._deployment_root_watch.sync_games(selected_game_id=game_id)
        if game_id == self._game_service.current_game_id:
            self._deployment_activation.cancel()
            self._library_probe.reset_failure()
            self._deployment_root_notice = None

    def _on_deploy_dir_cleared(self, game_id: str) -> None:
        """清空部署目录后重建视图，使卡片反映已保存的禁用状态。"""
        if not game_id or game_id != self._game_service.current_game_id:
            return
        try:
            self._load_characters(game_id, force_root=True)
            self._refresh_overview_mods()
            self._refresh_gallery()
            self._refresh_preset_combo()
        except Exception as e:
            logger.warning("清空部署目录后刷新视图失败: %s", e)

    # 类别/Mod 操作

    @_invalidates_library_probe
    def _on_character_add(self, data: dict) -> None:
        """创建当前游戏的分类及可选缩略图，成功后刷新分类视图并抑制自身文件事件。"""
        name = data.get("name", "")
        name_en = data.get("name_en", "")
        thumbnail = data.get("thumbnail", "")
        parent_id = data.get("parent_id", "")
        node_type = data.get("node_type", "category")
        char = self._mod_manager.add_character(
            name, name_en, self._game_service.current_game_id, parent_id=parent_id, node_type=node_type
        )
        if char and thumbnail:
            self._mod_manager.update_character_thumbnail(char.id, thumbnail)
        if char:
            self._load_characters(self._game_service.current_game_id)
            self._suppress_watcher()  # 直接刷新已生效，跳过 watcher 二次重建（避免闪两次）

    @_invalidates_library_probe
    def _on_character_edit(self, char_id: str, data: dict) -> None:
        """在无部署冲突时更新分类名称、类型和可选缩略图，再刷新视图。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        name = data.get("name", "")
        name_en = data.get("name_en", "")
        thumbnail = data.get("thumbnail", "")
        node_type = data.get("node_type", None)
        if self._mod_manager.update_character(char_id, name, name_en, node_type=node_type):
            if thumbnail is not None:
                self._mod_manager.update_character_thumbnail(char_id, thumbnail)
            self._load_characters(self._game_service.current_game_id)
            self._suppress_watcher()  # 跳过 watcher 二次重建

    @_invalidates_library_probe
    def _on_character_delete(self, char_id: str) -> None:
        """在事务空闲时确认永久删除分类及内容，成功后同步分类视图与商店安装状态。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if self._preset_apply_worker is not None:
            return
        # 获取类别名称用于提示信息。
        char_name = ""
        for c in self._overview_page._all_characters:
            if c.get("id") == char_id:
                char_name = c.get("name", "")
                break
        reply = QMessageBox.question(
            self,
            t("确认"),
            t("确定永久删除类别「{name}」及其中的 Mod 文件和子类别？\n删除后无法恢复。", name=char_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self._mod_manager.delete_character(char_id):
                self._load_characters(self._game_service.current_game_id)
                self._suppress_watcher()  # 跳过 watcher 二次重建
                # 级联删除会顺带删 character 下所有 Mod；让 store 全量比对一次
                self._refresh_store_installed_state()

    @_invalidates_library_probe
    def _on_character_batch_add(self, items: list) -> None:
        """按表单选择的类型和父目录批量创建节点，再刷新当前游戏的分类。"""
        count = 0
        for data in items:
            name = data.get("name", "")
            name_en = data.get("name_en", "")
            thumbnail = data.get("thumbnail", "")
            parent_id = data.get("parent_id", "")
            char = self._mod_manager.add_character(
                name,
                name_en,
                self._game_service.current_game_id,
                parent_id=parent_id,
                node_type=data.get("node_type", "category"),
            )
            if char:
                if thumbnail:
                    self._mod_manager.update_character_thumbnail(char.id, thumbnail)
                count += 1
        if count > 0:
            self._load_characters(self._game_service.current_game_id)
            self._suppress_watcher()  # 跳过 watcher 二次重建
            logger.info("批量添加类别完成: %d 个", count)

    @_invalidates_library_probe
    def _on_character_batch_delete(self, char_ids: list) -> None:
        """批量删除类别。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if self._preset_apply_worker is not None:
            return
        count = 0
        for char_id in char_ids:
            if self._mod_manager.delete_character(char_id):
                count += 1
        if count > 0:
            self._reload_overview_level(self._game_service.current_game_id)
            self._suppress_watcher()  # 跳过 watcher 二次重建
            logger.info("批量删除类别完成: %d 个", count)
            self._refresh_store_installed_state()

    def _refresh_store_installed_state(self) -> None:
        """让 Mod 商店重新比对所有可见卡片的「已安装」徽章。

        character 级联删除等场景不方便逐个收集 gb_mod_id，调本方法即可。
        """
        store = getattr(self, "_gb_store_page", None)
        if store is None or not hasattr(store, "refresh_installed_state"):
            return
        try:
            store.refresh_installed_state()
        except Exception as exc:
            logger.warning("refresh_store_installed_state 失败: %s", exc)

    # Overview 混合视图操作

    def _overview_query_key(self) -> tuple:
        """返回当前游戏、分类和全部筛选条件组成的总览请求键，用于隔离过期加载。"""
        page = self._overview_page
        return (
            self._game_service.current_game_id,
            page._current_parent_id,
            page._search_input.text().strip(),
            tuple(sorted(page._tag_filter.selected_ids)),
            page._type_filter.currentData(),
            page._age_filter.currentData(),
        )

    def _on_overview_navigated(self, parent_id: str) -> None:
        """OverviewPage 导航到新层级 → 加载该层级的 Mod"""
        self._library_probe.yield_to_local_events()
        overview = self._overview_page
        self._overview_root_presented = not parent_id and not overview._search_input.text().strip()
        if self._overview_root_presented:
            # 根分类已经可用，不因准备后代搜索数据而阻塞导航。
            overview.set_current_mods([], [])
        self._overview_refresh.schedule()

    def _refresh_overview_mods(self) -> None:
        """刷新 OverviewPage 当前层级的 Mod"""
        self._overview_root_presented = False
        self._overview_refresh.schedule()

    def _apply_overview_mods(self, mods, subtree_mods) -> None:
        """发布总览直接项与子树列表，避免重复刷新已呈现根视图，并为局部事件处理让行。"""
        self._library_probe.yield_to_local_events()
        overview = self._overview_page
        root_presented = (
            getattr(self, "_overview_root_presented", False)
            and not overview._current_parent_id
            and not overview._search_input.text().strip()
            and not overview._tag_filter.selected_ids
        )
        self._overview_root_presented = False
        overview.set_current_mods(mods, subtree_mods, refresh_view=not root_presented)
        self._defer_source_reconcile()

    def _reload_overview_level(self, gid: str) -> None:
        """一次性重建总览当前层级（类别 + Mod），只触发一次 _refresh_cards。

        先 set_characters(refresh=False) 仅更新类别数据与父子映射、不重建卡片，再由
        _refresh_overview_mods → set_current_mods 触发唯一一次重排。避免「先 set_characters
        再 set_current_mods」两次重建造成的二次闪烁（也减少导入后卡片重排的崩溃窗口）。"""
        if not gid or not self._game_service.has_active_game():
            self._overview_page.set_characters([], force_root=True)
            return
        chars = self._mod_manager.get_characters_for_ui(gid)
        self._overview_page.set_characters(chars, refresh=False)
        self._refresh_overview_mods()

    def _suppress_watcher(self, seconds: float = 1.2) -> None:
        """短期抑制应用自身文件操作引起的重复目录刷新。

        导入和分类修改已直接更新视图，抑制窗口覆盖监控防抖时间，避免同一操作再次重建页面。
        """
        self._watcher_suppress_until = time.monotonic() + seconds

    def _collect_subtree_mods(self, parent_id: str) -> list[Mod]:
        """收集某目录下（含子孙目录）的全部 Mod。"""
        if not self._game_service.has_active_game():
            return []  # 防空 game_id 回退到全库（见 _refresh_gallery 注释）
        if not parent_id:
            return self._mod_manager.get_mods(game_id=self._game_service.current_game_id)
        subtree_ids = self._mod_manager.get_subtree_ids(parent_id)
        all_mods: list[Mod] = []
        for cid in subtree_ids:
            all_mods.extend(self._mod_manager.get_mods(node_id=cid))
        return all_mods

    def _require_deploy_dir(self) -> bool:
        """启用 Mod / 切换预设前确认已配置部署目录；未配置则提示并返回 False。

        部署操作要求有效的部署目录，因此在界面入口统一检查并提示缺失配置。
        """
        root_watch = getattr(self, "_deployment_root_watch", None)
        if root_watch is not None:
            resolution = root_watch.check_current()
            if resolution.status == "missing":
                self._on_deployment_root_invalid(self._game_service.current_game_id, resolution.previous_path)
                return False
            if resolution.status == "moved":
                return False  # 存在待提交修改或配置保存失败。
            if resolution.status == "current":
                from src.core.library_snapshot import path_key

                bound = str(self._mod_manager._storage.get_deploy_dir() or "")
                if path_key(bound) != path_key(resolution.path):
                    self._apply_migoto_for_current_game()
                    self._ensure_toast().done(tr("正在准备部署目录，请稍后重试"), ok=False, hold_ms=2500)
                    return False
        if self._mod_manager.has_deploy_dir():
            return True
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(
            self,
            tr("未配置 Mod 文件夹"),
            tr("当前游戏尚未配置「Mod 文件夹」，请前往「游戏」页编辑当前游戏，设置「Mod 文件夹」后重试。"),
        )
        return False

    # 角色目录单 Mod 互斥（画廊/总览共用一份）

    _CONFLICT_SUPPRESS_KEY = "ui.suppress_character_conflict_prompt"

    def _mods_being_deployed(self) -> set[str]:
        """返回正在后台部署的 Mod 编号。

        部署完成前数据库仍为禁用状态，互斥检查须包含这些在途任务，
        以维持角色目录同时只启用一个 Mod 的约束。
        """
        return {mid for mid in getattr(self, "_deploy_workers", {})}

    def _character_conflict_scope(self, mod):
        """确定 Mod 所属角色祖先及冲突子树；无角色范围时返回空集合。"""
        if not mod.node_id:
            return None, []
        ancestor = self._mod_manager.find_character_ancestor(mod.node_id)
        if ancestor is not None:
            return ancestor, self._mod_manager.get_subtree_ids(ancestor.id)
        current = self._mod_manager.get_character(mod.node_id)
        scope_ids = [mod.node_id] if current and current.node_type == "character" else []
        return None, scope_ids

    def _enable_waits_for_publication(self, mod) -> bool:
        """归档可独立准备，共享偏好仍按顺序提交。"""
        if not self._mod_manager.needs_async_deploy(mod.mod_id):
            return True
        _, scope_ids = self._character_conflict_scope(mod)
        if not scope_ids:
            return False
        reserved = self._mods_being_deployed()
        protection = getattr(self._mod_manager, "_library_protection", None)
        if protection is not None:
            # 发布阶段保留被替换 Mod 的所有权，直到失败回滚结束。
            reserved.update(protection.snapshot()[1])
        siblings = self._mod_manager.get_character_conflict_mods(
            scope_ids, deploying_ids=reserved, excluding_mod_id=mod.mod_id
        )
        return any(sibling.mod_id in reserved for sibling in siblings)

    def _resolve_character_conflict(self, mod, *, defer=False) -> tuple[bool, list[str]]:
        """启用 mod 前处理「角色目录同时只启用一个」的互斥。

        Returns (是否继续启用, 已被禁用的其它 Mod id 列表)。
        约束来源：node_type == 'character' 的目录（或挂在这类目录之下的子分类）。
        """
        if mod.is_enabled or not mod.node_id:
            return True, []
        char_ancestor, scope_ids = self._character_conflict_scope(mod)
        if not scope_ids:
            return True, []

        deploying = self._mods_being_deployed()
        siblings = self._mod_manager.get_character_conflict_mods(
            scope_ids, deploying_ids=deploying, excluding_mod_id=mod.mod_id
        )
        # 在途部署尚未提交，不能按已启用状态禁用，也不能同时启用同一角色下的冲突项。
        pending = [m for m in siblings if m.mod_id in deploying]
        if pending:
            QMessageBox.information(
                self,
                t("提示"),
                tr(
                    "「{display_name}」正在部署中，请等它完成后再切换本角色下的 Mod。",
                    display_name=pending[0].display_name,
                ),
            )
            return False, []
        enabled_mods = [m for m in siblings if m.is_enabled]
        if not enabled_mods:
            return True, []

        scope = tr("角色「{name}」", name=char_ancestor.name) if char_ancestor else tr("当前角色")
        if not self._config.get(self._CONFLICT_SUPPRESS_KEY, False):
            dialog = QMessageBox(self)
            dialog.setWindowTitle(t("确认"))
            dialog.setIcon(QMessageBox.Icon.Question)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            adult_mode = self._overview_page._adult_mode
            first_enabled = enabled_mods[0]
            show_preview = (
                first_enabled.primary_preview
                and Path(first_enabled.primary_preview).exists()
                and not (adult_mode != "show" and first_enabled.info.age_rating == AgeRating.X)
            )
            msg_parts = [tr("{scope}已启用{value}Mod：", scope=scope, value=tr("左图") if show_preview else "")]
            for em in enabled_mods:
                msg_parts.append(f"  · {em.display_name}")
            msg_parts.append(tr('\n是否禁用它并启用"{display_name}"', display_name=mod.display_name))
            dialog.setText("\n".join(msg_parts))
            if show_preview:
                pixmap = QPixmap(first_enabled.primary_preview)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(
                        200,
                        200,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    dialog.setIconPixmap(scaled)
            # 仅在确认启用时保存不再提示选项，取消操作不改变后续确认行为。
            never = QCheckBox(tr("不再提示，以后自动禁用冲突的 Mod"))
            dialog.setCheckBox(never)
            confirmed = dialog.exec() == QMessageBox.StandardButton.Yes
            if not confirmed:
                return False, []
            if never.isChecked():
                self._config.set(self._CONFLICT_SUPPRESS_KEY, True)
        else:
            logger.info("角色互斥提示已被用户关闭，自动禁用 %d 个冲突 Mod", len(enabled_mods))

        if defer:
            return True, [em.mod_id for em in enabled_mods]
        if self._mod_manager.check_mod_operation(mod.mod_id, metadata=True) is None:
            return False, []
        disabled_ids: list[str] = []
        for em in enabled_mods:
            if self._mod_manager.toggle_mod(em.mod_id):
                disabled_ids.append(em.mod_id)
        return True, disabled_ids

    def _mod_update_blocks_deploy(self, mod_id: str) -> bool:
        """检测该 Mod 是否正在跨游戏更新，冲突时显示提示并阻止部署。"""
        if mod_id not in self._mod_update_service.active_update_ids(all_games=True):
            return False
        self._ensure_toast().done(tr("该 Mod 正在更新，请在更新完成或终止后重试。"), ok=False, hold_ms=2500)
        return True

    @_invalidates_library_probe
    def _on_overview_mod_toggle(self, mod_id: str) -> None:
        """切换总览页中 Mod 的启用状态，并就地更新卡片。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self, include_publication=False):
            return
        publication = getattr(self, "_enable_publication", None)
        confirming = getattr(self, "_deploy_conflict_mod_id", "")
        if confirming or (publication is not None and publication.busy):
            mod = self._mod_manager.get_mod(mod_id)
            if (
                (publication is not None and publication.closing)
                or self._deploy_stopping
                or mod is None
                or mod.is_enabled
                or mod_id == confirming
                or mod_id in self._deploy_workers
            ):
                return
            if confirming or self._enable_waits_for_publication(mod):
                if not hasattr(self, "_deferred_enable_ids"):
                    self._deferred_enable_ids = {}
                if mod_id not in self._deferred_enable_ids:
                    self._deferred_enable_ids[mod_id] = self._game_service.current_game_id
                    self._overview_page.begin_deploy_overlay(mod_id)
                return
        if (
            self._preset_apply_worker is not None
            or getattr(self, "_preset_apply_preparing", False)
            or getattr(self, "_disable_worker", None) is not None
            or mod_id in self._deploy_workers
        ):
            return
        if self._mod_update_blocks_deploy(mod_id):
            return
        mod = self._mod_manager.get_mod(mod_id)
        if not mod:
            return
        if mod.is_enabled:
            self._on_overview_batch_disable([mod_id])
            return
        self._deploy_conflict_mod_id = mod_id
        try:
            self._enable_overview_mod(mod)
        finally:
            self._deploy_conflict_mod_id = ""
            self._schedule_deferred_enables()

    def _enable_overview_mod(self, mod) -> None:
        """模态检查和工作线程注册期间保持请求准入限制。"""
        mod_id = mod.mod_id
        if not self._require_deploy_dir():
            return

        if self._mod_manager.check_mod_operation(mod_id, metadata=True, full=False) is None:
            return
        proceed, disabled_ids = self._resolve_character_conflict(mod, defer=True)
        if not proceed:
            return

        # 启用压缩档 Mod 需解压部署（耗时）→ 后台线程 + 卡片 / 列表实时进度，
        # 停放副本的身份扫描与恢复也走同一后台线程。
        if self._mod_manager.needs_async_deploy(mod_id):
            self._start_async_deploy(mod_id, disabled_ids)
            return

        toggled = self._mod_manager.toggle_mod(mod_id)
        if not toggled:
            if not mod.is_enabled:
                self._overview_page.finish_deploy_overlay(
                    mod_id, success=False, error=tr("启用失败，请检查 Mod 和部署目录后重试，详细原因见日志。")
                )
            return

        # 就地更新 Overview 卡片（轻量级，避免全量重建）
        self._overview_page.update_mod_toggle_in_place(mod_id, toggled, disabled_ids)
        if toggled.is_enabled:
            # 停放缓存恢复等快速启用直接显示成功反馈，不人为等待进度动画。
            self._overview_page.finish_deploy_overlay(mod_id)

        # 后台更新角色计数（不触发刷新）
        chars = self._mod_manager.get_characters_for_ui(self._game_service.current_game_id)
        self._overview_page.set_characters(chars, refresh=False)

    @_invalidates_library_probe
    def _start_async_deploy(self, mod_id: str, disabled_ids: list[str]) -> None:
        """异步部署归档，卡片和列表共用进度信号。

        后台执行文件操作，主线程校验并提交结果；同一 Mod 的重复部署请求直接忽略。
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self, include_publication=False):
            return
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and publication.closing:
            return
        if self._deploy_stopping or mod_id in self._deploy_workers:
            return  # 部署进行中，忽略重复触发
        if self._mod_update_blocks_deploy(mod_id):
            return
        # 冲突确认可进入嵌套事件循环，采集部署目标前再次检查。
        if not self._require_deploy_dir():
            return
        mod = self._mod_manager.get_mod(mod_id)
        if not mod or not mod.path:
            return

        from src.ui.deploy_worker import DeployWorker

        if not self._deploy_workers and not self._deploy_active:
            self._deploy_game_id = self._game_service.current_game_id
            self._game_page.setToolTip(tr("Mod 部署完成后可切换游戏或修改部署目录。"))
            self._game_page.setEnabled(False)
        self._overview_page.begin_deploy_overlay(mod_id)
        worker = DeployWorker(
            mod_id,
            mod.path,
            self._mod_manager._storage,
            self,
            deploy_name=mod.folder_name or Path(mod.path).stem,
            restore_existing=True,
            remembered_path=mod.deployed_path,
            stage_only=True,
            content_hash=mod.content_hash,
        )
        worker.disabled_ids = tuple(disabled_ids)
        self._deploy_workers[mod_id] = worker
        self._mod_manager.protect_library_operation(
            "deploy:" + mod_id, paths=(mod.path, mod.deployed_path), mod_ids=(mod_id,)
        )
        worker.progress.connect(self._overview_page.update_deploy_overlay)
        # 自定义结果信号发出时 run 尚未返回。等线程真正结束再释放 QThread，
        # 同时保证并发名额覆盖完整的解压和收尾阶段。
        worker.finished.connect(
            lambda mid=mod_id, w=worker, dids=disabled_ids: self._on_async_deploy_finished(mid, w, dids)
        )
        self._pending_deploy_ids.append(mod_id)
        self._start_pending_deploys()

    def _start_pending_deploys(self) -> None:
        """按点击顺序和当前并发设置调度，已运行的任务自然完成。"""
        if self._deploy_stopping or not self._pending_deploy_ids:
            return
        concurrency = effective_deploy_concurrency(self._config.get("mods.deploy_concurrency"))
        while self._pending_deploy_ids and len(self._deploy_active) < concurrency:
            mod_id = self._pending_deploy_ids.popleft()
            worker = self._deploy_workers.get(mod_id)
            if worker is None:
                continue
            self._deploy_active.add(mod_id)
            worker.start(QThread.Priority.LowPriority)

    @_invalidates_library_probe
    def _on_async_deploy_finished(self, mod_id: str, worker, disabled_ids: list[str]) -> None:
        """先提交真实启用态，再结束卡片 / 列表反馈，避免提前显示成功。"""
        if self._deploy_workers.get(mod_id) is not worker:
            # 关窗等待线程结束后可能已经补提交；忽略随后投递的 queued finished。
            # 用线程身份校验，旧请求不能误结算同一 Mod 后来的重试。
            return
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and worker.result[0] and not getattr(worker, "configuration_prepared", False):
            publication.enqueue(mod_id, worker, disabled_ids)
            return
        self._deploy_workers.pop(mod_id)
        ok, _deploy_path = worker.result
        switched_ids = list(getattr(worker, "switched_ids", ()))
        published = False
        try:
            if not ok:
                error = (
                    tr("已取消启用，Mod 保持禁用。")
                    if self._deploy_stopping
                    else tr("部署失败，Mod 保持禁用。请检查压缩包和部署目录后重试，详细原因见日志。")
                )
                self._overview_page.finish_deploy_overlay(mod_id, success=False, error=error)
                from src.core.operation_preflight import OperationPreflightError

                if isinstance(getattr(worker, "error", None), OperationPreflightError):
                    self._mod_manager.report_operation_failure(worker.error)
                elif not self._deploy_stopping:
                    QMessageBox.warning(self, t("错误"), error)
                return
            try:
                if getattr(worker, "configuration_error", None) is not None:
                    raise worker.configuration_error
                receipt = getattr(worker, "source_receipt", None)
                if receipt is not None:
                    receipt.recheck()
                if disabled_ids and not getattr(worker, "configuration_prepared", False):
                    from dataclasses import replace

                    entries = self._mod_manager.prepare_disable_batch(disabled_ids)
                    if len(entries) != len(disabled_ids):
                        return
                    results = []
                    for entry in entries:
                        result = self._mod_manager.disable_batch_step(replace(entry, park=True))
                        results.append(result)
                        if not result.ok:
                            break
                    switched_ids = self._mod_manager.commit_disable_batch(results)
                    if len(switched_ids) != len(entries):
                        return
                if getattr(worker, "_stage_only", False) and not getattr(worker, "configuration_prepared", False):
                    if receipt is not None:
                        receipt.recheck()
                    name = self._mod_manager._storage.active_name_from_parked(_deploy_path, mod_id)
                    restored = self._mod_manager._storage.restore_parked_deployment(_deploy_path, name)
                    if restored is None:
                        raise OSError(tr("无法启用已准备好的部署文件"))
                    _deploy_path = str(restored)
                preparation = getattr(worker, "enable_preparation", None)
                options = {"preparation": preparation} if preparation is not None else {}
                toggled = self._mod_manager.finalize_enable(
                    mod_id, deploy_ok=True, deploy_path=_deploy_path, source_receipt=receipt, **options
                )
            except Exception as exc:
                logger.exception("提交 Mod 启用状态失败 (%s)", mod_id)
                error = tr("无法确认 Mod 启用状态：{exc}", exc=exc)
                self._overview_page.finish_deploy_overlay(mod_id, success=False, error=error)
                from src.core.operation_preflight import OperationPreflightError

                if isinstance(exc, OperationPreflightError):
                    self._mod_manager.report_operation_failure(exc)
                elif not self._deploy_stopping:
                    QMessageBox.warning(self, t("错误"), error)
                return
            if not toggled:
                self._overview_page.finish_deploy_overlay(
                    mod_id, success=False, error=tr("无法确认 Mod 启用状态，请检查 Mod 与部署目录后重试。")
                )
                return
            self._overview_page.update_mod_toggle_in_place(mod_id, toggled, disabled_ids)
            published = True
            self._overview_page.finish_deploy_overlay(mod_id)
            chars = self._mod_manager.get_characters_for_ui(self._game_service.current_game_id)
            self._overview_page.set_characters(chars, refresh=False)
        finally:
            if not published and not getattr(worker, "rollback_managed", False):
                # 只撤销本操作的部署变化，资源缺失清理由后台对账负责。
                if ok and _deploy_path:
                    storage = self._mod_manager._storage
                    try:
                        storage._check_deployment_identity(_deploy_path, mod_id)
                        if getattr(worker, "created_deployment", False):
                            storage.remove_deploy_path(_deploy_path)
                        elif not Path(_deploy_path).name.upper().startswith("DISABLED"):
                            storage.park_deployment(_deploy_path, mod_id)
                    except (OSError, ValueError) as rollback_error:
                        logger.warning("撤销本次部署失败: %s", rollback_error)
                for previous_id in switched_ids:
                    self._mod_manager.toggle_mod(previous_id)
            self._mod_manager.release_library_operation("deploy:" + mod_id)
            self._deploy_active.discard(mod_id)
            worker.deleteLater()
            if not self._deploy_workers and not self._deploy_active:
                publication_busy = publication is not None and publication.busy
                self._game_page.setEnabled(not publication_busy)
                if not publication_busy:
                    apply_deferred_environment = self._deploy_environment_dirty and not self._deploy_close_pending
                    self._deploy_environment_dirty = False
                    self._deploy_stopping = False
                    self._deploy_close_pending = False
                    self._deploy_game_id = ""
                    self._game_page.setToolTip("")
                    if apply_deferred_environment:
                        call_later(0, self, self._apply_migoto_for_current_game)
            # 留一个事件循环间隙，让鼠标/绘制先得到响应，再开始下一项。
            call_later(0, self, self._start_pending_deploys)
            self._schedule_deferred_enables()

    def _stop_async_deploys(self, timeout_ms: int = 5000) -> bool:
        """退出时取消排队项、协作停止解压；已物化的成功结果仍在主线程补提交。"""
        for mod_id in tuple(getattr(self, "_deferred_enable_ids", {})):
            self._overview_page.finish_deploy_overlay(mod_id, success=False, error=tr("已取消排队的启用请求"))
        getattr(self, "_deferred_enable_ids", {}).clear()
        if not self._deploy_workers:
            return True
        self._deploy_stopping = True
        self._deploy_close_pending = True
        pending = list(self._pending_deploy_ids)
        self._pending_deploy_ids.clear()
        for mod_id in pending:
            worker = self._deploy_workers.get(mod_id)
            if worker is not None:
                self._on_async_deploy_finished(mod_id, worker, [])

        workers = list(self._deploy_workers.items())
        for _mod_id, worker in workers:
            worker.requestInterruption()
        deadline = time.monotonic() + timeout_ms / 1000
        for _mod_id, worker in workers:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not worker.wait(remaining_ms):
                # 不销毁仍在写盘的线程；窗口保持打开，finished 稍后正常收尾。
                self._deploy_close_pending = False
                return False
        for mod_id, worker in workers:
            self._on_async_deploy_finished(mod_id, worker, list(getattr(worker, "disabled_ids", ())))
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and publication.busy:
            self._enable_publication_close_pending = True
            publication.close()
            return False
        return True

    def _on_ini_synced_from_deploy(self, mod_id: str) -> None:
        """按键相关 ini 同步后（watchdog 线程经信号 marshal 到主线程）刷新按键面板。

        同步器已过滤不含按键的 ini；此处再由 OverviewPage 判断该 Mod 是否正被选中，
        仅在确有可见按键内容可能变化时就地重建，无需用户重新选择卡片。
        """
        try:
            self._overview_page.refresh_keys_if_selected(mod_id)
        except Exception as e:
            logger.debug("刷新按键面板失败 (%s): %s", mod_id, e)

    def _connect_variant_controller(self) -> None:
        """创建变体控制器，将 INI 与核心事件桥接到界面线程并连接忙碌及停止信号。"""
        from src.ui.mod_variant_controller import ModVariantController

        self._variant_controller = ModVariantController(
            self._overview_page,
            self._mod_manager,
            enable_callback=self._on_overview_mod_toggle,
            notify=lambda message, ok: self._ensure_toast().done(message, ok=ok, hold_ms=5000),
            parent=self,
        )
        # 核心回调可能来自工作线程，通过绑定槽将采集和局部预览刷新转到所有者线程。
        self._plugin_variant_pending = False
        self._pending_variant_event = None
        self.user_ini_changed.connect(self._on_user_ini_changed)
        self.variant_event_received.connect(self._on_variant_event)
        self._mod_manager.variants.set_change_notify(self.user_ini_changed.emit)
        self._variant_controller.activity_changed.connect(self._on_variant_activity_changed)
        self._variant_controller.stopped.connect(self._on_variant_stopped)

    def _on_variant_activity_changed(self, busy) -> None:
        """变体写入期间锁定游戏与组合选择，任务结束后恢复延后的库环境操作。"""
        selector = getattr(self._overview_page, "_preset_selector", None)
        if selector is not None:
            selector.setEnabled(not busy)
            if busy:
                selector._popup.hide()
        self._game_page.setEnabled(not busy)
        if not busy and not self._variant_controller._closing:
            self._resume_deferred_library_environment()

    def _on_variant_stopped(self) -> None:
        """清除变体关闭等待标记，并在下一轮事件循环继续关闭窗口。"""
        if getattr(self, "_variant_close_pending", False):
            self._variant_close_pending = False
            call_later(0, self, self.close)

    def _notify_variant_data_not_ready(self, error) -> None:
        """记录变体读取错误并停止监控，异步提示原文件保留及后续检查方式。"""
        logger.warning("变体数据尚未就绪: %s", error)
        stop = getattr(self._mod_manager.variants, "stop_watch", None)
        if callable(stop):
            stop()
        message = tr("外观工作状态无法读取，原文件已保留。请检查文件或备份后重试。")
        call_later(0, self, lambda: self._ensure_toast().done(message, ok=False, hold_ms=8000))

    def _collect_startup_variants(self) -> bool:
        """启动时收集变体配置，读取失败转为保留原文件的提示并返回 False。"""
        try:
            self._mod_manager.variants.collect(trigger="startup")
            return True
        except (OSError, ValueError) as error:
            self._notify_variant_data_not_ready(error)
            return False

    def _resume_pending_variant_capture(self) -> None:
        # 插件和部署完成都可能请求重试，只消费一次最新事件。
        """插件操作曾推迟 INI 采集时重新进入用户配置变化处理。"""
        if getattr(self, "_plugin_variant_pending", False):
            self._on_user_ini_changed()

    @_invalidates_library_probe
    def _on_user_ini_changed(self, event=None) -> None:
        """在主线程响应 d3dx_user.ini 的文件变更并采集运行时变体。

        加载器重载配置和游戏退出均会写入该文件，无需依赖特定快捷键或检测游戏进程。
        only_if_changed 排除程序自身写入的内容；成功后只刷新当前 Mod 的变体页。
        """
        if event is not None:
            self._pending_variant_event = event
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            self._plugin_variant_pending = True
            return
        event = getattr(self, "_pending_variant_event", None)
        self._pending_variant_event = None
        self._plugin_variant_pending = False
        try:
            report = self._mod_manager.variants.collect(trigger="user_ini_watch", only_if_changed=True, event=event)
        except Exception as e:
            logger.warning("d3dx_user.ini 变更后采集变体失败: %s", e)
            return
        if report is not None and report.changed:
            logger.info("d3dx_user.ini 变更：已采集 %d 个 Mod 的变体状态", len(report.attributed))

    @_invalidates_library_probe
    def _on_overview_mod_delete(self, mod_id: str) -> None:
        """删除总览页中的 Mod。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if self._preset_apply_worker is not None:
            return
        mod = self._mod_manager.get_mod(mod_id)
        if not mod:
            return
        delete_method = self._configured_mod_delete_method()
        # 删除前抓 gb 元数据——_mod_manager.delete_mod 之后再查就无法获取了
        gb_mod_id = self._extract_gb_mod_id(mod)

        # 已启用的 Mod 使用不同的提示
        if mod.is_enabled:
            message = (
                tr("当前 Mod 已启用，将先从游戏目录移除，再放入系统回收站。\n是否继续？")
                if delete_method == "recycle_bin"
                else tr("当前Mod已启用，您是否仍要删除它？\n此操作不可恢复！")
            )
            reply = QMessageBox.question(
                self,
                t("确认"),
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        else:
            message = (
                tr("确定要将“{display_name}”放入系统回收站吗？", display_name=mod.display_name)
                if delete_method == "recycle_bin"
                else t("确定删除该 Mod 吗？此操作不可恢复。")
            )
            reply = QMessageBox.question(
                self,
                t("确认"),
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )

        if reply == QMessageBox.StandardButton.Yes:
            if delete_mod_with_fallback(
                self,
                self._mod_manager,
                mod_id,
                delete_method,
            ):
                # 删除当前选中的 Mod 时清空预览面板。
                if self._overview_page._selected_mod and self._overview_page._selected_mod.mod_id == mod_id:
                    self._overview_page._selected_mod = None
                    self._overview_page._selected_ids.discard(mod_id)
                    self._overview_page._hide_panel()
                # 立即同步 UI（角色 + Mod 列表）
                self._load_characters(self._game_service.current_game_id)
                self._refresh_overview_mods()
                # 分别在下一个事件循环和延迟 500 毫秒后刷新，合并异步文件操作引起的状态变化。
                call_later(0, self, self._refresh_overview_mods)
                self._watcher_debounce.start()
                # 通知 Mod 商店把对应卡片的「已安装」徽章撤掉
                self._notify_store_mod_deleted(gb_mod_id)

    @_invalidates_library_probe
    def _on_overview_mod_batch_delete(self, mod_ids: list) -> None:
        """批量删除总览页中的 Mod，并清理选中状态和预览面板。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if self._preset_apply_worker is not None:
            return
        if not mod_ids:
            return

        # 记录选中的Mod id（在删除前）
        selected_mod_id = self._overview_page._selected_mod.mod_id if self._overview_page._selected_mod else ""
        # 删除前先把 gb_mod_id 抓出来——删除后再去 mod_index 查就空了
        gb_mod_ids: list[int] = []
        for mid in mod_ids:
            m = self._mod_manager.get_mod(mid)
            if m:
                gid = self._extract_gb_mod_id(m)
                if gid:
                    gb_mod_ids.append(gid)

        # 使用删除进度对话框
        dialog = DeleteProgressDialog(
            mod_ids,
            self._mod_manager,
            parent=self,
            delete_method=self._configured_mod_delete_method(),
        )
        dialog.deletion_finished.connect(
            lambda count: self._on_batch_delete_finished(count, selected_mod_id, mod_ids, gb_mod_ids)
        )
        dialog.start_deletion()
        dialog.exec()

    def _on_batch_delete_finished(
        self,
        deleted_count: int,
        selected_mod_id: str,
        mod_ids: list,
        gb_mod_ids: list[int] | None = None,
    ):
        """批量删除完成后的处理"""
        if deleted_count > 0:
            # 若当前选中的Mod在删除列表中，清空预览面板
            if selected_mod_id and selected_mod_id in mod_ids:
                self._overview_page._selected_mod = None
                self._overview_page._selected_ids.discard(selected_mod_id)
                self._overview_page._hide_panel()
            self._load_characters(self._game_service.current_game_id)
            self._refresh_overview_mods()
            # 异步回退处理刷新 + watcher 防抖（同 _on_overview_mod_delete）
            call_later(0, self, self._refresh_overview_mods)
            self._watcher_debounce.start()
            logger.info("批量删除 Mod 完成（Overview）: %d 个", deleted_count)
            # 通知 Mod 商店去掉相应卡片的「已安装」徽章
            if gb_mod_ids:
                self._notify_store_mod_deleted(gb_mod_ids)

    @staticmethod
    def _extract_gb_mod_id(mod) -> int:
        """从本地 Mod 中抠出 gamebanana mod_id；不是 GB 导入的返回 0。"""
        try:
            gb = (mod.info.custom_data or {}).get("gamebanana") or {}
            return int(gb.get("mod_id") or 0)
        except Exception:
            return 0

    def _configured_mod_delete_method(self) -> str:
        """读取有效的 Mod 删除方式，未知配置回退为永久删除。"""
        method = self._config.get("mods.delete_method", "permanent")
        return method if method in ("permanent", "recycle_bin") else "permanent"

    def _notify_store_mod_deleted(self, gb_mod_ids):
        """通知 Mod 商店刷新对应卡片状态。store 页可能还没构建好，做容错。"""
        if not gb_mod_ids:
            return
        store = getattr(self, "_gb_store_page", None)
        if store is None or not hasattr(store, "notify_mod_deleted"):
            return
        try:
            store.notify_mod_deleted(gb_mod_ids)
        except Exception as exc:
            logger.warning("notify_store_mod_deleted 失败: %s", exc)

    @_invalidates_library_probe
    def _on_overview_batch_disable(self, mod_ids: list) -> None:
        """统一处理批量禁用入口。

        主线程一次性准备整批操作，DisableBatchWorker 执行文件读写，
        完成后由主线程提交单个索引事务并保存预设。
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if (
            self._preset_apply_worker is not None
            or getattr(self, "_preset_apply_preparing", False)
            or self._deploy_workers
            or getattr(self, "_disable_worker", None) is not None
        ):
            return
        mod_ids = tuple(dict.fromkeys(mod_ids or []))
        if not mod_ids:
            return
        from src.ui.preset_deploy_worker import DeploymentPhaseWorker

        total = len(mod_ids)
        title = tr("正在禁用 {total} 个 Mod", total=total)
        panel = self._intake_progress
        panel.start_batch(total, title)
        panel.update_progress(None, title, tr("正在准备：检查部署与保存变体…"))
        self._raise_task_center()
        self._disable_game_id = self._game_service.current_game_id
        self._game_page.setEnabled(False)
        # 先显示反馈，再扫描文件和读取用户 INI；此前仅采集小型数据库投影。
        try:
            preparation = self._mod_manager.create_deployment_preparation(trigger="mod_disable", mod_ids=mod_ids)
            self._mod_manager.protect_library_operation(
                "disable-batch",
                mod_ids=mod_ids,
                paths=tuple(p for mod in preparation.mods for p in (mod.path, mod.deployed_path)),
            )
        except Exception as error:
            panel.finish(tr("禁用准备失败"), str(error), ok=False)
            self._disable_game_id = ""
            self._game_page.setEnabled(True)
            return
        worker = DeploymentPhaseWorker(preparation.prepare, self)
        self._disable_worker = worker
        worker.finished.connect(lambda w=worker: self._on_disable_prepared(w, mod_ids))
        panel.stop_requested.connect(self._on_disable_stop_requested)
        worker.start(QThread.Priority.LowPriority)

    def _on_disable_prepared(self, worker, mod_ids) -> None:
        """验证后台停用准备结果并创建逐项停用线程，准备失败时报告错误并释放界面状态。"""
        if self._disable_worker is not worker:
            return
        panel = self._intake_progress
        try:
            if worker.isInterruptionRequested():
                self._on_disable_batch_finished([], True)
                return
            if worker.error is not None:
                raise worker.error
            entries = self._mod_manager.prepare_disable_batch(mod_ids, preparation=worker.result)
        except Exception as error:
            panel.finish(tr("禁用准备失败"), str(error), ok=False)
            from src.core.operation_preflight import OperationPreflightError

            if isinstance(error, OperationPreflightError):
                self._mod_manager.report_operation_failure(error)
            self._clear_disable_ui(worker)
            return
        if not entries:
            self._on_disable_batch_finished([], False)
            return
        from src.ui.disable_worker import DisableBatchWorker

        for index, entry in enumerate(entries):
            panel.update_item(index, entry.display_name, ST_QUEUED)
        self._disable_entries = entries
        self._disable_done = 0
        worker.deleteLater()
        worker = DisableBatchWorker(entries, self._mod_manager, self)
        self._disable_worker = worker
        worker.item_started.connect(self._on_disable_item_started)
        worker.item_finished.connect(self._on_disable_item_finished)
        worker.finished.connect(lambda w=worker: self._finish_disable_worker(w))
        worker.start(QThread.Priority.LowPriority)

    def _finish_disable_worker(self, worker) -> None:
        """只为当前停用线程提交其保存的批次结果与取消状态。"""
        if self._disable_worker is worker:
            self._on_disable_batch_finished(worker.results or [], worker.cancelled)

    def _on_disable_stop_requested(self) -> None:
        """浮层 ■：请求中断批量禁用。已处理的保持已禁用，未处理的保持原样（不回滚）。"""
        worker = getattr(self, "_disable_worker", None)
        if worker is not None:
            worker.requestInterruption()
            self._intake_progress.set_stop_enabled(False)

    def _on_disable_item_started(self, index: int, display_name: str) -> None:
        """标记条目正在移除部署，并按已完成数更新批次说明。"""
        panel = self._intake_progress
        panel.update_item(index, display_name, ST_RUNNING, tr("正在移除部署…"))
        total = len(getattr(self, "_disable_entries", ()) or ())
        pct = (self._disable_done / total * 100.0) if total else None
        panel.update_progress(
            pct, tr("正在禁用 {total} 个 Mod", total=total), f"{display_name} ({self._disable_done + 1}/{total})"
        )

    def _on_disable_item_finished(self, index: int, result) -> None:
        """记录单项文件处理结果，成功条目保持待提交状态，整体进度在发布前不达到百分之百。"""
        self._disable_done += 1
        panel = self._intake_progress
        entries = getattr(self, "_disable_entries", ()) or ()
        name = entries[index].display_name if index < len(entries) else ""
        if result is not None and result.ok:
            panel.update_item(index, name, ST_RUNNING, tr("文件已处理，等待提交…"))
        else:
            reason = (result.error if result is not None else "") or tr("失败")
            panel.update_item(index, name, ST_FAILED, reason)
        total = len(entries)
        if total:
            panel.update_progress(
                min(99.0, self._disable_done / total * 100.0),
                tr("正在禁用 {total} 个 Mod", total=total),
                f"{self._disable_done}/{total}",
            )

    @_invalidates_library_probe
    def _on_disable_batch_finished(self, results, cancelled: bool) -> None:
        """发布停用批次结果并保证释放界面状态，收尾异常时保留已提交结果的提示。"""
        worker = getattr(self, "_disable_worker", None)
        try:
            self._publish_disable_results(results, cancelled)
        except Exception as error:
            logger.exception("禁用界面收尾失败")
            self._intake_progress.finish(tr("禁用状态已提交，界面刷新失败"), str(error), ok=False)
        finally:
            self._clear_disable_ui(worker)

    def _publish_disable_results(self, results, cancelled: bool) -> None:
        """在主线程以单个事务提交批量禁用结果，再刷新条目并结算任务进度。"""
        worker = getattr(self, "_disable_worker", None)
        panel = self._intake_progress
        entries = getattr(self, "_disable_entries", ()) or ()
        try:
            disabled = self._mod_manager.commit_disable_batch(results or [])
        except Exception as error:
            logger.exception("禁用状态提交失败")
            panel.finish(tr("禁用状态提交失败"), str(error), ok=False)
            return
        if worker is not None:
            worker.committed = True
        failed = sum(1 for r in (results or []) if r is not None and not r.ok)
        skipped = sum(1 for r in (results or []) if r is None)

        if disabled:
            chars = self._mod_manager.get_characters_for_ui(self._game_service.current_game_id)
            self._overview_page.set_characters(chars, refresh=False)
            mods = [self._mod_manager.get_mod(mid) for mid in disabled]
            mods = [mod for mod in mods if mod is not None]
            self._overview_page.update_mods_in_place(mods)
            self._gallery_page.update_mods_in_place(mods)
            logger.info("批量禁用 Mod 完成: %d 个（失败 %d，未执行 %d）", len(disabled), failed, skipped)

        detail_bits = [tr("成功 {count}", count=len(disabled))]
        if failed:
            detail_bits.append(tr("失败 {failed}", failed=failed))
        if skipped:
            detail_bits.append(tr("未执行 {skipped}", skipped=skipped))
        detail = "，".join(detail_bits)
        if cancelled:
            panel.finish_cancelled(detail)
        else:
            panel.finish(
                tr("已禁用 {count}/{count_2} 个 Mod", count=len(disabled), count_2=len(entries)), detail, ok=not failed
            )
        for index, entry in enumerate(entries):
            if entry.mod_id in disabled:
                panel.update_item(index, entry.display_name, ST_DONE, tr("已禁用"))

    def _clear_disable_ui(self, worker) -> None:
        """释放停用事务保护、断开停止连接并清空线程状态，随后恢复游戏操作和延后任务。"""
        if self._disable_worker is not worker:
            return
        self._mod_manager.release_library_operation("disable-batch")
        try:
            self._intake_progress.stop_requested.disconnect(self._on_disable_stop_requested)
        except TypeError:
            pass
        self._disable_worker = None
        if worker is not None:
            worker.deleteLater()
        self._disable_entries = ()
        self._disable_done = 0
        self._disable_game_id = ""
        self._game_page.setEnabled(True)
        resume = getattr(self, "_resume_deferred_library_environment", None)
        if resume is not None:
            resume()

    def _on_overview_items_export(self, cat_ids: list, mod_ids: list) -> None:
        """OverviewPage「导出」：导出选中的类别（展开子树）+ Mod。

        把选中节点作为导出根、保留其下目录结构（rel_dir 由逻辑类别名链算出）。
        类别展开成子树全部 Mod；与直接选中的 Mod 合并去重。
        """
        items = self._build_export_plan(cat_ids or [], mod_ids or [])
        if not items:
            QMessageBox.information(self, tr("提示"), tr("选中的项目下没有可导出的 Mod。"))
            return
        self._run_export_plan(items)

    def _build_export_plan(self, cat_ids: list, mod_ids: list) -> list[ExportItem]:
        """为导出项目计算相对于目标目录的分类路径。

        保留完整目录结构时使用从资源库根到所属分类的名称链；
        否则从选中的节点开始计算，直接选中的 Mod 平铺输出。
        路径取自数据库中的逻辑分类，按 Mod 编号去重，避免重叠选择重复导出。
        """
        preserve = bool(self._config.get("export.preserve_tree", True))
        chars = self._mod_manager.get_characters()
        char_map = {c.id: c for c in chars}

        def name_chain(root_id, leaf_id: str) -> str:
            """从 leaf_id 沿 category_id 向上取类别名链。
            root_id=None → 一直取到全局顶层根；否则到 root_id（含）为止。"""
            parts: list[str] = []
            cur = leaf_id
            guard = 0
            while cur and cur in char_map and guard < 1000:
                guard += 1
                c = char_map[cur]
                parts.append(c.name or c.name_en or cur)
                if cur == root_id:
                    break
                cur = c.parent_id
            parts.reverse()
            return str(Path(*parts)) if parts else ""

        items: list[ExportItem] = []
        seen: set[str] = set()
        for cid in cat_ids:
            for dcid in self._mod_manager.get_subtree_ids(cid):
                # 勾选 → 从全局根算链；未勾选 → 从选中类别 cid 算链
                rel = name_chain(None, dcid) if preserve else name_chain(cid, dcid)
                for mod in self._mod_manager.get_mods(node_id=dcid):
                    if mod.mod_id in seen:
                        continue
                    seen.add(mod.mod_id)
                    items.append(
                        ExportItem(
                            name=mod.display_name,
                            src_path=mod.path,
                            rel_dir=rel,
                            meta=share_meta_from_mod(mod),
                            preview_paths=list(mod.preview_images or []),
                        )
                    )
        for mid in mod_ids:
            if mid in seen:
                continue
            mod = self._mod_manager.get_mod(mid)
            if mod:
                seen.add(mid)
                # 勾选 → 保留该 Mod 所属类别的完整链；未勾选 → 平铺到 dest 根
                rel = name_chain(None, mod.node_id) if preserve else ""
                items.append(
                    ExportItem(
                        name=mod.display_name,
                        src_path=mod.path,
                        rel_dir=rel,
                        meta=share_meta_from_mod(mod),
                        preview_paths=list(mod.preview_images or []),
                    )
                )
        return items

    def _run_export_plan(self, items: list[ExportItem]) -> None:
        """按导出计划（含 rel_dir）执行导出：选目录 → 右下角进度浮层（非模态）。"""
        dest_dir = QFileDialog.getExistingDirectory(self, tr("选择导出目录"))
        if not dest_dir:
            return
        self._start_export(dest_dir, export_items=items)

    @staticmethod
    def _invalid_metadata_summary(paths: list[str]) -> str:
        """对损坏容器路径去重并生成摘要，最多展示五项，其余数量引导查看日志。"""
        unique_paths = list(dict.fromkeys(paths))
        if not unique_paths:
            return ""
        shown = unique_paths[:5]
        lines = "\n".join(f"- {path}" for path in shown)
        if len(unique_paths) > len(shown):
            lines += tr("\n- 另有 {value} 个，详见日志", value=len(unique_paths) - len(shown))
        return tr("\n\n未恢复的损坏 Mod 容器（{count} 个）：\n{lines}", count=len(unique_paths), lines=lines)

    @_invalidates_library_probe
    def _on_rebuild_index(self) -> None:
        """工具页「重建索引」：确认 → 进度对话框（哈希在后台、DB 在主线程）→ 差异报告。

        见 RebuildProgressDialog：plan/apply 在主线程（ModIndex sqlite 连接线程绑定），
        重活 hash 在 worker 线程并显示进度条，主窗口不冻结。
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        from PySide6.QtWidgets import QMessageBox

        from src.ui.dialogs.rebuild_progress import RebuildProgressDialog

        # 启动时选择暂不恢复后，工具页仍须能隔离坏库，而不是要求先激活游戏。
        if not self._mod_manager.index_healthy:
            self._maybe_recover_index()
            if self._mod_manager.index_healthy:
                self._init_data()
            return

        gid = self._mod_manager.current_game_id
        if not gid:
            QMessageBox.information(self, tr("重建索引"), tr("请先选择游戏。"))
            return
        ret = QMessageBox.question(
            self,
            tr("重建索引"),
            tr(
                "将从文件库重新扫描并重建当前游戏的 Mod 索引。\n"
                "分类、启用态、预设会自动恢复，源文件不受影响。\n\n确定继续？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        dlg = RebuildProgressDialog(self._mod_manager, gid, self)
        dlg.exec()
        if dlg.error:
            QMessageBox.warning(self, tr("重建索引"), tr("重建失败：{error}", error=dlg.error))
            return
        report = dlg.report
        if report is not None:
            invalid_summary = self._invalid_metadata_summary(report.invalid_metadata)
            QMessageBox.information(
                self,
                tr("重建索引"),
                tr(
                    (
                        "重建完成：\n  登记 {registered} 个 Mod\n  已归类 {classified} / "
                        "未分类 {unclassified}{invalid_summary}"
                    ),
                    registered=report.registered,
                    classified=report.classified,
                    unclassified=report.unclassified,
                    invalid_summary=invalid_summary,
                ),
            )
        try:
            self._load_characters(self._mod_manager.current_game_id, force_root=True)
            self._refresh_overview_mods()
        except Exception:
            pass

    def _maybe_recover_index(self) -> None:
        """启动自检分流：结构不兼容/corrupt → 隔离+重建；stale（库被清空但磁盘有数据）→ 直接重建。"""
        try:
            state = self._mod_manager.index_needs_recovery()
        except Exception as e:
            logger.warning("索引自检失败: %s", e)
            return
        if state == "incompatible":
            self._prompt_index_schema_mismatch()
        elif state == "corrupt":
            self._prompt_index_recovery()
        elif state == "stale":
            self._prompt_index_rebuild()

    def _prompt_index_schema_mismatch(self) -> None:
        """结构不符合当前格式时停止读写，保留数据库与源文件。"""
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(
            self,
            tr("索引格式无法使用"),
            tr(
                "当前索引格式无法使用，已停止读写。\n\n原因：{index_schema_error}\n\n"
                "原数据库和源 Mod 文件均已保留。请先备份数据，再检查索引与资源文件的格式。",
                index_schema_error=self._mod_manager.index_schema_error,
            ),
            QMessageBox.StandardButton.Ok,
        )

    def _prompt_index_rebuild(self) -> None:
        """索引可打开但为空、而磁盘文件库仍有数据（被清空/截断/删除）→ 从文件库重建。"""
        from PySide6.QtWidgets import QApplication, QMessageBox

        if not self._game_service.games:
            QMessageBox.information(self, tr("重建索引"), tr("没有可重建的游戏，请先在「游戏」页新建游戏。"))
            return
        ret = QMessageBox.warning(
            self,
            tr("索引为空"),
            tr(
                "检测到文件库中有 Mod / 分类数据，但索引为空（索引可能被清空或丢失）。\n\n"
                "是否从文件库重新扫描重建索引？\n（源 Mod 文件不受影响；角色树将从目录标记 "
                ".d3dxcategory 自动重建）"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            reports = self._mod_manager.rebuild_all_games()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, tr("重建索引"), tr("重建失败：{e}", e=e))
            return
        QApplication.restoreOverrideCursor()
        total = sum(r.registered for r in reports.values())
        invalid = [path for report in reports.values() for path in report.invalid_metadata]
        QMessageBox.information(
            self,
            tr("重建索引"),
            tr(
                "已从文件库重建索引，共登记 {total} 个 Mod，角色树已从目录标记恢复。{value}",
                total=total,
                value=self._invalid_metadata_summary(invalid),
            ),
        )
        try:
            self._load_characters(self._mod_manager.current_game_id, force_root=True)
            self._refresh_overview_mods()
        except Exception:
            pass

    def _prompt_index_recovery(self) -> None:
        """索引 DB 损坏：提示用户从文件库恢复（隔离旧库 + 从 marker 重建树与索引）。"""
        from PySide6.QtWidgets import QMessageBox

        ret = QMessageBox.critical(
            self,
            tr("索引损坏"),
            tr(
                "Mod 索引数据库无法打开（可能已损坏）。\n\n"
                "是否隔离损坏文件并从文件库重新扫描重建？\n（源 Mod 文件不受影响；"
                "角色/类别树将从目录标记 .d3dxcategory 自动重建）"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._recover_index_by_quarantine(tr("索引恢复"))

    def _recover_index_by_quarantine(self, title: str) -> None:
        """隔离损坏索引后从资源库重建；未配置游戏时不创建空库。"""
        from PySide6.QtWidgets import QApplication, QMessageBox

        if not self._game_service.games:
            QMessageBox.information(self, title, tr("没有可重建的游戏，请先在「游戏」页新建游戏。"))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._mod_manager.recover_corrupt_index()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, title, tr("恢复失败：{e}", e=e))
            return
        QApplication.restoreOverrideCursor()
        reports = result.get("reports", {})
        total = sum(r.registered for r in reports.values())
        invalid = [path for report in reports.values() for path in report.invalid_metadata]
        QMessageBox.information(
            self,
            title,
            tr(
                "已隔离原索引文件并重建索引，共重新登记 {total} 个 Mod，角色树已从目录标记恢复。{value}",
                total=total,
                value=self._invalid_metadata_summary(invalid),
            ),
        )
        try:
            self._load_characters(self._mod_manager.current_game_id, force_root=True)
            self._refresh_overview_mods()
        except Exception:
            pass

    def _run_export(self, mods: list) -> None:
        """公共导出流程：选择目标目录 → 右下角进度浮层（格式/结构取自设置）。"""
        dest_dir = QFileDialog.getExistingDirectory(self, tr("选择导出目录"))
        if not dest_dir:
            return
        self._start_export(dest_dir, mods=mods)

    def _start_export(
        self,
        dest_dir: str,
        *,
        mods: list | None = None,
        export_items: list[ExportItem] | None = None,
    ) -> None:
        """启动非模态导出并使用独立任务面板显示进度。

        导出与导入可同时运行；一次只允许一个导出批次，避免同目录产物相互覆盖。
        """
        if self._export_controller is not None:
            QMessageBox.information(self, tr("提示"), tr("已有一批导出正在进行，请等它结束后再导出。"))
            return
        controller = ExportProgressDialog(
            mods or [],
            dest_dir,
            self._mod_manager,
            parent=self,
            export_items=export_items,
            panel=self._export_progress,
        )
        self._export_controller = controller

        def _on_export_finished(_success: int, _total: int) -> None:
            """解除导出控制器引用，并在 Qt 事件循环中释放已结束控制器。"""
            self._export_controller = None
            controller.deleteLater()

        controller.export_finished.connect(_on_export_finished)
        controller.start_export()

    def _on_overview_import_requested(self) -> None:
        """OverviewPage 中触发导入"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if not self._game_service.has_active_game() or self._import_active or self._preset_apply_worker is not None:
            return
        parent_id = self._overview_page._current_parent_id
        if not parent_id:
            QMessageBox.information(self, tr("提示"), tr("请先进入一个类别后再导入 Mod。"))
            return
        dialog = ModImportDialog(
            node_id=parent_id,
            parent=self,
        )
        dialog.import_confirmed.connect(self._do_import_overview)
        dialog.exec()

    def _do_import_overview(self, items: list, node_id: str) -> None:
        """从 OverviewPage 启动通用导入流程。

        成功后的唯一一次总览重建由 ``_do_import`` 的完成回调统一安排；入口层不能
        再追加刷新，否则会在 50ms / 100ms 各触发一次卡片重排。
        """
        self._do_import(items, node_id)

    def _on_overview_files_dropped(self, paths: list, node_id: str) -> None:
        """OverviewPage 拖拽文件导入"""
        call_later(0, self, lambda: self._show_overview_import_dialog(paths, node_id))

    def _show_overview_import_dialog(self, paths: list, node_id: str) -> None:
        """按指定分类和初始路径打开导入对话框，将确认结果连接到概览导入入口。"""
        dialog = ModImportDialog(
            node_id=node_id,
            initial_paths=paths,
            parent=self,
        )
        dialog.import_confirmed.connect(self._do_import_overview)
        dialog.exec()

    @_invalidates_library_probe
    def _on_items_move(self, cat_ids: list, mod_ids: list, target_parent: str) -> None:
        """处理剪切粘贴：移动类别和 Mod"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        moved = 0
        for cid in cat_ids:
            if self._mod_manager.move_character(cid, target_parent):
                moved += 1
        for mid in mod_ids:
            if self._mod_manager.move_mod(mid, target_parent):
                moved += 1
        if moved > 0:
            self._load_characters(self._game_service.current_game_id)
            self._refresh_overview_mods()
            logger.info("移动项目完成: %d 个", moved)

    def _sync_overview_mod(self, mod: Mod) -> None:
        """同步更新 overview 页面中的 Mod 引用（_selected_mod + _current_mods），避免全量刷新"""
        # 更新 _selected_mod
        if self._overview_page._selected_mod and self._overview_page._selected_mod.mod_id == mod.mod_id:
            self._overview_page._selected_mod = mod
        # 同步 _current_mods 中对应 Mod 对象
        for i, m in enumerate(self._overview_page._current_mods):
            if m.mod_id == mod.mod_id:
                self._overview_page._current_mods[i] = mod
                break
        # 同步 _display_items 中对应 Mod 对象
        for item in self._overview_page._display_items:
            if item["type"] == "mod" and item["id"] == mod.mod_id:
                item["data"] = mod
                break

    @_invalidates_library_probe
    def _on_overview_preview_dropped(self, item_id: str, image_path: str) -> None:
        """处理总览页的预览图拖入或卡片图片设置，并就地更新相关卡片。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        mod = self._mod_manager.get_mod(item_id)
        if mod:
            if not self._mod_manager.save_preview(item_id, image_path):
                return
            mod = self._mod_manager.get_mod(item_id)
            if mod and mod.preview_images:
                new_index = len(mod.preview_images) - 1
                if new_index > 0:
                    img = mod.preview_images.pop(new_index)
                    mod.preview_images.insert(0, img)
                    if not self._mod_manager.update_mod(mod):
                        return
                # 就地更新 Mod 引用，避免全量重建卡片。
                self._sync_overview_mod(mod)
                # 同步更新卡片和面板预览
                self._overview_page._update_card_preview_by_path(
                    item_id, mod.preview_images[0] if mod.preview_images else ""
                )
                # 更新预览面板显示 —— 仅当右栏正在看这个 Mod。卡片右键上图可能发生在
                # 多选态下（面板显示的是另一个 Mod），无条件重灌会把右栏内容换错。
                sel = self._overview_page._selected_mod
                if sel is not None and sel.mod_id == item_id:
                    self._overview_page._panel_preview.set_images(mod.preview_images, initial_index=0)
            logger.info("Mod 预览图已保存（Overview）: %s", item_id[:8])

    @_invalidates_library_probe
    def _on_category_preview_dropped(self, cat_id: str, image_path: str) -> None:
        """处理类别预览图的拖入、修改和删除。"""
        if not image_path:
            # 空路径表示删除预览图。
            if not self._mod_manager.update_character_thumbnail(cat_id, ""):
                return
            # 仅更新单个卡片
            card = self._overview_page._vs_card_cache.get(cat_id)
            if card and isinstance(card, CharacterCard):
                card._data["thumbnail"] = ""
                card._cached_pixmap = None
                card._thumb.clear()
                card._thumb.setText(card._data.get("name", ""))
                card._thumb.setWordWrap(True)
                card._thumb.setStyleSheet("font-size: 14px; font-weight: bold;")
            else:
                self._load_characters(self._game_service.current_game_id)
            # 同步内部角色数据
            for c in self._overview_page._all_characters:
                if c.get("id") == cat_id:
                    c["thumbnail"] = ""
                    break
            if (
                self._overview_page._panel_mode == "category"
                and self._overview_page._selected_category
                and self._overview_page._selected_category.get("id") == cat_id
            ):
                self._overview_page._selected_category["thumbnail"] = ""
                self._overview_page._show_category_panel(self._overview_page._selected_category)
            logger.info("类别预览图已删除: %s", cat_id[:8])
            return
        if not Path(image_path).exists():
            return
        # 类别仅一张预览图，直接覆盖
        if not self._mod_manager.update_character_thumbnail(cat_id, image_path):
            return
        # 仅更新单个卡片的预览图，无需全量刷新
        card = self._overview_page._vs_card_cache.get(cat_id)
        if card and isinstance(card, CharacterCard):
            card._data["thumbnail"] = image_path
            if image_path and Path(image_path).exists():
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    card._cached_pixmap = pixmap
                    scaled = pixmap.scaled(
                        card._card_width - 8,
                        card._preview_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation,
                    )
                    card._thumb.setPixmap(scaled)
            else:
                card._cached_pixmap = None
                card._thumb.clear()
                card._thumb.setText(card._data.get("name", ""))
        else:
            # 卡片不在可见缓存中，回退到全量刷新
            self._load_characters(self._game_service.current_game_id)
        # 同步预览面板
        if (
            self._overview_page._panel_mode == "category"
            and self._overview_page._selected_category
            and self._overview_page._selected_category.get("id") == cat_id
        ):
            self._overview_page._selected_category["thumbnail"] = image_path
            self._overview_page._show_category_panel(self._overview_page._selected_category)
        # 同步内部角色数据
        for c in self._overview_page._all_characters:
            if c.get("id") == cat_id:
                c["thumbnail"] = image_path
                break
        logger.info("类别预览图已更新: %s", cat_id[:8])

    @_invalidates_library_probe
    def _on_overview_preview_delete(self, mod_id: str, image_path: str) -> None:
        """在无文件事务冲突时删除指定预览，并同步详情图片列表与卡片封面。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        from src.core.preview_operations import delete_preview_files

        if not delete_preview_files(self._mod_manager, mod_id, [image_path]):
            return
        mod = self._mod_manager.get_mod(mod_id)
        self._sync_overview_mod(mod)
        self._overview_page._panel_preview.set_images(mod.preview_images)
        self._overview_page._update_card_preview_by_path(mod_id, mod.preview_images[0] if mod.preview_images else "")
