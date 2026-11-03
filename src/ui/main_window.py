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
        phase_started = time.perf_counter()
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
        logger.info("启动阶段：核心服务初始化 %.3f 秒", time.perf_counter() - phase_started)
        phase_started = time.perf_counter()
        self._setup_ui()
        logger.info("启动阶段：主界面构建 %.3f 秒", time.perf_counter() - phase_started)
        phase_started = time.perf_counter()
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
        logger.info("启动阶段：运行状态恢复 %.3f 秒", time.perf_counter() - phase_started)

        # 插件系统：发现 + 激活（在数据/游戏就绪后，使插件激活时只读门面可用）
        phase_started = time.perf_counter()
        self._init_plugins()
        logger.info("启动阶段：插件初始化 %.3f 秒", time.perf_counter() - phase_started)
        phase_started = time.perf_counter()
        self._prepare_interaction_dialogs()
        self._directory_cards_prepared = False
        self._directory_card_prepare_timer = QTimer(self)
        self._directory_card_prepare_timer.setSingleShot(True)
        self._directory_card_prepare_timer.timeout.connect(self._prepare_directory_cards)
        self._prepare_directory_cards()
        logger.info("启动阶段：交互资源准备 %.3f 秒", time.perf_counter() - phase_started)
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
        """复用导航命令菜单，每次读取当前贡献并在退出菜单后执行已选回调。"""
        import shiboken6
        from PySide6.QtWidgets import QMenu

        if getattr(self, "_plugin_command_menu_active", False):
            return
        actions = self._build_mod_context_actions("global", "")
        if not actions:
            return
        menu = getattr(self, "_plugin_command_menu", None)
        if menu is None or not shiboken6.isValid(menu):
            menu = QMenu(self)
            self._plugin_command_menu = menu
        self._plugin_command_menu_active = True
        act_map = {}
        callback = None
        try:
            menu.clear()
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
            callback = act_map.get(chosen)
        finally:
            act_map.clear()
            if shiboken6.isValid(menu):
                menu.clear()
            self._plugin_command_menu_active = False
        # QAction 已释放；回调可同步重开菜单，不会覆盖当前选择或保留旧贡献。
        if callback is not None:
            callback()

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
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._nav_container)

        # 先确定最终父对象，避免完整页面加入容器时逐层重算字体和 QSS。
        self._stack = QStackedWidget(self._main_splitter)

        self._overview_page = OverviewPage(self._stack)
        self._overview_page._mod_manager = self._mod_manager  # 注入，供按键面板/override 编辑
        self._stack.addWidget(self._overview_page)  # 0

        # 部署 INI 同步可能来自工作线程，仅发出信号，由主线程刷新按键面板。
        self.ini_synced_from_deploy.connect(self._on_ini_synced_from_deploy)
        self._mod_manager.set_ini_sync_notify(self.ini_synced_from_deploy.emit)

        self._connect_variant_controller()

        self._gallery_page = GalleryPage(self._stack)
        self._stack.addWidget(self._gallery_page)  # 1

        # 恢复上次的成人级Mod预览模式（show|hide|blur），两页保持一致
        _adult_mode = self._config.get("ui.adult_preview_mode", "show")
        self._overview_page.set_adult_mode(_adult_mode)
        self._gallery_page.set_adult_mode(_adult_mode)

        self._settings_page = SettingsPage(self._stack, shortcuts=self._shortcut_settings)
        self._stack.addWidget(self._settings_page)  # 2

        self._tools_page = ToolsPage(self._stack)
        self._stack.addWidget(self._tools_page)  # 3

        # GameBanana Mod Store 页
        self._gb_store_page = self._build_gb_store_page()
        self._stack.addWidget(self._gb_store_page)  # 4

        # 游戏管理页（独立顶级页，PAGE_GAME）
        self._game_page = GamePage(self._stack, save_context=self._game_settings_save)
        self._stack.addWidget(self._game_page)  # 5
        self._game_page.game_changed.connect(self._on_game_changed)
        self._game_page.games_changed.connect(self._on_games_changed)
        self._game_page.deployment_directory_selected.connect(self._on_deployment_directory_selected)
        self._game_page.deploy_dir_cleared.connect(self._on_deploy_dir_cleared)

        # 插件管理页（PAGE_PLUGINS）
        self._plugins_page = PluginsPage(self._stack)
        self._stack.addWidget(self._plugins_page)  # 6

        # 关于页（PAGE_ABOUT，导航底部入口）
        self._about_page = AboutPage(self._stack)
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
            parent=self._stack,
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
        try:
            dlg.exec()
            error, report = dlg.error, dlg.report
        finally:
            # 模态循环等待任务收尾；后续报告弹窗可能投递删除事件，先复制结果。
            dlg.deleteLater()
        if error:
            QMessageBox.warning(self, tr("重建索引"), tr("重建失败：{error}", error=error))
            return
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

    @_invalidates_library_probe
    def _on_overview_preview_delete_all(self, mod_id: str) -> None:
        """删除当前 Mod 的全部预览文件，成功后清空详情预览并更新卡片。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        mod = self._mod_manager.get_mod(mod_id)
        if mod is None or not mod.preview_images:
            return
        from src.core.preview_operations import delete_preview_files

        if not delete_preview_files(self._mod_manager, mod_id, list(mod.preview_images)):
            return
        mod = self._mod_manager.get_mod(mod_id)
        self._sync_overview_mod(mod)
        self._overview_page._panel_preview.clear()
        self._overview_page._update_card_preview_by_path(mod_id, "")

    @_invalidates_library_probe
    def _on_overview_preview_reorder(self, mod_id: str, image_path: str) -> None:
        """将当前预览保存为主图，同时更新索引和元数据。

        以路径定位图像，避免展示顺序与已保存顺序不同。原地更新已选 Mod 的列表，
        保持预览栏和卡片对同一对象的引用。
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if not mod_id or not image_path:
            return
        mod = self._mod_manager.get_mod(mod_id)
        if not mod or not mod.preview_images:
            return
        target = -1
        for i, p in enumerate(mod.preview_images):
            if OverviewPage._preview_paths_equal(p, image_path):
                target = i
                break
        if target <= 0:
            return  # 不在库里，或已经是主图 —— 无需写库
        img = mod.preview_images.pop(target)
        mod.preview_images.insert(0, img)
        if not self._mod_manager.update_mod(mod):
            return
        new_order = list(mod.preview_images)
        sel = self._overview_page._selected_mod
        if sel is not None and sel.mod_id == mod_id:
            sel.preview_images[:] = new_order
        for m in self._overview_page._current_mods:
            if m.mod_id == mod_id:
                m.preview_images[:] = new_order
        for entry in self._overview_page._display_items:
            data = entry.get("data")
            if entry.get("type") == "mod" and getattr(data, "id", "") == mod_id:
                data.preview_images[:] = new_order
        logger.info("Overview 预览图已设为主图: %s", Path(img).name)

    # Mod 操作

    @_invalidates_library_probe
    def _on_gallery_mod_toggle(self, mod_id: str) -> None:
        """将画廊的启停请求转入与总览相同的部署事务入口。"""
        self._on_overview_mod_toggle(mod_id)

    @_invalidates_library_probe
    def _on_mod_edit(self, mod_id: str) -> None:
        """校验 Mod 元数据身份后复用编辑对话框，并由标签事务提交字段与标签修改。

        保存成功后按需同步文件夹名称，刷新受影响条目；重命名或侧车文件同步失败单独提示，保留已提交信息。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        mod = self._mod_manager.get_mod(mod_id)
        if not mod:
            return
        if self._mod_manager.check_mod_operation(mod_id, metadata=True, full=False) is None:
            return
        original_name = mod.name
        dialog = getattr(self, "_mod_edit_dialog", None)
        if dialog is not None and getattr(dialog, "isVisible", lambda: False)():
            dialog.raise_()
            dialog.activateWindow()
            return
        if dialog is None:
            dialog = ModEditDialog(mod, parent=self)
            self._mod_edit_dialog = dialog
        try:
            dialog.bind_mod(mod, tag_service=self._mod_manager.get_tag_service())
        except Exception as exc:
            from src.core.operation_preflight import OperationPreflightError

            if isinstance(exc, OperationPreflightError):
                self._mod_manager.report_operation_failure(exc)
            else:
                QMessageBox.warning(self, tr("无法编辑 Mod"), str(exc))
            return
        dialog.set_save_handler(lambda fields, draft: self._mod_manager.commit_tag_edit(draft, fields))
        if not dialog.exec():
            return
        result = dialog.get_result()
        if not result:
            return
        from src.models.tag import TagCommitResult
        from src.utils import fs

        committed = result.get("tag_commit_result")
        if not isinstance(committed, TagCommitResult):
            logger.error("Mod 编辑未返回有效提交结果：%s", mod_id)
            return
        changed = {saved.mod_id: saved for saved in committed.mods}
        saved = changed.get(mod_id)
        followup_errors = []
        new_name = result.get("name", original_name)
        if saved and new_name and new_name != original_name and result.get("sync_folder", True):
            # 显示元数据已经提交，物理改名独立执行，不重新提交表单。
            old_path = saved.path
            try:
                if not fs.exists(old_path):
                    raise OSError(tr("Mod 文件或文件夹不存在"))
                renamed_mod = self._mod_manager.rename_mod(mod_id, new_name, sync_folder=True)
                if renamed_mod is None:
                    raise OSError(tr("文件或文件夹未能完成重命名"))
                changed[mod_id] = renamed_mod
            except Exception as exc:
                logger.warning("Mod 文件或文件夹重命名失败 (%s)：%s", mod_id, exc)
                followup_errors.append(tr("Mod 文件或文件夹未能重命名。"))
        refresh_tags = getattr(self._overview_page, "refresh_tags", None)
        conditions_changed = False
        if callable(refresh_tags):
            conditions_changed = bool(refresh_tags(refresh_view=False))
        if conditions_changed:
            # 移除活动筛选标签可能显示更多内容，直接重新投影一次，避免先局部重绘。
            self._refresh_overview_mods()
        else:
            self._overview_page.update_mods_in_view(list(changed.values()))
        if committed.sidecar_failed_ids:
            count = len(set(committed.sidecar_failed_ids))
            followup_errors.append(tr("{count} 个 Mod 的信息未能同步到 Mod 文件。", count=count))
        if followup_errors:
            QMessageBox.warning(
                self,
                tr("已保存，部分后续操作未完成"),
                tr("Mod 信息和标签已保存。\n") + "\n".join(followup_errors),
            )
        return
        # 局部差异已更新排序、筛选和详情，原生监控继续处理真实外部变化。

    # 导入

    @_invalidates_library_probe
    def _do_import(self, items: list, node_id: str, *, on_done=None) -> None:
        """在后台执行批量导入，并通过共用任务中心浮层显示进度。

        导入期间用户可继续浏览。完成回调负责解除 ``_import_active`` 状态保护、
        对账源目录，并执行一次总览重建。

        ``on_done(results)``：可选的批次完成回调（商店安装用它做界面收尾），先于
        本方法自己的刷新逻辑执行，无论本批是否有成功项都会调用。
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        from src.ui.dialogs.import_progress import ImportProgressDialog

        controller = ImportProgressDialog(
            items,
            node_id,
            self._mod_manager,
            parent=self,
            panel=self._intake_progress,
        )

        def _on_import_finished(results):
            """清除导入活动状态、交付调用方结果并继续排队安装，成功导入后延后刷新当前分类。"""
            self._import_active = False
            self._import_controller = None
            if on_done is not None:
                try:
                    on_done(results)
                except Exception as exc:
                    logger.warning("导入完成回调失败: %s", exc)
            # 导入已结束，在下一轮事件循环继续处理商店安装队列，避免重入。
            if self._pending_gb_installs:
                call_later(0, self, self._drain_gb_installs)
            imported_count = sum(1 for _, mod in results if mod is not None)
            if imported_count <= 0:
                return
            # 导入完成后已直接刷新页面，抑制同次写入引起的重复监控重建。
            self._suppress_watcher()
            self._watcher_debounce.stop()
            refresh_game_id = self._game_service.current_game_id
            logger.info("批量导入完成: %d 个 Mod", imported_count)
            self._defer_source_reconcile()
            call_later(
                0,
                self,
                lambda gid=refresh_game_id: self._reload_overview_level(gid),
            )

        controller.import_finished.connect(_on_import_finished)
        # 持有控制器，防止异步导入完成前被垃圾回收。
        self._import_controller = controller
        # 暂停 watcher 对账，避免将待压缩或删除的中间目录登记为独立 Mod。
        # 导入完成回调负责解除此状态保护。
        self._import_active = True
        controller.start_import()

    # 商店下载完成后的安装

    def _on_gb_install_requested(self, payload: object) -> None:
        """将商店下载结果送入与手动导入共用的后台流水线，由任务中心显示进度。"""
        if not isinstance(payload, dict):
            return
        if not payload.get("archive_path") or not payload.get("node_id"):
            logger.warning("商店安装请求缺少必要字段，已忽略")
            return
        from uuid import uuid4

        payload = dict(payload)
        metadata = (payload.get("plan") or {}).get("gamebanana_metadata") or {}
        owner = f"install:{uuid4().hex}"
        self._gb_store_page._download_manager.retain(
            int(metadata.get("file_id") or 0),
            str(metadata.get("file_md5") or ""),
            owner=owner,
            path=payload["archive_path"],
        )
        payload["_archive_consumer"] = owner
        self._pending_gb_installs.append(payload)
        self._drain_gb_installs()

    @_invalidates_library_probe
    def _drain_gb_installs(self) -> None:
        """处理一个待安装请求；导入进行中时保留队列，等待完成回调继续处理。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if self._import_active or not self._pending_gb_installs:
            return
        payload = self._pending_gb_installs.pop(0)
        dm = self._gb_store_page._download_manager
        owner = payload["_archive_consumer"]
        # items 第 5 元素 = 透传给 ModIntakeService.plan_request 的额外参数
        # （GameBanana 元数据 / auto_preview / NSFW 分级）；第 6 元素 = 仅显示名，
        # 见 import_progress。
        item = (
            payload.get("archive_path", ""),
            payload.get("display_name", ""),
            "",
            "G",
            dict(payload.get("plan") or {}),
            payload.get("display_label", ""),
        )

        def _done(results: list) -> None:
            """处理商店导入结果与可清理压缩包，并保证释放下载任务的安装所有权。"""
            try:
                mod = next((m for _, m in results if m is not None), None)
                self._maybe_delete_installed_archive(payload, mod)
                store = getattr(self, "_gb_store_page", None)
                if store is not None:
                    try:
                        store.apply_install_result(payload, mod)
                    except RuntimeError:
                        pass  # 页面已销毁
            finally:
                dm.release(owner)

        try:
            self._do_import([item], payload["node_id"], on_done=_done)
        except Exception:
            logger.exception("无法启动商店安装")
            self._import_active = False
            self._import_controller = None
            try:
                _done([])
            finally:
                if self._pending_gb_installs:
                    call_later(0, self, self._drain_gb_installs)

    # 下载数据统计 / 清理

    def _wire_settings_downloads(self, page) -> None:
        """接设置页的下载数据信号。初始构造与**换主题整页重建**各调一次——只接一处的
        表现是「换过主题后清理按钮失灵」。lambda 捕获 page 本身，避免回填到已被替换的旧页。"""
        page.download_usage_requested.connect(lambda p=page: self._on_download_usage_requested(p))
        page.download_purge_requested.connect(lambda p=page: self._on_download_purge_requested(p))
        page.api_cache_clear_requested.connect(self._on_api_cache_clear_requested)

    def _on_api_cache_clear_requested(self) -> None:
        """清理 GbApiCache 的进程内缓存。

        设置页负责删除磁盘缓存，此处同步清除分类、详情、更新和下载量等内存条目。
        客户端尚未初始化时无需处理。
        """
        client = getattr(self, "_gb_client", None)
        cache = getattr(client, "cache", None) if client is not None else None
        if cache is None:
            logger.info("清理缓存：当前没有 GameBanana 客户端实例，跳过内存缓存清理")
            return
        try:
            cache.clear()
            logger.info("清理缓存：GameBanana 接口内存缓存已清空")
        except Exception as e:
            logger.warning("清理 GameBanana 接口内存缓存失败: %s", e)

    def _collect_installed_gb_keys(self) -> tuple[set, set]:
        """收集各游戏已入库的 GameBanana 文件标识和 MD5。

        必须在数据库连接所属线程调用；任一标识匹配即表明对应安装包已经入库。
        """
        ids: set = set()
        md5s: set = set()
        try:
            mods = self._mod_manager.get_mods_all_games()
        except Exception:
            return ids, md5s
        for m in mods:
            gb = (m.info.custom_data or {}).get("gamebanana") or {}
            try:
                fid = int(gb.get("file_id") or 0)
            except (TypeError, ValueError):
                fid = 0
            if fid:
                ids.add(fid)
            md5 = str(gb.get("file_md5") or "").strip().lower()
            if md5:
                md5s.add(md5)
        return ids, md5s

    def _scan_download_usage(self):
        """收集已安装来源标识和哈希后扫描下载占用，区分仍被安装记录引用的文件。"""
        from src.services.gamebanana.download_store import scan_downloads

        ids, md5s = self._collect_installed_gb_keys()
        return scan_downloads(ids, md5s)

    def _on_download_usage_requested(self, page=None) -> None:
        """设置页只排队；全库元数据读取和下载目录扫描均在后台完成。"""
        page = page or getattr(self, "_settings_page", None)
        if page is None:
            return
        from src.ui.download_usage_loader import DownloadUsageLoader
        from src.utils.paths import get_data_dir, get_gb_download_dir

        loader = getattr(page, "_download_usage_loader", None)
        if loader is None:
            loader = page._download_usage_loader = DownloadUsageLoader(page)
            loader.ready.connect(page.set_download_usage)
        loader.request(get_data_dir() / "mod_index.db", get_gb_download_dir())

    def _on_download_purge_requested(self, page=None) -> None:
        """清理已入库源包；仍有下载、更新或安装占用的文件交给下载器延后删除。"""
        page = page or getattr(self, "_settings_page", None)
        # 删除前重新扫描，使用当前文件状态核实清理目标。
        usage = self._scan_download_usage()
        dm = getattr(getattr(self, "_gb_store_page", None), "_download_manager", None)
        count = freed = 0
        if dm is not None:
            for entry in usage.entries:
                if not entry.installed:
                    continue
                try:
                    if not entry.path.is_file():
                        continue
                    dm.delete_archive_when_unused(entry.path)
                    # 延后删除的源包仍占空间，不能计入本次释放结果。
                    if entry.path.exists():
                        continue
                except OSError as exc:
                    logger.warning("清理下载文件失败 %s: %s", entry.path.name, exc)
                    continue
                count += 1
                freed += entry.size
        self._refresh_download_local_index()
        if page is not None:
            page.set_download_purge_result(count, freed)

    def _refresh_download_local_index(self) -> None:
        """下载文件经外部清理后重新扫描缓存索引，避免安装时引用已不存在的文件。

        下载管理器尚未创建时无需处理，其初始化会建立当前索引。
        """
        dm = getattr(getattr(self, "_gb_store_page", None), "_download_manager", None)
        if dm is None or not hasattr(dm, "refresh_local_index"):
            return
        try:
            dm.refresh_local_index()
        except Exception as e:
            logger.warning("刷新下载索引失败: %s", e)

    def _maybe_delete_installed_archive(self, payload: dict, mod) -> None:
        """按配置清理安装成功后的下载源包。

        仅在自动清理已开启、返回有效入库 Mod 且源路径属于共享下载目录时处理。
        未返回 Mod 的重复或失败结果不自动删除，已安装的重复源包可由下载清理统一处理。
        """
        if mod is None:
            return
        if not self._config.get("gamebanana.delete_archive_after_install", True):
            return
        raw = str((payload or {}).get("archive_path") or "")
        if not raw:
            return
        dm = getattr(getattr(self, "_gb_store_page", None), "_download_manager", None)
        if dm is not None:
            dm.delete_archive_when_unused(Path(raw))

    # 画廊

    def _refresh_gallery(self) -> None:
        # 无激活游戏：直接清空。不能走 get_mods——空 game_id 会被底层当「全库」返回所有游戏的
        # Mod（mod_index.get_all_mods 在 gid 为空时 SELECT * 不带 game 过滤）→ 残留别的游戏数据。
        # 画廊与总览不同：每次切到画廊 _on_nav_changed 都会重拉，故这里的拦截是必须的。
        """无激活游戏时取消加载并清空画廊，否则为局部事件让行后安排分片读取。"""
        if not self._game_service.has_active_game():
            self._gallery_refresh.cancel()
            self._gallery_page.set_mods([])
            return
        self._library_probe.yield_to_local_events()
        self._gallery_refresh.schedule()

    def _persist_adult_mode(self, mode: str) -> None:
        """成人级Mod预览模式变更 → 写入 config，下次启动恢复。"""
        self._config.set("ui.adult_preview_mode", mode)

    def _on_gallery_mod_selected(self, mod_id: str) -> None:
        """画廊中点击 Mod → 导航到 Mod 所在目录并选中该 Mod"""
        mod = self._mod_manager.get_mod(mod_id)
        if not mod or not mod.node_id:
            return
        # 按页面标识定位总览导航项，避免依赖导航列表的固定行号。
        self._select_nav_page(self.PAGE_OVERVIEW)
        # 导航到 Mod 所在的父类别目录
        parent_id = mod.node_id
        self._overview_page._current_parent_id = parent_id
        # 加载该层级的 Mod
        mods = self._mod_manager.get_mods(node_id=parent_id) if parent_id else []
        subtree_mods = self._collect_subtree_mods(parent_id)
        self._overview_page.set_current_mods(mods, subtree_mods)
        # 选中该 Mod
        self._overview_page._selected_ids.clear()
        self._overview_page._selected_ids.add(mod_id)
        self._overview_page._selected_mod = mod
        self._overview_page._selected_category = None
        self._overview_page._show_mod_panel(mod)
        self._overview_page._update_card_selection()
        # 滚动到选中的 Mod 位置（延迟执行以确保布局已更新）
        call_later(0, self, lambda: self._overview_page.scroll_to_item(mod_id))

    # 设置

    @_invalidates_library_probe
    def _on_settings_changed(self) -> None:
        """响应实时保存的设置变更。

        配置单例已包含最新值；代理和定时器更新通过 150 毫秒单次定时器合并，
        避免连续操作重复读取系统代理和重启定时器。
        """
        # 并发数调大后立即补齐名额；调小仅约束后续启动，不打断进行中的部署。
        self._start_pending_deploys()
        update_service = getattr(self, "_mod_update_service", None)
        if update_service is not None:
            update_service.refresh_update_concurrency()
        monitor = getattr(self, "_image_memory_monitor", None)
        if monitor is not None:
            monitor.apply_settings()
        from PySide6.QtCore import QTimer

        if not hasattr(self, "_settings_apply_timer"):
            self._settings_apply_timer = QTimer(self)
            self._settings_apply_timer.setSingleShot(True)
            self._settings_apply_timer.timeout.connect(self._apply_settings_reconcile)
        self._settings_apply_timer.start(150)

    @_invalidates_library_probe
    def _apply_settings_reconcile(self) -> None:
        """合并后的设置重对账：定时检查间隔 + 代理热生效（仅在真正变化时换 client）。"""
        if hasattr(self, "_mod_update_service"):
            self._apply_periodic_update_timer()
        store = getattr(self, "_gb_store_page", None)
        if store is not None and hasattr(store, "apply_proxy"):
            from src.utils.proxy import get_active_proxy_url

            store.apply_proxy(get_active_proxy_url(self._config))

    # 外观实时应用（字体 / 主题 / 背景）+ 「正在应用」通知
    def _ensure_toast(self):
        """懒创建并复用绑定到主窗口内容区域的提示浮层。"""
        t = getattr(self, "_toast", None)
        if t is None:
            from src.ui.widgets.toast import Toast

            # 提示层放入视频前景合成范围，避免透明边角与窗口底色合成。
            t = Toast(self.centralWidget() or self)
            self._toast = t
        return t

    def _ensure_appearance_queue(self):
        """懒创建串行外观更新队列，并与主窗口提示浮层关联。"""
        queue = getattr(self, "_appearance_queue", None)
        if queue is None:
            from src.ui.appearance import AppearanceQueue

            queue = AppearanceQueue(self, self._ensure_toast())
            self._appearance_queue = queue
        return queue

    def _toast_done(self, text: str) -> None:
        """尝试显示外观操作完成提示，提示控件失效时忽略显示异常。"""
        try:
            self._ensure_toast().done(text)
        except Exception:
            pass

    def _on_font_changed(self, family: str) -> None:
        """把字体变更加入可合并外观队列，通过应用回调报告完成。"""
        from src.app import App

        self._ensure_appearance_queue().submit(
            "font",
            t("正在应用字体…"),
            t("字体已应用"),
            lambda finish: App.instance().apply_font(family, on_finished=finish),
        )

    def _on_theme_changed(self, theme: str) -> None:
        """把主题变更加入外观队列，串行应用主题并显示处理状态。"""
        self._ensure_appearance_queue().submit(
            "theme",
            t("正在应用主题…"),
            t("主题已应用"),
            lambda finish: self._do_theme_change(theme, finish),
        )

    def _do_theme_change(self, theme: str, on_finished=None) -> None:
        """请求应用切换主题，完成后依据实际变化决定刷新页面或等待现有材质就绪。"""
        import shiboken6

        from src.app import App

        finish = on_finished or (lambda: self._toast_done(t("主题已应用")))

        def applied(changed: bool) -> None:
            """仅在窗口有效时处理主题应用结果，变化时继续分阶段刷新界面。"""
            if (not shiboken6.isValid(self)) or getattr(self, "_appearance_disposed", False):
                return
            if changed:
                self._post_appearance_refresh(t("主题已应用"), on_finished=finish)
            else:
                self._when_appearance_ready(finish)

        App.instance().apply_theme(theme, on_finished=applied)
        logger.info("主题已切换: %s", theme)

    def _on_background_changed(self) -> None:
        # 图片加载自行处理最新请求，清除、禁用和替换立即生效，不等待外观队列。
        """立即同步背景遮罩，并将完整背景变更加到串行外观队列。"""
        self._refresh_bg_overlay()
        self._ensure_appearance_queue().submit(
            "background",
            t("正在应用背景…"),
            t("背景已应用"),
            self._do_background_change,
        )

    def _do_background_change(self, on_finished=None) -> None:
        """保存原主题并请求背景更新，完成后按颜色是否变化决定刷新范围，同时安排卡片预热。"""
        import shiboken6

        from src.ui import theme

        previous_theme = theme.current_theme()
        finish = on_finished or (lambda: self._toast_done(t("背景已应用")))

        def applied(changed: bool) -> None:
            """核验窗口生命周期后刷新受背景影响的界面，未变化时等待材质就绪再完成。"""
            if (not shiboken6.isValid(self)) or getattr(self, "_appearance_disposed", False):
                return
            if changed:
                self._post_appearance_refresh(
                    t("背景已应用"),
                    on_finished=finish,
                    colors_changed=previous_theme != theme.current_theme(),
                    refresh_background=False,
                )
            else:
                # 仅更换图片时等待解码，不重新生成主题、图标或页面样式。
                self._when_appearance_ready(finish)

        self.apply_background(on_applied=applied)
        MainWindow._schedule_directory_card_preparation(self)

    def _when_appearance_ready(self, on_finished) -> None:
        """等待背景覆盖层准备完成后执行回调；无覆盖层时直接完成。"""
        overlay = getattr(self, "_bg_overlay", None)
        if overlay is not None:
            overlay.when_ready(on_finished)
        else:
            on_finished()

    def _post_appearance_refresh(
        self, toast_label: str, *, on_finished=None, colors_changed=True, refresh_background=True
    ) -> None:
        """只有主题/透明材质变化才重涂，分步执行并惰性刷新不可见页面。

        颜色/材质选择器已由 GUI 线程分批应用；这里只更新内联样式和图标。
        """
        import shiboken6

        from src.ui import acrylic, icons

        if (not shiboken6.isValid(self)) or getattr(self, "_appearance_disposed", False):
            return
        # 立即更新当前页面，其余页面在下次显示前刷新。
        self._pending_retheme = {
            self.PAGE_GAME,
            self.PAGE_OVERVIEW,
            self.PAGE_GALLERY,
            self.PAGE_TOOLS,
            self.PAGE_ABOUT,
            self.PAGE_PLUGINS,
        }
        cur = self._stack.currentIndex()
        self._pending_retheme.discard(cur)

        def _flyout() -> None:
            """刷新插件界面、下载浮层、激活提示和各任务面板的主题样式。"""
            plugin_host = getattr(self, "_plugin_ui_host", None)
            if plugin_host is not None:
                plugin_host.retheme()
            store = getattr(self, "_gb_store_page", None)
            if store is not None and hasattr(store, "download_flyout"):
                store.download_flyout.retheme()
            for panel in (
                getattr(self, "_lock_overlay", None),
                getattr(self, "_intake_progress", None),
                getattr(self, "_export_progress", None),
                getattr(self, "_mod_update_progress", None),
                getattr(self, "_plugin_progress", None),
            ):
                if panel is not None:
                    panel.retheme()

        steps = [acrylic.reset_for_theme_change]
        if colors_changed:
            steps.extend((lambda: icons.retheme_icons(self), self._refresh_themed_icons))
        if refresh_background:
            steps.append(self._refresh_bg_overlay)
        steps.extend(
            [
                lambda: self._retheme_one_page(cur),  # 仅当前可见页
                _flyout,
            ]
        )

        def _finish() -> None:
            """窗口仍有效时等待背景就绪，再交付外观完成回调或显示提示。"""
            if shiboken6.isValid(self) and not getattr(self, "_appearance_disposed", False):
                self._when_appearance_ready(on_finished or (lambda: self._toast_done(toast_label)))

        def _run(i: int) -> None:
            """逐阶段刷新外观并在步骤间让出事件循环，结束后重建设置页；销毁窗口立即终止。"""
            if (not shiboken6.isValid(self)) or getattr(self, "_appearance_disposed", False):
                return
            if i >= len(steps):
                self._rebuild_settings_page(on_finished=_finish)
                return
            try:
                steps[i]()
            except Exception:
                pass
            call_later(8, self, lambda: _run(i + 1))

        _run(0)

    def _retheme_one_page(self, idx: int) -> None:
        """重涂指定页的内联样式 / 卡片（按页类型分派）。供惰性重涂调用。"""
        try:
            if idx == self.PAGE_GAME:
                self._game_page.retheme()
            elif idx == self.PAGE_OVERVIEW:
                self._overview_page.retheme()
                if self._game_service.has_active_game():
                    self._load_characters(self._game_service.current_game_id)
                    self._refresh_overview_mods()
            elif idx == self.PAGE_GALLERY:
                self._gallery_page.refresh_from_index()
            elif idx == self.PAGE_TOOLS:
                self._tools_page.retheme()
            elif idx == self.PAGE_ABOUT:
                self._about_page.retheme()
            elif idx == self.PAGE_PLUGINS:
                self._plugins_page.retheme()
        except Exception:
            pass

    def _rebuild_settings_page(self, *, on_finished=None) -> None:
        """换肤后刷新设置页。

        `SettingsPage.retheme` 就地重涂登记过的内联样式；实时切换用回调等待分批刷新。
        仅当该页没有 retheme 或调用失败才回退到重建。图标由 `icons.retheme_icons` 统一重涂。
        """
        page = getattr(self, "_settings_page", None)
        if page is None:
            if on_finished is not None:
                on_finished()
            return
        if hasattr(page, "retheme"):
            try:
                if on_finished is None:
                    page.retheme()
                else:
                    page.retheme(on_finished=on_finished)
                return
            except Exception:
                pass
        self._rebuild_settings_page_full()
        if on_finished is not None:
            on_finished()

    def _rebuild_settings_page_full(self) -> None:
        """整页重建设置页（回退路径）。"""
        old = getattr(self, "_settings_page", None)
        if old is None:
            return
        # 正在跑代理测试线程时不重建——线程父挂在旧页上，删旧页会连带删运行中的线程→崩。
        prev = getattr(old, "_proxy_test_thread", None)
        if prev is not None:
            try:
                if prev.isRunning():
                    return
            except RuntimeError:
                pass
        idx = self._stack.indexOf(old)
        if idx < 0:
            return
        from src.ui.pages.settings import SettingsPage

        was_current = self._stack.currentIndex() == idx
        new = SettingsPage(shortcuts=self._shortcut_settings)
        new.settings_changed.connect(self._on_settings_changed)
        new.theme_changed.connect(self._on_theme_changed)
        new.font_changed.connect(self._on_font_changed)
        new.language_changed.connect(self._on_language_changed)
        new.background_changed.connect(self._on_background_changed)
        new.bg_overlay_refresh.connect(self._refresh_bg_overlay)
        new.bg_playback_requested.connect(self._background_controller.playback)
        new.bg_scrim_live.connect(self._on_bg_scrim_live)
        new.bg_blur_live.connect(self._on_bg_blur_live)
        new.app_update_check_requested.connect(self._on_app_update_check_requested)
        self._wire_settings_downloads(new)  # 整页重建后必须重接，否则清理按钮失灵
        self._stack.insertWidget(idx, new)
        self._stack.removeWidget(old)
        old.deleteLater()
        self._settings_page = new
        self._refresh_bg_overlay()
        if was_current:
            self._stack.setCurrentWidget(new)

    def _refresh_themed_icons(self) -> None:
        """主题切换后重新着色导航栏图标（apply_theme 已清空图标缓存）。"""
        for i, (icon_name, _label, _idx) in enumerate(self._nav_items_data):
            item = self._nav.item(i)
            if item is not None:
                icons.apply_icon(item, icon_name)
        icons.apply_icon(self._launch_loader_btn, "actions/launch")
        icons.apply_icon(self._launch_game_btn, "actions/game")
        self._collapse_btn.setIcon(icons.get_icon("actions/chevron-right" if self._nav_collapsed else "actions/menu"))

    def _on_language_changed(self, lang: str) -> None:
        """语言确有变化时提示设置已保存且将在重启后生效。"""
        from src.utils.i18n import get_i18n
        from src.utils.language_pack import normalize_language_code

        if normalize_language_code(lang) == get_i18n().current_language:
            return
        QMessageBox.information(
            self,
            t("提示"),
            t("语言设置已保存，重启软件后生效。"),
        )

    def _apply_migoto_for_current_game(self) -> None:
        """根据当前游戏配置重新指向加载器与 Mod 部署目录（各游戏相互独立）。

        部署目录取显式配置的 mods_path；未配置时禁止启用 Mod 和切换预设。
        3DMigoto 的实际 Mod 目录可由 d3dx.ini 改写，不能仅凭 migoto_path 推导。
        migoto_path 用于启动游戏专属加载器，exe_path 用于启动游戏。
        """
        if getattr(self, "_game_settings_saving", False):
            self._deploy_environment_dirty = True
            return
        if getattr(self, "_preset_delete_preparing", False) or getattr(self, "_preset_delete_worker", None) is not None:
            self._deploy_environment_dirty = True
            return
        variants = getattr(self, "_variant_controller", None)
        if variants is not None and variants.busy:
            self._deploy_environment_dirty = True
            return
        if _plugin_operation_busy(self):
            self._deploy_environment_dirty = True
            return
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and publication.busy:
            self._deploy_environment_dirty = True
            return
        if (
            self._preset_apply_worker is not None
            or getattr(self, "_preset_apply_preparing", False)
            or getattr(self, "_disable_worker", None) is not None
        ):
            self._deploy_environment_dirty = True
            return
        if self._deploy_workers or self._deploy_active:
            # 存储实例由在途 worker 共用，整批完成前不能修改它的目标目录。
            self._deploy_environment_dirty = True
            return
        root_watch = getattr(self, "_deployment_root_watch", None)
        resolution = root_watch.check_current() if root_watch is not None else None
        game = self._game_service.get_current_game()
        migoto_path = game.migoto_path if game and game.migoto_path else ""
        deploy_dir = game.mods_path if game and game.mods_path else ""

        self._migoto.migoto_path = migoto_path or None
        self._migoto.game_exe_path = game.exe_path if game and game.exe_path else None
        self._update_launch_buttons()
        if resolution is not None and resolution.status == "missing":
            self._on_deployment_root_invalid(game.id, deploy_dir)
            return
        if resolution is not None and resolution.status == "moved":
            self._deploy_environment_dirty = True
            return
        self._overview_page._deploy_status_label.setProperty("deploymentRootError", "")
        if not self._deployment_activation.is_current(deploy_dir):
            probe = getattr(self, "_library_probe", None)
            if probe is not None:
                probe.reset_failure()
                probe.cancel()
            self._deploy_watcher.stop()
            self._deploy_state_poll.stop()
            self._overview_page._deploy_status_label.setText(tr("正在准备游戏环境…"))
            self._overview_page._deploy_status_label.show()
            self._deployment_activation.start(deploy_dir)
        # d3dx_user.ini 的位置跟着 migoto_path 走（还含 d3dx.ini 的 [Include]
        # user_config）→ 切游戏 / 改 3DMigoto 路径都必须在这个咽喉点重新指向监视目标。
        try:
            self._mod_manager.variants.refresh_watch()
        except Exception as e:
            logger.debug("重设 d3dx_user.ini 监视失败: %s", e)

    def _on_deployment_root_relocated(self, game_id, old, new) -> None:
        """当前部署根迁移后撤销旧监控与探测，安排环境重建并刷新游戏配置页面。"""
        if game_id == self._game_service.current_game_id:
            self._deploy_watcher.stop()
            self._deploy_watcher_debounce.stop()
            self._deploy_changed_paths.clear()
            self._deployment_events = []
            self._library_probe.reset_failure()
            self._library_probe.cancel()
            self._deployment_root_notice = None
            call_later(0, self, self._apply_migoto_for_current_game)
            self._ensure_toast().done(tr("部署目录已自动更新：{new}", new=new), ok=True, hold_ms=5000)
        self._game_page.reload()

    def _on_deployment_root_invalid(self, game_id, path) -> None:
        """取消无效根的激活；文件事务忙碌时延后处理，空闲时暂停部署并去重提示。"""
        if game_id != self._game_service.current_game_id:
            return
        self._deployment_activation.cancel()
        if (
            _deployment_operation_busy(self)
            or _plugin_operation_busy(self)
            or self._deploy_workers
            or self._deploy_active
        ):
            self._deploy_environment_dirty = True
            return
        message = tr("部署目录无效，请重新选择")
        self._suspend_deployment_root(message)
        if getattr(self, "_deployment_root_notice", None) != (game_id, path):
            self._deployment_root_notice = (game_id, path)
            self._show_deployment_root_notice(message, path)

    def _show_deployment_root_notice(self, message: str, path: str) -> None:
        """关闭旧提示后非阻塞显示原部署路径与重新选择入口，结束时清除引用。"""
        previous = getattr(self, "_deployment_root_dialog", None)
        if previous is not None:
            previous.close()
        notice = QMessageBox(self)
        notice.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        notice.setIcon(QMessageBox.Icon.Warning)
        notice.setWindowTitle(tr("部署目录无效"))
        notice.setText(message)
        notice.setInformativeText(
            tr("原部署目录：{path}\n\n请前往「游戏」页编辑当前游戏，重新选择「Mod 文件夹」。", path=path)
        )
        notice.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._deployment_root_dialog = notice
        notice.finished.connect(lambda: setattr(self, "_deployment_root_dialog", None))
        notice.open()

    def _suspend_deployment_root(self, message: str) -> None:
        """保留部署记录，但禁止通过失效目录绑定写入。"""
        self._deploy_watcher.stop()
        self._deploy_state_poll.stop()
        self._deploy_watcher_debounce.stop()
        self._deploy_changed_paths.clear()
        self._deployment_events = []
        if self._mod_manager._storage.get_deploy_dir() is not None:
            self._mod_manager.set_deploy_dir("", defer_activation=True)
        if not self._library_probe.paused or self._library_probe.last_error != message:
            self._library_probe.pause(message)
        label = self._overview_page._deploy_status_label
        label.setProperty("deploymentRootError", message)
        label.setText(message)
        label.show()

    def _on_deployment_root_available(self, game_id) -> None:
        """当前部署根恢复时撤销错误提示与探测暂停，并重新应用游戏部署环境。"""
        if game_id == self._game_service.current_game_id:
            self._deployment_root_notice = None
            notice = getattr(self, "_deployment_root_dialog", None)
            if notice is not None:
                notice.close()
            self._library_probe.reset_failure()
            call_later(0, self, self._apply_migoto_for_current_game)

    def _on_deployment_root_failed(self, game_id, error) -> None:
        """当前游戏根路径更新失败时取消激活、暂停部署并显示错误。"""
        if game_id == self._game_service.current_game_id:
            self._deployment_activation.cancel()
            self._suspend_deployment_root(tr("更新部署目录失败：{error}", error=error))
            self._ensure_toast().done(tr("更新部署目录失败：{error}", error=error), ok=False, hold_ms=8000)

    def _on_deployment_activation_settled(self, game_id, result) -> None:
        """当前游戏激活成功后恢复部署监控、刷新变更条目，并继续库探测和延后任务。"""
        if game_id != self._game_service.current_game_id:
            return
        root = self._mod_manager._storage.get_deploy_dir()
        if root:
            self._deploy_watcher.start(root)
            self._deploy_state_poll.start()
        self._overview_page._refresh_deploy_summary()
        if result.changed_ids:
            mods = [mod for mid in result.changed_ids if (mod := self._mod_manager.get_mod(mid)) is not None]
            self._overview_page.update_mods_in_place(mods)
        self._library_probe.schedule()
        self._resume_deferred_library_environment()

    def _on_deployment_activation_failed(self, game_id, error) -> None:
        """当前游戏激活失败时暂停库探测、刷新部署摘要并提示准备错误。"""
        if game_id == self._game_service.current_game_id:
            self._library_probe.pause(error)
            self._overview_page._refresh_deploy_summary()
            self._ensure_toast().done(tr("游戏环境准备未完成：{error}", error=error), ok=False, hold_ms=6000)

    def _on_deployment_activation_discarded(self, game_id) -> None:
        """当前游戏激活结果过期时，在下一轮事件循环重新准备环境。"""
        if game_id == self._game_service.current_game_id:
            call_later(0, self, self._apply_migoto_for_current_game)

    def _on_deployment_activation_stopped(self) -> None:
        """解除激活线程关闭等待标记，并异步继续关闭主窗口。"""
        if self._deployment_activation_close_pending:
            self._deployment_activation_close_pending = False
            call_later(0, self, self.close)

    def _on_enable_publication_stopped(self) -> None:
        """解除启用发布关闭等待标记，并异步继续窗口关闭流程。"""
        if getattr(self, "_enable_publication_close_pending", False):
            self._enable_publication_close_pending = False
            call_later(0, self, self.close)

    def _on_enable_publication_idle(self) -> None:
        """启用结果发布空闲后继续排队请求，并在部署结束时恢复游戏页和延后操作。"""
        publication = self._enable_publication
        if not publication.closing and getattr(self, "_deferred_enable_ids", {}):
            self._schedule_deferred_enables()
        if publication.closing or self._deploy_workers or self._deploy_active:
            return
        self._deploy_stopping = False
        self._deploy_close_pending = False
        self._deploy_game_id = ""
        self._game_page.setEnabled(not (_deployment_operation_busy(self) or _plugin_operation_busy(self)))
        self._game_page.setToolTip("")
        self._resume_deferred_library_environment()

    def _schedule_deferred_enables(self) -> None:
        """无冲突且未在处理队列时，合并安排下一轮启用请求处理。"""
        if (
            getattr(self, "_deferred_enable_ids", {})
            and not getattr(self, "_deferred_enable_scheduled", False)
            and not getattr(self, "_deploy_conflict_mod_id", "")
            and not getattr(self, "_draining_deferred_enables", False)
        ):
            self._deferred_enable_scheduled = True
            call_later(0, self, self._drain_deferred_enables)

    def _drain_deferred_enables(self) -> None:
        """前一配置提交后重新核对冲突和路径。"""
        self._deferred_enable_scheduled = False
        if getattr(self, "_draining_deferred_enables", False) or getattr(self, "_deploy_conflict_mod_id", ""):
            return
        pending = getattr(self, "_deferred_enable_ids", {})
        if not pending or self._enable_publication.closing or self._deploy_stopping:
            return
        if _deployment_operation_busy(self, include_publication=False) or _plugin_operation_busy(self):
            return
        mod_id = next(iter(pending))
        game_id = pending[mod_id]
        mod = self._mod_manager.get_mod(mod_id)
        if (
            game_id == self._game_service.current_game_id
            and mod is not None
            and not mod.is_enabled
            and (self._enable_publication.busy or self._deploy_workers)
            and self._enable_waits_for_publication(mod)
        ):
            return
        self._draining_deferred_enables = True
        try:
            pending.pop(mod_id)
            if game_id == self._game_service.current_game_id:
                mod = self._mod_manager.get_mod(mod_id)
                if mod is not None and not mod.is_enabled:
                    self._on_overview_mod_toggle(mod_id)
            if mod_id not in self._deploy_workers and mod_id not in pending:
                mod = self._mod_manager.get_mod(mod_id)
                success = mod is not None and mod.is_enabled
                self._overview_page.finish_deploy_overlay(
                    mod_id, success=success, error="" if success else tr("已取消排队的启用请求")
                )
        finally:
            self._draining_deferred_enables = False
            self._schedule_deferred_enables()

    def _resume_deferred_library_environment(self) -> None:
        """部署事务和界面结算后重放等待中的操作。"""
        activation = getattr(self, "_deployment_activation", None)
        if activation is not None and activation.closing:
            return
        if _deployment_operation_busy(self) or _plugin_operation_busy(self):
            return
        if self._deploy_environment_dirty:
            self._deploy_environment_dirty = False
            call_later(0, self, self._apply_migoto_for_current_game)
        if getattr(self, "_plugin_variant_pending", False):
            call_later(0, self, self._resume_pending_variant_capture)
        if self._watcher_changed_paths:
            self._watcher_debounce.start()
        if self._pending_gb_installs:
            call_later(0, self, self._drain_gb_installs)
        if getattr(self, "_deferred_enable_ids", {}):
            self._schedule_deferred_enables()

    def _update_launch_buttons(self) -> None:
        """按当前游戏配置更新加载器和游戏启动按钮。

        加载器按钮仅在对应可执行文件存在时启用；由外部启动器注入的环境
        仍可使用部署及变体功能，无需提供独立加载器程序。
        """
        if not hasattr(self, "_launch_loader_btn"):
            return
        game = self._game_service.get_current_game()
        name = game.name if game else ""
        has_path = bool(self._migoto.migoto_path)
        has_loader = self._migoto.has_loader
        has_exe = bool(self._migoto.game_exe_path)
        self._launch_loader_btn.setEnabled(has_loader)
        self._launch_game_btn.setEnabled(has_exe)
        if has_loader:
            loader_tip = t("启动「{game}」的 Mod 加载器", game=name)
        elif has_path:
            loader_tip = f"{t('文件不存在或无法读取')}\n{self._migoto.migoto_path / MIGOTO_LOADER_NAME}"
        else:
            loader_tip = t("当前游戏未配置 3DMigoto / 加载器路径")
        self._launch_loader_btn.setToolTip(loader_tip)
        self._launch_game_btn.setToolTip(t("启动「{game}」", game=name) if has_exe else t("当前游戏未配置可执行文件"))

    def _on_launch_migoto(self) -> None:
        """启动当前游戏配置的 Mod 加载器（3DMigoto Loader）。"""
        if not self._migoto.launch_migoto():
            QMessageBox.warning(
                self, t("错误"), t("启动 Mod 加载器失败，请到「游戏」页编辑当前游戏，检查 3DMigoto 路径。")
            )

    def _on_launch_game(self) -> None:
        """启动当前游戏（使用「游戏管理」中的可执行文件配置）。"""
        if not self._migoto.launch_game():
            QMessageBox.warning(self, t("错误"), t("启动游戏失败，请到「游戏」页编辑当前游戏，检查可执行文件路径。"))

    # 工具

    def _show_category_preset_recovery_notice(self) -> None:
        """仅创建一次恢复失败提示，列出仍受保护的分类预设目录。"""
        paths = getattr(self._mod_manager, "category_preset_recovery_errors", ())
        if not paths or getattr(self, "_category_preset_recovery_notice", None) is not None:
            return
        notice = QMessageBox(self)
        notice.setObjectName("categoryPresetRecoveryNotice")
        notice.setIcon(QMessageBox.Icon.Warning)
        notice.setWindowTitle(tr("分类预设恢复未完成"))
        notice.setText(tr("部分分类预设操作尚未完成恢复。"))
        notice.setInformativeText(tr("相关目录继续受保护，暂不自动纳管。可展开详细信息查看路径；重启软件后会重试。"))
        notice.setDetailedText("\n".join(paths))
        notice.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._category_preset_recovery_notice = notice
        notice.show()

    def _open_category_presets(self, *, start_import: bool = False, start_export: bool = False) -> None:
        """复用或异步打开分类预设窗口；库操作忙碌时拒绝创建，避免并发修改。"""
        dialog = getattr(self, "_category_preset_dialog", None)
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        if _plugin_operation_busy(self) or self._host_library_busy():
            self._ensure_toast().done(tr("当前任务完成后可管理分类预设"), ok=False, hold_ms=2500)
            return
        from src.ui.dialogs.category_preset import CategoryPresetDialog

        try:
            dialog = CategoryPresetDialog(
                self._mod_manager,
                self._game_service.current_game_id,
                self,
                start_import=start_import,
                start_export=start_export,
            )
        except Exception as error:
            logger.exception("无法打开分类预设")
            QMessageBox.warning(self, tr("无法打开分类预设"), str(error))
            return
        self._category_preset_dialog = dialog
        dialog.import_completed.connect(self._on_category_preset_imported)
        dialog.view_categories_requested.connect(self._view_preset_categories)
        dialog.games_requested.connect(self._open_game_settings)
        dialog.finished.connect(self._category_preset_dialog_finished)
        dialog.open()

    @_invalidates_library_probe
    def _on_category_preset_imported(self, game_id: str) -> None:
        """分类预设导入后使操作校验证据失效，并刷新当前游戏的概览目录。"""
        self._mod_manager.invalidate_operation_trust()
        if game_id == self._game_service.current_game_id:
            self._reload_overview_level(game_id)

    def _view_preset_categories(self, game_id: str) -> None:
        """确认目标游戏存在后切换游戏，并导航到概览的分类根目录。"""
        if self._game_service.get_game(game_id) is None:
            return
        if game_id != self._game_service.current_game_id:
            self._on_game_changed(game_id)
        self._select_nav_page(self.PAGE_OVERVIEW)
        self._overview_page._navigate_to("")

    def _category_preset_dialog_finished(self) -> None:
        """释放分类预设窗口，恢复积压的目录事件处理，并继续等待中的主窗口关闭。"""
        dialog = getattr(self, "_category_preset_dialog", None)
        self._category_preset_dialog = None
        if dialog is not None:
            dialog.deleteLater()
        if getattr(self, "_watcher_events", None) or getattr(self._mod_manager, "_deferred_source_events", None):
            self._watcher_debounce.start(0)
        if getattr(self, "_category_preset_close_pending", False):
            self._category_preset_close_pending = False
            call_later(0, self, self.close)

    def _on_view_logs(self) -> None:
        """复用只读日志查看器，显示时重新读取日志，已有窗口可见时只激活。"""
        import shiboken6

        from src.ui.dialogs.log_viewer import LogViewerDialog

        dialog = getattr(self, "_log_viewer_dialog", None)
        if dialog is None or not shiboken6.isValid(dialog):
            dialog = LogViewerDialog(self)
            self._log_viewer_dialog = dialog
        if dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog.exec()

    # 辅助

    def _start_file_watcher(self, game_id: str) -> None:
        """启动或重启文件监控"""
        self._watcher_changed_paths.clear()
        self._watcher_events.clear()
        self._deployment_events = []
        self._library_discovery.reset_scope()
        self._library_local_discovery.reset_scope()
        self._watcher_debounce.stop()
        watch_path = get_data_dir() / self._game_service.get_resource_dir(game_id) / "mods"
        watch_path.mkdir(parents=True, exist_ok=True)
        self._file_watcher.start(watch_path)
        # 即使 watchdog 不可用/启动失败，轮询仍提供最终一致性。
        self._source_state_poll.start()

    def _reconcile_deploy_state(self) -> None:
        """周期定时器共享不可变探测输入和待处理标记。"""
        if _plugin_operation_busy(self):
            return
        if self._game_service.has_active_game():
            self._library_probe.schedule(periodic=True)

    def _defer_source_reconcile(self) -> None:
        """让周期源对账避开刚开始的卡片逐项动画。"""
        if self._source_state_poll.isActive():
            self._source_state_poll.start()

    def _reconcile_source_state(self) -> None:
        """定时核对已登记的源和部署路径，不发现新增内容。

        新增发现由目录事件、手动刷新、启动和游戏切换触发；纳管进行时暂停本轮检查。
        """
        if _plugin_operation_busy(self):
            return
        if self._game_service.has_active_game():
            self._library_probe.schedule(periodic=True)

    def _refresh_after_source_reconcile(self, game_id: str, removed: int) -> None:
        """发布已提交标识；手动刷新或不完整差异重新加载数据。"""
        if game_id != self._game_service.current_game_id:
            return
        if self._stack.currentIndex() == self.PAGE_OVERVIEW:
            probe = getattr(self, "_library_probe", None)
            changed_ids = getattr(probe, "changed_ids", None)
            removed_ids = getattr(probe, "removed_ids", None)
            overview = self._overview_page
            applied = False
            refresh = getattr(self, "_overview_refresh", None)
            if (
                changed_ids is not None
                and removed_ids is not None
                and removed == len(removed_ids)
                and len(set(changed_ids) | set(removed_ids)) <= 128
                and not getattr(refresh, "active", False)
                and hasattr(overview, "apply_reconcile_delta")
            ):
                removed_set = set(removed_ids)
                mods = [self._mod_manager.get_mod(mid) for mid in changed_ids if mid not in removed_set]
                if all(mod is not None for mod in mods):
                    parent_id = getattr(overview, "_current_parent_id", "")
                    overview.set_characters(self._mod_manager.get_characters_for_ui(game_id), refresh=False)
                    if parent_id == getattr(overview, "_current_parent_id", ""):
                        applied = overview.apply_reconcile_delta(mods, removed_ids)
            if not applied:
                self._reload_overview_level(game_id)
            if not applied and removed and self._overview_page._selected_mod is not None:
                selected_id = self._overview_page._selected_mod.mod_id
                if self._mod_manager.get_mod(selected_id) is None:
                    self._overview_page._selected_mod = None
                    self._overview_page._selected_ids.discard(selected_id)
                    self._overview_page._hide_panel()
        elif self._stack.currentIndex() == self.PAGE_GALLERY:
            self._refresh_gallery()
        if removed:
            self._refresh_preset_combo()
            self._refresh_store_installed_state()

    def _schedule_source_cleanup_retry(self) -> None:
        """按退避间隔重试耐久清理，并明确告知用户当前仍在等待。"""
        if self._mod_manager.has_pending_source_cleanup():
            if not self._source_cleanup_retry.isActive():
                delay = self._source_cleanup_retry_delays[
                    min(self._source_cleanup_retry_step, len(self._source_cleanup_retry_delays) - 1)
                ]
                self._source_cleanup_retry.start(delay)
            if not self._source_cleanup_notified:
                self._source_cleanup_notified = True
                try:
                    self._ensure_toast().done(
                        tr("Mod 清理尚未完成：文件可能被占用或部署盘离线，将在后台继续重试"),
                        ok=False,
                        hold_ms=6000,
                    )
                except Exception:
                    pass
        else:
            self._source_cleanup_retry.stop()
            if self._source_cleanup_notified:
                self._toast_done(tr("待处理的 Mod 清理已完成"))
            self._source_cleanup_retry_step = 0
            self._source_cleanup_notified = False

    def _retry_pending_source_cleanup(self) -> None:
        """重试继续使用后台流程，不恢复同步轮询。"""
        if self._game_service.has_active_game():
            self._source_cleanup_retry_step += 1
            self._library_probe.schedule()

    def _on_watcher_path_event(self, path: str) -> None:
        """收集 watcher 报出的具体变化路径，供本轮防抖结束后的对账定范围。"""
        if path:
            self._watcher_changed_paths.add(Path(path))
            from src.core.watcher import FileChange

            watcher = self._file_watcher
            self._on_source_change(FileChange(str(watcher._watch_path), watcher.generation, "modified", path))

    def _on_source_change(self, event) -> None:
        """接收当前源目录监视事件，保留配对路径并启动防抖；根目录失效时安排重连。"""
        watcher = self._file_watcher
        if event.generation != watcher.generation or event.root != str(watcher._watch_path):
            return
        probe = getattr(self, "_library_probe", None)
        if probe is not None:
            probe.yield_to_local_events()
        if event.kind in {"root_replaced", "watch_failed", "root_offline"}:
            self._schedule_watcher_restart(watcher, event.generation, event.root)
        self._watcher_events.append(event)
        # 防抖期间保留配对事件，调度前将后代路径合并到父事件范围。
        for path in (event.path, event.destination):
            if path:
                self._watcher_changed_paths.add(Path(path))
        if not self._watcher_debounce.isActive():
            self._watcher_debounce.start(0)

    def _on_source_observed(self, payload) -> None:
        """仅将当前游戏的源目录观测结果发布到概览，忽略切换游戏前的结果。"""
        game_id, observations = payload
        if game_id == self._game_service.current_game_id:
            self._overview_page.set_source_observations(observations)

    def _on_deployment_change(self, event) -> None:
        """筛选当前监视代次中影响部署容器身份或位置的事件，合并路径后调度局部核对。"""
        watcher = self._deploy_watcher
        if event.generation != watcher.generation or event.root != str(watcher._watch_path):
            return
        if event.kind in {"root_replaced", "watch_failed", "root_offline"}:
            self._schedule_watcher_restart(watcher, event.generation, event.root)
        root = Path(event.root)
        # INI 内容保存由同步器处理，仅容器成员和身份变化使部署位置证据失效。
        paths = [Path(p) for p in (event.path, event.destination) if p]
        relevant = [
            p
            for p in paths
            if p == root or p.parent == root or (p.name == MOD_CONTAINER_MARKER and p.parent.parent == root)
        ]
        if event.kind in {"overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline"}:
            relevant = [root]
        if not relevant:
            return
        probe = getattr(self, "_library_probe", None)
        if probe is not None:
            probe.yield_to_local_events()
        self._deploy_changed_paths.update(relevant)
        if not hasattr(self, "_deployment_events"):
            self._deployment_events = []
        self._deployment_events.append(event)
        if not self._deploy_watcher_debounce.isActive():
            self._deploy_watcher_debounce.start(0)

    def _schedule_watcher_restart(self, watcher, generation, root) -> None:
        """重新启动失败或更换的根监控，旧代次不能重新激活它。"""
        pending = getattr(self, "_watcher_restarts", None)
        if pending is None:
            pending = self._watcher_restarts = set()
        key = (watcher, generation)
        if key in pending:
            return
        pending.add(key)
        expected_generation = [generation]

        def retry():
            """重连仍有效的监视范围并触发源审计；关闭或代次变化时退出，失败时延后一秒重试。"""
            if watcher.generation != expected_generation[0] or getattr(self._library_probe, "closing", False):
                pending.discard(key)
                return
            try:
                if fs.is_dir(root) and watcher.start(root):
                    pending.discard(key)
                    if watcher is self._file_watcher:
                        self._mod_manager._local_source_events = None
                    self._library_probe.schedule(force_source_audit=True)
                    return
            except Exception as error:
                logger.warning("重新接通目录监视失败 path=%s reason=%s", root, error)
            expected_generation[0] = watcher.generation
            call_later(1000, self, retry)

        call_later(1000, self, retry)

    def _refresh_deploy_changes(self) -> None:
        """每次提交至多 32 个部署事件并发布局部差异，失败时把本批放回队首重试。"""
        self._deploy_changed_paths.clear()
        events = getattr(self, "_deployment_events", [])
        batch, self._deployment_events = events[:32], events[32:]
        if self._game_service.has_active_game() and (
            batch or getattr(self._mod_manager, "_deferred_deployment_events", None)
        ):
            try:
                delta = self._mod_manager.apply_local_deployment_events(batch)
                self._publish_local_library_delta(delta)
            except Exception:
                logger.exception("局部部署事件提交失败，保留事件待重试")
                self._deployment_events[:0] = batch
                self._deploy_watcher_debounce.start(500)
                return
        if self._deployment_events:
            self._deploy_watcher_debounce.start(0)
        elif getattr(self._mod_manager, "_deferred_deployment_events", None):
            self._deploy_watcher_debounce.start(50)

    def _refresh_from_watcher(self) -> None:
        """处理有限批次的资源事件，不受无关导入和审计阻塞。"""
        self._watcher_changed_paths.clear()
        batch, self._watcher_events = self._watcher_events[:32], self._watcher_events[32:]
        if self._game_service.has_active_game() and (
            batch or getattr(self._mod_manager, "_deferred_source_events", None)
        ):
            try:
                delta = self._mod_manager.apply_local_source_events(batch)
                inline_missing = (
                    getattr(delta, "retry_after_ms", None) == 0
                    and not self._watcher_events
                    and not getattr(self._mod_manager, "_deferred_source_events", None)
                )
                self._local_missing_inline = inline_missing
                try:
                    self._publish_local_library_delta(delta)
                finally:
                    self._local_missing_inline = False
                if inline_missing:
                    self._confirm_local_missing()
            except Exception:
                logger.exception("局部源事件提交失败，保留事件待重试")
                self._watcher_events[:0] = batch
                self._watcher_debounce.start(500)
                return
        if self._watcher_events:
            self._watcher_debounce.start(0)
        elif getattr(self._mod_manager, "_deferred_source_events", None):
            self._watcher_debounce.start(50)
        self._plugin_watcher_pending = False

    def _publish_local_library_delta(self, delta) -> None:
        """将已提交的 Mod 与分类差异更新到概览，并调度新增内容纳管、元数据刷新和缺失确认。"""
        game_id = self._game_service.current_game_id
        overview = self._overview_page
        removed = set(delta.removed_ids)
        mods = self._mod_manager._index.get_mods_by_ids(set(delta.changed_ids) - removed)
        node_ids = set(getattr(delta, "moved_node_ids", ()))
        prior = getattr(overview, "_delta_by_id", {})
        parents = getattr(overview, "_delta_node_parents", {})
        for mod in mods + [prior[mid] for mid in set(delta.changed_ids) | removed if mid in prior]:
            node_id = mod.node_id
            seen = set()
            while node_id and node_id not in seen:
                seen.add(node_id)
                node_ids.add(node_id)
                node_id = parents.get(node_id)
        moved_nodes = getattr(delta, "moved_node_ids", ())
        if moved_nodes:
            nodes = self._mod_manager.get_nodes_for_ui(node_ids)
            new_ancestors = set()
            for node in nodes:
                parent = node.get("parent_id")
                while parent and parent not in node_ids and parent not in new_ancestors:
                    new_ancestors.add(parent)
                    parent = parents.get(parent)
            if new_ancestors:
                nodes.extend(self._mod_manager.get_nodes_for_ui(new_ancestors))
            # 后代归属使用已提交的父节点链。
            overview.apply_node_delta(nodes)
        if mods or removed:
            overview.apply_reconcile_delta(mods, removed)
        if node_ids and not moved_nodes:
            overview.apply_node_delta(self._mod_manager.get_nodes_for_ui(node_ids))
        discoveries = getattr(delta, "discoveries", ())
        if discoveries:
            # 先确认输入，再由统一导入任务中心显示等待和运行状态。
            self._library_local_discovery.schedule(
                game_id,
                trigger="watcher",
                changed_paths=discoveries,
                external_copy_paths=getattr(delta, "external_copy_paths", ()),
            )
        refresh = getattr(delta, "refresh_mod_ids", ())
        if refresh:
            if not hasattr(self, "_library_metadata"):
                from src.ui.library_metadata import LibraryMetadataController

                self._library_metadata = LibraryMetadataController(
                    self._mod_manager, self._publish_local_library_delta, self
                )
            self._library_metadata.schedule(refresh)
        missing = getattr(delta, "pending_missing_ids", ())
        retry = getattr(delta, "retry_after_ms", None)
        if missing and retry != 0:
            # 下一轮完成最终确认，清理失败通过重试差异发布不可用状态。
            overview.set_pending_missing_mods(missing)
        if (missing or retry == 0) and not getattr(self, "_local_missing_inline", False):
            # 后续原生批次可能补足待处理证据，明确提示可提前结束等待期。
            if not getattr(self, "_local_missing_scheduled", False) or retry == 0:
                self._local_missing_scheduled = True
                delay = retry if retry is not None else 500 if getattr(self, "_local_missing_retrying", False) else 16
                call_later(delay, self, self._confirm_local_missing)
        review = getattr(delta, "review_paths", ())
        if review:
            service = getattr(self._mod_manager, "_local_source_events", None)
            if service is not None and service._tainted:
                self._library_probe.schedule_changes(review, force_source_audit=True)
            else:
                self._library_probe.schedule_changes(review)

    def _confirm_local_missing(self) -> None:
        """等待目录事件消化且清理任务空闲后，再确认当前游戏缺失的源文件并发布清理结果。"""
        self._local_missing_scheduled = False
        cleanup = getattr(self, "_library_source_cleanup", None)
        if cleanup is not None and cleanup.running:
            return
        if self._watcher_events:
            self._local_missing_scheduled = True
            call_later(16, self, self._confirm_local_missing)
            return
        if self._game_service.has_active_game():
            if cleanup is None:
                from src.ui.library_source_cleanup import LibrarySourceCleanupController

                cleanup = self._library_source_cleanup = LibrarySourceCleanupController(
                    self._mod_manager, self._publish_local_library_delta, self._confirm_local_missing, self
                )
            self._local_missing_retrying = True
            try:
                self._publish_local_library_delta(self._mod_manager.confirm_local_missing(purge=cleanup.purge))
            finally:
                self._local_missing_retrying = False

    def _reconcile_library(
        self,
        game_id: str,
        *,
        trigger: str,
        changed_paths: set[Path] | None = None,
        manual: bool = False,
        discover: bool = True,
        publish: str = "on-change",
    ) -> None:
        """由所有者协调器排队执行探测和发现。"""
        if _plugin_operation_busy(self):
            return
        busy = self._intake_worker is not None or self._intake_settling
        if manual and not busy:
            self._manual_refresh_active = True
            self._intake_progress.start_batch(0, tr("正在刷新 Mod 库"))
            self._intake_progress.update_progress(None, tr("正在刷新 Mod 库"), tr("正在检查新增内容…"))
        root_watch = getattr(self, "_deployment_root_watch", None)
        if root_watch is not None:
            resolution = root_watch.check_current()
            if resolution.status == "missing":
                self._on_deployment_root_invalid(game_id, resolution.previous_path)
                self._on_library_discovery_failed(
                    tr("部署目录无效，请重新选择"), {"game_id": game_id, "manual": manual}
                )
                return
        if manual:
            self._library_probe.reset_failure()
        self._library_discovery.schedule(
            game_id, trigger=trigger, changed_paths=changed_paths, manual=manual, discover=discover, publish=publish
        )

    def _on_library_discovery_failed(self, error, options) -> None:
        """仅结算当前游戏的手动发现失败；纳管仍在执行或提交时保留其进度显示。"""
        if options.get("game_id") != self._game_service.current_game_id:
            return
        if options.get("manual") or getattr(self, "_manual_refresh_active", False):
            self._manual_refresh_active = False
            if self._intake_worker is None and not self._intake_settling:
                self._intake_progress.finish(tr("刷新未完成"), str(error), ok=False)

    def _consume_library_discovery(self, report, options) -> None:
        """接收当前游戏的发现结果，按需刷新概览、重试清理并启动新增内容纳管。"""
        if report.game_id != self._game_service.current_game_id:
            return
        if options["manual"]:
            self._manual_refresh_active = False
        self._schedule_source_cleanup_retry()
        if options["publish"] == "always":
            self._refresh_after_source_reconcile(report.game_id, 0)
        if report.intake_requests or report.invalid_containers:
            self._start_intake_batch(report, manual=options["manual"])
        elif options["manual"]:
            self._intake_progress.finish(tr("Mod 库已是最新"), tr("未发现需要纳管的新增内容"))

    def _recover_pending_preset_applies(self, game_id: str) -> None:
        """启动时恢复未完成的预设部署事务及运行时变体补丁。

        残留事务会阻止后续切换和启停，因此在接受操作前完成恢复。
        未配置部署目录时暂缓恢复，设置目录后由 set_deploy_dir 处理。
        """
        try:
            if self._mod_manager._storage.get_deploy_dir() is None:
                return
            pm = self._mod_manager.preset_manager
            if not pm.has_pending_apply(game_id):
                return
            errors = pm.recover_incomplete_applies(game_id)
        except Exception as e:
            logger.warning("启动回滚未完成的预设切换失败: %s", e)
            return
        if errors:
            logger.warning("未完成的预设切换未能完整回滚: %s", errors)
            call_later(0, self, lambda: self._toast_done(tr("上次预设切换未能完整回滚，请检查部署目录")))
        else:
            logger.info("已回滚未完成的预设切换事务: %s", game_id)

    def _on_core_event(self, event: str, **payload) -> None:
        """将操作失败、变体及部署状态事件转交界面线程，并在游戏切换后更新相关服务。"""
        if event == "operation_failed":
            self.operation_failed.emit(payload)
            return
        if event in (
            "variant_state_changed",
            "variant_references_missing",
            "variant_restored",
            "mod_enabled",
            "mod_disabled",
            "game_changed",
        ):
            self.variant_event_received.emit(event, payload)
        if event == "game_changed":
            # 数据源已切换：按目标游戏恢复离线更新结果，同时清除旧游戏的检查提示。
            self._update_check_is_manual = False
            self._overview_page.set_update_status_text("")
            dlg = getattr(self, "_mod_updates_dialog", None)
            if dlg is not None:
                dlg.force_close()
            self._mod_update_service.load_status()
            if dlg is not None:
                self._open_mod_updates_center()
            return

    def _on_variant_event(self, event: str, payload) -> None:
        """转发当前游戏的变体事件，并提示缺失引用回退及用户偏好写入结果。"""
        game_id = payload.get("game_id")
        if game_id and game_id != self._mod_manager.current_game_id:
            return
        controller = getattr(self, "_variant_controller", None)
        if controller is not None:
            controller.handle_core_event(event, **payload)
        if event == "variant_references_missing":
            names = list(dict.fromkeys(str(name) for name in payload.get("names", ()) if name))
            label = "、".join(names)
            self._ensure_toast().done(
                tr("{value}缺失的变体引用已改用 Mod 默认。", value=label + "：" if label else ""),
                ok=False,
                hold_ms=6000,
            )
        elif event == "variant_restored":
            applied = int(payload.get("applied") or 0)
            who = tr("{applied} 个 Mod 的", applied=applied) if applied > 1 else ""
            self._ensure_toast().done(
                tr("{who}用户偏好已写入；若游戏仍在运行，请退出后重新应用，再启动。", who=who), ok=True, hold_ms=5000
            )

    def _show_operation_failure(self, payload):
        """显示当前游戏的操作失败详情，确认且游戏未切换时删除对应记录并刷新视图。"""
        from src.ui.dialogs.operation_failure import OperationFailureDialog

        game_id = payload.get("game_id")
        if game_id != self._mod_manager.current_game_id:
            return
        error = payload["error"]
        node_id = getattr(error, "node_id", None)
        target = self._mod_manager.get_character(node_id) if node_id else self._mod_manager.get_mod(error.mod_id)
        delete_method = "permanent" if node_id else str(self._config.get("mods.delete_method", "permanent"))
        if (
            OperationFailureDialog(
                error,
                self,
                target_name=target.display_name if target else "",
                delete_method=delete_method,
            ).exec()
            != 1
        ):
            return
        if game_id != self._mod_manager.current_game_id:
            return
        removed = (
            self._mod_manager.delete_character(node_id)
            if node_id
            else self._mod_manager.delete_mod(error.mod_id, delete_method=delete_method)
        )
        if removed:
            self._refresh_intake_views()
        else:
            self._toast_done(tr("删除未完成，相关记录已保留。"))

    def _schedule_activation_reconcile(self, game_id: str, trigger: str) -> None:
        """先显示索引，再异步对账离线变化。"""
        self._reconcile_library(game_id, trigger=trigger, publish="on-change")

    def _resume_plugin_deferred_intake(self) -> None:
        """插件文件操作结束后，为仍处于当前游戏的延后纳管请求重新扫描和规划。"""
        pending = getattr(self, "_plugin_intake_pending", None)
        if pending is None or _plugin_operation_busy(self):
            return
        self._plugin_intake_pending = None
        game_id, manual = pending
        if game_id == self._game_service.current_game_id:
            # 插件文件工作后重新规划，不能复用之前排队的报告。
            self._reconcile_library(game_id, trigger="manual", manual=manual)

    @_invalidates_library_probe
    def _start_intake_batch(self, report, *, manual: bool) -> bool:
        """消费统一对账给出的纳管计划：处置损坏容器 + 起后台 worker。"""
        if report.game_id != self._game_service.current_game_id or not self._game_service.has_active_game():
            return False  # 已切走游戏：这批属于上一个游戏，丢弃
        if _plugin_operation_busy(self):
            pending = getattr(self, "_plugin_intake_pending", None)
            self._plugin_intake_pending = (report.game_id, manual or bool(pending and pending[1]))
            return False
        if self._intake_worker is not None or self._intake_settling:
            # 计划做好后才被别的批次抢先（延后起批时可能发生）→ 记一次补扫。
            if not self._intake_rescan_pending:
                self._intake_rescan_pending = True
                self._intake_progress.queue_info(1)
            self._intake_rescan_manual = self._intake_rescan_manual or manual
            return False

        requests = list(report.intake_requests)
        invalid_containers = list(report.invalid_containers)

        broken_note = ""
        if invalid_containers:
            # 无法解析标记的容器不进入纳管流程，须单独提示处置。
            # 处置对话框运行嵌套事件循环，期间阻止目录监控或手动刷新启动新批次。
            self._intake_settling = True
            try:
                broken_note = self._dispose_broken_containers(invalid_containers)
            finally:
                self._intake_settling = False
            if broken_note:
                self._refresh_intake_views()

        if not requests:
            if broken_note:
                self._intake_progress.finish(
                    tr("已处置损坏的 Mod 容器"),
                    tr("未发现可纳管的新增内容{broken_note}", broken_note=broken_note),
                    ok=False,
                )
            elif manual:
                self._intake_progress.finish(tr("Mod 库已是最新"), tr("未发现需要纳管的新增内容"))
            return False

        if not manual:
            self._intake_progress.start_batch(len(requests), tr("发现 {count} 个新 Mod，正在纳管", count=len(requests)))
        else:
            self._intake_progress.start_batch(len(requests), tr("正在纳管 {count} 个新增项", count=len(requests)))
        # 逐项明细：先全部登记为「排队中」，随进度逐个转态。
        self._intake_item_names = [Path(str(r.source_path)).name for r in requests]
        # 保存本批源路径，停止后据此查找并处理仍未纳管的输入。
        self._intake_item_paths = [Path(str(r.source_path)) for r in requests]
        self._intake_handled_indexes: set[int] = set()
        # 失败/重复项索引单独记：它们也算「已处置」，故不能从 _intake_handled_indexes
        # 反推；两者再分桶，因为处置弹窗文案要按原因区分。
        self._intake_failed_indexes: set[int] = set()
        self._intake_duplicate_indexes: set[int] = set()
        # 本轮发现阶段已处置的损坏容器文案，结算时拼进终态通知。
        self._intake_broken_note = broken_note
        # 新一批开始：清掉上一批的提交记录与终止决策，否则「取消全部」会误删上批的 Mod。
        self._intake_committed_mods = []
        self._intake_cancel_all = False
        for index, name in enumerate(self._intake_item_names):
            self._intake_progress.update_item(index, name, ST_QUEUED)
        # 投放/F5 纳管与程序内导入共用同一个并行度设置（import.parallel_workers）：
        # 两条通道的 prepare 都是各自独立的文件 IO，没道理一条并行一条串行。
        # 单项批次强制 1，省掉没意义的线程池开销。
        parallel_workers = effective_archive_workers(self._config.get("import.parallel_workers"))
        worker = IntakeWorker(
            requests,
            self._intake_service,
            max_workers=parallel_workers if len(requests) > 1 else 1,
            parent=self,
        )
        worker.progress_updated.connect(self._on_intake_progress)
        worker.item_progress.connect(self._on_intake_item_progress)
        worker.item_prepared.connect(self._on_intake_item_prepared)
        worker.item_failed.connect(self._on_intake_item_failed)
        worker.batch_finished.connect(self._on_intake_batch_finished)
        worker.finished.connect(worker.deleteLater)
        self._intake_worker = worker
        worker.start()
        return True

    def _on_panel_stop_requested(self) -> None:
        """处理任务面板发出的停止请求。

        投放纳管和程序内导入分别处理自身批次的确认与回滚。
        """
        if getattr(self, "_manual_refresh_active", False) and self._intake_worker is None and not self._intake_settling:
            self._manual_refresh_active = False
            self._intake_rescan_pending = self._intake_rescan_manual = False
            self._library_discovery.cancel()
            self._library_local_discovery.cancel(cancel_probe=False)
            self._intake_progress.finish_cancelled(tr("刷新已停止"))
            return
        self._cancel_intake()

    def _cancel_intake(self) -> None:
        """停止投放纳管前，确认保留已提交结果或撤销整批操作。

        纳管逐项提交，终止决策需覆盖已经入库的 Mod。
        """
        worker = self._intake_worker
        if worker is None:
            return
        committed = list(getattr(self, "_intake_committed_mods", ()))
        # 先冻结工作线程再显示确认框，避免嵌套事件循环期间继续提交导入结果。
        worker.freeze()
        cancel_all = True
        if committed:
            msg = QMessageBox(self)
            msg.setWindowTitle(tr("停止纳管"))
            total = len(getattr(self, "_intake_item_names", ()) or ())
            remaining = max(0, total - len(committed))
            msg.setText(
                tr(
                    "已纳管 {count} 个 Mod，还有 {remaining} 个未处理。\n\n请选择操作：",
                    count=len(committed),
                    remaining=remaining,
                )
            )
            cancel_all_btn = msg.addButton(tr("取消全部纳管"), QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton(tr("保留已纳管的Mod"), QMessageBox.ButtonRole.AcceptRole)
            # 误点 ■ 要有退路：选它则解冻放行，纳管照常继续。
            resume_btn = msg.addButton(tr("继续导入"), QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(resume_btn)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked is resume_btn:
                worker.unfreeze()
                return  # 不请求取消、不动决策位，纳管照常进行
            cancel_all = clicked is cancel_all_btn
        # 决策先记下，等 worker 真正停下来再在 batch_finished 里执行回滚。
        self._intake_cancel_all = cancel_all
        self._intake_progress.set_stop_enabled(False)
        worker.request_cancel()  # 内部会解冻，让 worker 走到取消分支收尾

    def _on_intake_progress(
        self,
        value: float | None,
        text: str,
        current: int,
        total: int,
    ) -> None:
        """将纳管比例限制在有效范围并转换为百分比，保留未知进度的忙碌状态。"""
        pct = None if value is None else max(0.0, min(1.0, float(value))) * 100.0
        title = tr("正在纳管 {value}/{total}", value=max(1, current), total=total) if total > 0 else tr("正在纳管")
        self._intake_progress.update_progress(pct, title, text)

    def _on_intake_item_progress(self, index: int, frac: float, text: str) -> None:
        """使用任务提供的实际条目索引更新明细进度，不从展示用计数推导索引。"""
        name = self._intake_item_name(index)
        if not name:
            return
        pct = int(max(0.0, min(1.0, frac)) * 100)
        stage = (text or "").split(":", 1)[0].strip()
        label = f"{stage} {pct}%" if stage else f"{pct}%"
        self._intake_progress.update_item(index, name, ST_RUNNING, label)

    def _intake_item_name(self, index: int) -> str:
        """按本批索引读取纳管项目名称，索引无效或名称尚未建立时返回空字符串。"""
        names = getattr(self, "_intake_item_names", None)
        if not names or index < 0 or index >= len(names):
            return ""
        return names[index]

    @_invalidates_library_probe
    def _on_intake_item_prepared(self, _index: int, prepared) -> None:
        """在界面线程提交后台准备结果，向工作线程确认提交或拒绝，并记录整批回滚所需的 Mod。"""
        worker = self._intake_worker
        if worker is None:
            return
        if worker.is_cancel_requested:
            worker.acknowledge(committed=False)
            self._mark_intake_item(_index, ST_ROLLED_BACK, tr("已回滚"))
            return
        if isinstance(prepared, Exception):
            worker.acknowledge(committed=False, error=str(prepared))
            self._mark_intake_item(_index, ST_FAILED, str(prepared)[:24])
            return
        try:
            mod = self._intake_service.commit(
                prepared,
                rollback_on_error=False,
            )
        except Exception as exc:
            logger.warning("提交后台纳管结果失败: %s", exc)
            worker.acknowledge(committed=False, error=str(exc))
            self._mark_intake_item(_index, ST_FAILED, tr("提交失败"))
            return
        worker.acknowledge(committed=True)
        # 保留已提交的 Mod，以支持取消全部纳管时回滚本批提交。
        if mod is not None:
            self._intake_committed_mods.append(mod)
            self._overview_page.apply_reconcile_delta([mod], ())
        # 显示元数据读取降级，避免将读取失败误认为原内容未提供作者或来源。
        notice = getattr(prepared, "metadata_notice", "")
        if notice:
            logger.warning("纳管降级: %s (%s)", notice, self._intake_item_name(_index))
            self._mark_intake_item(_index, ST_DONE, tr("已纳管·无元数据"))
            return
        self._mark_intake_item(_index, ST_DONE, tr("已纳管"))

    def _mark_intake_item(self, index: int, status: str, text: str) -> None:
        """记录已处置的项目索引并更新任务明细，供停止后的遗留输入筛选使用。"""
        # 成功、失败和回滚均记为已处理；停止后按此集合筛选尚未开始的输入。
        try:
            self._intake_handled_indexes.add(int(index))
        except Exception:
            pass
        name = self._intake_item_name(index)
        if name:
            self._intake_progress.update_item(index, name, status, text)

    def _on_intake_item_failed(
        self,
        _index: int,
        message: str,
        source_restored: bool,
        is_duplicate: bool = False,
    ) -> None:
        """分别登记失败和重复输入以供后续处置，并同步源文件恢复情况及任务行状态。"""
        suffix = tr("；原文件已保留或恢复") if source_restored else ""
        if is_duplicate:
            logger.info("后台 Mod 纳管跳过重复: %s%s", message, suffix)
        else:
            logger.warning("后台 Mod 纳管失败: %s%s", message, suffix)
        # 失败和重复输入也会标为已处理，但源文件可能仍在库中；另存索引供分类处置。
        try:
            if is_duplicate:
                self._intake_duplicate_indexes.add(int(_index))
            else:
                self._intake_failed_indexes.add(int(_index))
        except Exception:
            pass
        # 将处理结果同步到任务明细行。
        if is_duplicate:
            # 重复输入未产生新提交，以回滚状态显示跳过结果。
            self._mark_intake_item(_index, ST_ROLLED_BACK, tr("重复，已跳过"))
            return
        self._mark_intake_item(
            _index,
            ST_FAILED,
            tr("失败，原文件已恢复") if source_restored else tr("失败"),
        )

    def _pending_leftover_paths(self) -> list[Path]:
        """本批中从未开始处理、仍原样躺在库目录里的项。"""
        paths = getattr(self, "_intake_item_paths", None) or []
        handled = getattr(self, "_intake_handled_indexes", None) or set()
        out: list[Path] = []
        for index, path in enumerate(paths):
            if index in handled:
                continue
            try:
                if path.exists():
                    out.append(path)
            except OSError:
                continue
        return out

    def _failed_intake_paths(self, *, duplicates: bool = False) -> list[Path]:
        """分别返回本批失败和重复且仍存在于资源库的输入。

        这些项目已被纳管流程处理，不属于未处理项集合；
        须单独收集并按原因显示处置说明。
        """
        paths = getattr(self, "_intake_item_paths", None) or []
        indexes = (
            getattr(
                self,
                "_intake_duplicate_indexes" if duplicates else "_intake_failed_indexes",
                None,
            )
            or set()
        )
        out: list[Path] = []
        for index in sorted(indexes):
            if index < 0 or index >= len(paths):
                continue
            path = paths[index]
            try:
                if path.exists():
                    out.append(path)
            except OSError:
                continue
        return out

    def _dispose_broken_containers(self, containers: list[str]) -> str:
        """处理标记无法解析的容器，并返回用于批次通知的补充说明。

        此类容器在发现时即被拒绝，不会出现在正常纳管结果中，须在此单独处理。
        """
        from src.ui.dialogs.leftover_intake import REASON_BROKEN

        paths: list[Path] = []
        for raw in containers:
            try:
                path = Path(raw)
                if path.exists():
                    paths.append(path)
            except OSError:
                continue
        if not paths:
            return ""
        recycled, deleted = self._resolve_intake_leftovers(paths, allow_keep=False, reason=REASON_BROKEN)
        return self._disposal_note(tr("无法识别的 Mod"), recycled, deleted)

    @staticmethod
    def _disposal_note(label: str, recycled: int, deleted: int) -> str:
        """把处置结果拼成通知框尾巴（无处置则空串）。"""
        parts = []
        if recycled:
            parts.append(tr("{recycled} 个移入回收站", recycled=recycled))
        if deleted:
            parts.append(tr("{deleted} 个已永久删除", deleted=deleted))
        if not parts:
            return ""
        return f"；{label} " + "、".join(parts)

    def _resolve_intake_leftovers(
        self,
        paths: list[Path] | None = None,
        *,
        allow_keep: bool = True,
        reason: str | None = None,
    ) -> tuple[int, int]:
        """处理资源库遗留项，返回移入回收站和永久删除的数量。

        allow_keep 控制是否允许保留；不允许保留时关闭对话框采用其已说明的删除行为。
        reason 决定提示内容，重复、失败和损坏须分别说明。
        """
        from src.ui.dialogs.leftover_intake import (
            ACTION_DELETE,
            ACTION_RECYCLE,
            REASON_STOPPED,
            resolve_leftover_paths,
        )

        leftovers = self._pending_leftover_paths() if paths is None else list(paths)
        if not leftovers:
            return (0, 0)
        decisions = resolve_leftover_paths(
            leftovers,
            parent=self,
            allow_keep=allow_keep,
            reason=reason or REASON_STOPPED,
        )
        recycled = deleted = 0
        for raw, action in decisions.items():
            if action not in (ACTION_RECYCLE, ACTION_DELETE):
                continue  # 保留：维持现状
            try:
                ok = self._mod_manager._storage.delete_source(Path(raw), delete_method=action)
            except Exception as exc:
                logger.warning("处置未纳管残留失败 (%s): %s", raw, exc)
                continue
            if not ok:
                logger.warning("处置未纳管残留未成功: %s", raw)
                continue
            if action == ACTION_RECYCLE:
                recycled += 1
            else:
                deleted += 1
        if recycled or deleted:
            logger.info("未纳管残留处置完成: 回收站 %d, 删除 %d", recycled, deleted)
        return (recycled, deleted)

    @_invalidates_library_probe
    def _on_intake_batch_finished(self, result: IntakeBatchResult) -> None:
        """释放本批工作线程引用并结算结果，以结算标记阻止模态对话框期间启动新批次。"""
        self._intake_worker = None
        # 完成后释放任务引用，再由 _intake_settling 保护结果处理期间的模态事件循环，
        # 防止遗留项对话框尚未关闭时启动新一批纳管。
        self._intake_settling = True
        try:
            self._settle_intake_batch(result)
        finally:
            # 异常路径也须释放结算状态，允许后续目录事件或刷新继续纳管。
            self._intake_settling = False

    @_invalidates_library_probe
    def _settle_intake_batch(self, result: IntakeBatchResult) -> None:
        """处置纳管遗留输入，按取消选择回滚已提交内容，再汇总结果并恢复后续任务。"""
        committed_mods = list(getattr(self, "_intake_committed_mods", ()))
        cancel_all = bool(getattr(self, "_intake_cancel_all", False))
        self._intake_committed_mods = []
        self._intake_cancel_all = False
        # 将发现期间已处理的损坏容器计入本批结果说明。
        broken_note = getattr(self, "_intake_broken_note", "") or ""
        self._intake_broken_note = ""

        # 停止后先处置未纳管输入，避免留在库目录中的文件在下次刷新时再次纳管。
        leftover_note = ""
        disposed_any = False
        if result.cancelled:
            recycled, deleted = self._resolve_intake_leftovers()
            leftover_note = self._disposal_note(tr("尚未加入 Mod 库的项目"), recycled, deleted)
            disposed_any = bool(recycled or deleted)
        elif result.failed or result.duplicates:
            # 失败及重复输入仍位于资源库中，须进入遗留项处理，避免后续发现反复处理。

            # 失败和重复输入分别询问并使用对应文案，避免将重复内容误报为损坏。
            from src.ui.dialogs.leftover_intake import (
                REASON_DUPLICATE,
                REASON_FAILED,
            )

            failed_paths = self._failed_intake_paths()
            if failed_paths:
                recycled, deleted = self._resolve_intake_leftovers(failed_paths, allow_keep=False, reason=REASON_FAILED)
                leftover_note += self._disposal_note(tr("失败项"), recycled, deleted)
                disposed_any = disposed_any or bool(recycled or deleted)
            duplicate_paths = self._failed_intake_paths(duplicates=True)
            if duplicate_paths:
                recycled, deleted = self._resolve_intake_leftovers(
                    duplicate_paths, allow_keep=False, reason=REASON_DUPLICATE
                )
                leftover_note += self._disposal_note(tr("重复项"), recycled, deleted)
                disposed_any = disposed_any or bool(recycled or deleted)
        leftover_note += broken_note

        if result.cancelled and cancel_all and committed_mods:
            # 取消全部纳管时，在 SQLite 连接所属的主线程回滚本批已提交内容。
            rolled_back = 0
            for mod in committed_mods:
                try:
                    self._mod_manager.delete_mod(mod.mod_id)
                    rolled_back += 1
                except Exception as exc:
                    logger.warning("回滚已纳管 Mod 失败: %s", exc)
            detail = tr("已取消全部纳管，回滚 {rolled_back} 个 Mod", rolled_back=rolled_back)
            if result.restored:
                detail += tr("；已恢复 {restored} 个原文件", restored=result.restored)
            self._intake_progress.finish_cancelled(detail + leftover_note)
            self._refresh_intake_views()
            self._finish_intake_followup(result)
            return

        if result.cancelled:
            if committed_mods:
                detail = tr("已停止纳管，保留已纳管的 {count} 个 Mod", count=len(committed_mods))
                if result.restored:
                    detail += tr("；已恢复 {restored} 个原文件", restored=result.restored)
            else:
                detail = (
                    tr("未提交内容已清理，已恢复 {restored} 个原文件", restored=result.restored)
                    if result.restored
                    else tr("未提交内容已清理，用户原文件保持不变")
                )
            self._intake_progress.finish_cancelled(detail + leftover_note)
        elif result.failed:
            detail = tr("已纳管 {committed} 个，{failed} 个失败", committed=result.committed, failed=result.failed)
            if result.duplicates:
                detail += tr("，{duplicates} 个重复", duplicates=result.duplicates)
            if result.restored:
                detail += tr("；已恢复 {restored} 个原文件", restored=result.restored)
            self._intake_progress.finish(tr("纳管完成，但有失败项"), detail + leftover_note, ok=False)
        elif result.duplicates:
            # 重复输入以成功状态汇总，冗余副本的处置结果已记录在 leftover_note 中。
            detail = tr("跳过 {duplicates} 个重复内容", duplicates=result.duplicates)
            if result.committed:
                detail = tr("已纳管 {committed} 个，", committed=result.committed) + detail
            self._intake_progress.finish(tr("Mod 纳管完成"), detail + leftover_note)
        elif result.committed:
            self._intake_progress.finish(
                tr("Mod 纳管完成"), tr("已纳管 {committed} 个 Mod", committed=result.committed) + leftover_note
            )
        else:
            self._intake_progress.finish(tr("Mod 库已是最新"), tr("未发现需要纳管的新增内容") + leftover_note)
        # 处置遗留输入后重新扫描视图，移除已删除内容的卡片。
        if result.committed or disposed_any:
            self._refresh_intake_views()
        self._finish_intake_followup(result)

    @_invalidates_library_probe
    def _finish_intake_followup(self, result: IntakeBatchResult) -> None:
        """本轮结束后的待复扫合并（多次 F5/watcher 只补一次）。"""
        # 调度补充扫描前解除结算标记，使新批次能够通过重入检查。
        self._intake_settling = False
        pending = self._intake_rescan_pending and not result.cancelled
        pending_manual = self._intake_rescan_manual
        self._intake_rescan_pending = False
        self._intake_rescan_manual = False
        if pending:
            call_later(
                0,
                self,
                lambda: self._refresh_current(manual=pending_manual),
            )

    def _refresh_intake_views(self) -> None:
        """刷新当前可见的概览或画廊，并同步商店已安装标记；无当前游戏时跳过。"""
        gid = self._game_service.current_game_id
        if not gid:
            return
        if self._stack.currentIndex() == self.PAGE_OVERVIEW:
            self._reload_overview_level(gid)
        elif self._stack.currentIndex() == self.PAGE_GALLERY:
            self._refresh_gallery()
        self._refresh_store_installed_state()

    def _refresh_current(
        self,
        *,
        after_preset_commit: bool = False,
        manual: bool = False,
        changed_paths: set[Path] | None = None,
    ) -> None:
        """选择统一对账的触发器并检查执行条件。

        手动刷新和预设提交检查当前游戏；目录事件限定为合并后的变化路径。
        控制器依次执行路径核对、后台发现和主线程规划。
        """
        if _plugin_operation_busy(self):
            return
        # 导入期间的复制和压缩目录尚未稳定，暂不执行后台纳管，避免重复登记同一资源。
        if self._import_active or (getattr(self, "_preset_apply_worker", None) is not None and not after_preset_commit):
            return
        # 「自致变更」抑制窗口内：本次 watcher 刷新与刚才的直接刷新重复，跳过以免二次重排。
        if not manual and time.monotonic() < self._watcher_suppress_until:
            return
        if not self._game_service.has_active_game():
            return  # 无激活游戏：不刷新（视图已清空，避免 F5/watcher 拉回全库残留）
        # 预设提交后是应用自发的一次全量收敛（不是目录事件），故与 F5 同档。
        full_scan = manual or after_preset_commit
        self._reconcile_library(
            self._game_service.current_game_id,
            trigger="manual" if full_scan else "watcher",
            changed_paths=None if full_scan else changed_paths,
            manual=manual,
            # F5 / 预设提交：用户或应用主动要求刷新，必须无条件重绘当前页 ——
            # 预设切换改的是 DB 启用态，对账报告里的计数天然是 0。
            # watcher：每个目录事件都会走到这里，无变化就重绘会造成多余重排。
            publish="always" if full_scan else "on-change",
        )

    # 下载浮窗为独立顶级窗口，主窗口移动、缩放或从最小化恢复时须重新定位。

    def moveEvent(self, event):
        """窗口移动后重定位可见的下载浮窗，保持其与主窗口对齐。"""
        super().moveEvent(event)
        self._reposition_download_flyout()

    def _on_bg_scrim_live(self, pct: int) -> None:
        """背景遮罩滑块拖动 → 随主题实时压暗/提亮（轻量 fillRect，直连无节流）。"""
        if getattr(self, "_bg_overlay", None) is not None:
            self._bg_overlay.set_scrim_live(pct)

    def _on_bg_blur_live(self, pct: int) -> None:
        """背景模糊滑块拖动 → 节流实时模糊（重算偏重，~40ms 合并一次）。"""
        self._pending_bg_blur = pct
        if not self._bg_blur_throttle.isActive():
            self._bg_blur_throttle.start(40)

    def _flush_bg_blur(self) -> None:
        """将防抖期间记录的最新模糊强度应用到仍存在的背景层。"""
        if getattr(self, "_bg_overlay", None) is not None:
            self._bg_overlay.set_blur_live(self._pending_bg_blur)

    def _refresh_bg_overlay(self) -> None:
        """根据配置更新背景绘制层，不重新应用全局样式。"""
        from src.ui.backgrounds.config import background_options

        cfg = background_options(self._config)
        if hasattr(self, "_bg_overlay"):
            self._bg_overlay.setGeometry(self._central.rect())
            self._bg_overlay.lower()
            validator = getattr(getattr(self, "_settings_page", None), "_bg_engine_validator", None)
            valid = bool(validator and validator.valid and validator.path == cfg.get("engine_path"))
            self._background_controller.apply(cfg, engine_valid=valid)
            self._on_bg_state_changed(self._background_controller.state, self._background_controller.message)

    def _on_bg_state_changed(self, state: str, message: str) -> None:
        """将背景加载状态传给设置页，并重新安排目录卡片资源准备。"""
        MainWindow._schedule_directory_card_preparation(self)
        page = getattr(self, "_settings_page", None)
        if page is not None:
            page.set_background_status(state, message)

    def _on_bg_preview_ready(self, path, image) -> None:
        """把解码完成的视频背景预览交给仍存在的设置页显示。"""
        page = getattr(self, "_settings_page", None)
        if page is not None:
            page.set_video_background_preview(path, image)

    def _on_bg_active_changed(self, _active: bool) -> None:
        """背景可用状态变化时延后同步窗口材质，外观已释放时忽略通知。"""
        # 仅首帧、错误和来源切换影响材质，普通帧不刷新材质。
        if hasattr(self, "_settings_page") and not getattr(self, "_appearance_disposed", False):
            call_later(0, self, self._sync_bg_material)

    def _sync_bg_material(self) -> None:
        """通过外观队列串行应用背景材质，避免覆盖正在执行的外观任务完成回调。"""
        if getattr(self, "_appearance_disposed", False):
            return
        # 首帧和加载失败也通过外观队列串行处理，避免直接刷新替换当前完成回调。
        self._ensure_appearance_queue().submit(
            "background",
            t("正在应用背景…"),
            t("背景已应用"),
            self._do_background_change,
        )

    def apply_background(self, *, on_applied=None) -> bool:
        """重配背景绘制层；透明材质可分批更新，颜色变化才替换全局 QSS。"""
        self._refresh_bg_overlay()
        try:
            from src.app import App

            return App.instance().apply_theme(
                self._config.get("general.theme", "dark"),
                on_finished=on_applied,
            )
        except Exception as e:
            logger.debug("应用背景后重渲染主题失败: %s", e)
            if on_applied is not None:
                on_applied(False)
            return False

    def resizeEvent(self, event):
        """窗口缩放后调整下载浮窗位置、背景层范围和背景控制器输出尺寸。"""
        super().resizeEvent(event)
        self._reposition_download_flyout()
        if hasattr(self, "_bg_overlay") and hasattr(self, "_central"):
            self._bg_overlay.setGeometry(self._central.rect())
            if hasattr(self, "_background_controller"):
                self._background_controller.resize()

    def showEvent(self, event):
        """首次显示时恢复最大化状态并延后发布插件通知，同时校准可见提示条的位置。"""
        super().showEvent(event)
        # 首次显示后恢复最大化状态，避免与初始化尺寸设置竞争；仅执行一次。
        if getattr(self, "_restore_maximized", False) and not getattr(self, "_maximized_restored", False):
            self._maximized_restored = True
            self.showMaximized()
        # 窗口首次显示后再发送插件启动通知，以使用最终窗口几何定位。
        if not getattr(self, "_plugin_notify_flushed", False):
            self._plugin_notify_flushed = True
            host = getattr(self, "_plugin_ui_host", None)
            if host is not None:
                call_later(200, self, host.flush_notifications)
        # 通知条可见时保持右上角对齐
        toast = getattr(self, "_toast", None)
        if toast is not None and toast.isVisible():
            try:
                toast._reposition(visible=True)
            except Exception:
                pass

    def changeEvent(self, event):
        """按最小化状态暂停或恢复背景渲染，并在最小化时隐藏独立下载浮窗。"""
        from PySide6.QtCore import QEvent

        # 主窗口最小化时同步隐藏独立浮窗。
        # 还原后用户再点下载管理按钮重新打开。
        if event.type() == QEvent.Type.WindowStateChange:
            if hasattr(self, "_background_controller"):
                self._background_controller.set_suspended(self.isMinimized())
            try:
                store = getattr(self, "_gb_store_page", None)
                if store is not None and self.isMinimized():
                    flyout = getattr(store, "download_flyout", None)
                    if flyout is not None and flyout.isVisible():
                        flyout.hide()
            except Exception:
                pass
        super().changeEvent(event)

    def _reposition_download_flyout(self) -> None:
        """在线商店已创建时重定位可见下载浮窗，浮窗失效不阻断窗口事件处理。"""
        store = getattr(self, "_gb_store_page", None)
        if store is None or not hasattr(store, "reposition_flyout_if_visible"):
            return
        try:
            store.reposition_flyout_if_visible()
        except Exception:
            pass

    def _pending_intake_count(self) -> int:
        """面板上仍处于「排队中 / 处理中」的项数；取不到就退回本轮总数。"""
        panel = getattr(self, "_intake_progress", None)
        try:
            rows = panel._items.values()
            pending = sum(1 for row in rows if row._status in (ST_QUEUED, ST_RUNNING))
            if pending:
                return pending
        except Exception:
            pass
        return len(getattr(self, "_intake_item_names", ()) or ())

    def _confirm_quit_during_intake(self) -> bool:
        """纳管进行中关窗确认。返回 True 表示用户选择「停止并退出」。"""
        pending = self._pending_intake_count()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("退出确认"))
        box.setText(tr("仍有 {pending} 个 Mod 正在纳管。", pending=pending) if pending else tr("仍有 Mod 正在纳管。"))
        box.setInformativeText(tr("直接退出会停止纳管并恢复未提交的原文件。"))
        stop_btn = box.addButton(tr("停止并退出"), QMessageBox.ButtonRole.DestructiveRole)
        wait_btn = box.addButton(tr("继续等待"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(wait_btn)
        box.exec()
        return box.clickedButton() is stop_btn

    def closeEvent(self, event: QCloseEvent) -> None:
        """先请求业务与探测任务退出，必要时推迟关闭；取消退出后恢复暂停的控制器。"""
        publication = getattr(self, "_enable_publication", None)
        if publication is not None and publication.busy:
            self._enable_publication_close_pending = True
            self._deploy_stopping = True
            event.ignore()
            publication.close()
            return
        if getattr(self, "_preset_delete_preparing", False) or getattr(self, "_preset_delete_worker", None) is not None:
            self._preset_delete_close_pending = True
            self._cancel_preset_delete()
            event.ignore()
            return
        variants = getattr(self, "_variant_controller", None)
        if variants is not None and variants.running:
            self._variant_close_pending = True
            event.ignore()
            variants.close()
            return
        preset_dialog = getattr(self, "_category_preset_dialog", None)
        if preset_dialog is not None:
            self._category_preset_close_pending = True
            event.ignore()
            preset_dialog.request_close()
            return
        cleanup = getattr(self, "_library_source_cleanup", None)
        if cleanup is not None:
            cleanup.stop()
        activation = getattr(self, "_deployment_activation", None)
        if activation is not None:
            activation.close()
            if activation.running:
                self._deployment_activation_close_pending = True
                event.ignore()
                return
        # 进入关闭确认前请求停止，完成前保留窗口和控制器，不同步等待读取。
        probe = getattr(self, "_library_probe", None)
        discovery = getattr(self, "_library_discovery", None)
        local_discovery = getattr(self, "_library_local_discovery", None)
        metadata = getattr(self, "_library_metadata", None)
        if metadata is not None:
            metadata.stop()
        overview = getattr(self, "_overview_refresh", None)
        if overview is not None:
            overview.stop()
        gallery = getattr(self, "_gallery_refresh", None)
        if gallery is not None:
            gallery.stop()
        if discovery is not None:
            discovery.stop()
        if local_discovery is not None:
            local_discovery.stop()
        if probe is not None:
            probe.stop()
            if (
                probe.worker is not None
                or probe.applying
                or (discovery is not None and discovery.worker is not None)
                or (local_discovery is not None and local_discovery.worker is not None)
            ):
                self._library_probe_close_pending = True
                event.ignore()
                return
        self._close_after_library_probe(event)
        if variants is not None and not event.isAccepted():
            variants.resume()
        if probe is not None and not event.isAccepted():
            if publication is not None:
                publication.closing = False
                call_later(0, self, self._on_enable_publication_idle)
            if activation is not None:
                activation.closing = False
                call_later(0, self, self._apply_migoto_for_current_game)
            if overview is not None:
                overview.resume()
            if gallery is not None:
                gallery.resume()
            if discovery is not None:
                discovery.resume()
            if local_discovery is not None:
                local_discovery.closing = False
            if metadata is not None:
                metadata.resume()
            probe.resume()

    def _on_library_probe_stopped(self) -> None:
        """库探测停止后清除关闭等待标记，并在下一轮事件循环重新发起窗口关闭。"""
        if self._library_probe_close_pending:
            self._library_probe_close_pending = False
            call_later(0, self, self.close)

    def _close_after_library_probe(self, event: QCloseEvent) -> None:
        """处理退出确认与剩余任务的停止，保存窗口状态并释放插件、监视和图像资源。
        文件任务未能及时停止或用户取消退出时拒绝关闭，保留主窗口供后续继续操作。
        """
        intake_worker = self._intake_worker
        if intake_worker is not None and intake_worker.isRunning():
            # 关闭程序前确认是否终止仍在后台执行的纳管任务。
            if not self._confirm_quit_during_intake():
                event.ignore()
                return
            intake_worker.request_cancel()
            # 工作线程等待提交决定时，拒绝当前结果并释放等待，使回滚仍在工作线程执行。
            intake_worker.acknowledge(committed=False)
            if not intake_worker.wait(5000):
                event.ignore()
                self._ensure_toast().done(
                    tr("正在停止 Mod 纳管，请稍候"),
                    ok=False,
                    hold_ms=2500,
                )
                return
        if self._preset_apply_worker is not None or getattr(self, "_preset_apply_preparing", False):
            if not self._stop_preset_apply():
                event.ignore()
                self._ensure_toast().done(tr("正在取消预设切换，请稍候"), ok=False, hold_ms=2500)
                return
        disable_worker = getattr(self, "_disable_worker", None)
        if disable_worker is not None and disable_worker.isRunning():
            # 批量禁用在项目之间检查中断；等待当前文件操作返回后再提交已完成的结果。
            disable_worker.requestInterruption()
            if not disable_worker.wait(5000):
                event.ignore()
                self._ensure_toast().done(tr("正在停止批量禁用，请稍候"), ok=False, hold_ms=2500)
                return
        if disable_worker is not None and not getattr(disable_worker, "committed", False):
            # 关闭期间可能不再投递排队信号，直接提交尚未处理的禁用结果。
            try:
                self._on_disable_batch_finished(getattr(disable_worker, "results", None) or [], True)
            except Exception as e:
                logger.warning("关窗前补提交批量禁用失败: %s", e)
        if disable_worker is not None:
            self._clear_disable_ui(disable_worker)
        export_ctl = getattr(self, "_export_controller", None)
        export_worker = getattr(export_ctl, "_worker", None) if export_ctl else None
        if export_worker is not None and export_worker.isRunning():
            # 导出不修改库数据，可直接请求中断；关闭时禁止完成回调自动打开输出目录。
            export_ctl._open_dir_on_finish = False
            export_worker.terminate_export()
            if not export_worker.wait(5000):
                event.ignore()
                self._ensure_toast().done(tr("正在停止导出，请稍候"), ok=False, hold_ms=2500)
                return
        if self._stack.currentIndex() == self.PAGE_GAME and not self._game_page.confirm_pending_changes():
            event.ignore()
            return
        if not self._stop_async_deploys():
            event.ignore()
            self._ensure_toast().done(tr("正在停止 Mod 部署，请稍候"), ok=False, hold_ms=2500)
            return
        from src.ui.mod_update_actions import confirm_stop_updates

        svc = self._mod_update_service
        if svc.active_update_ids(all_games=True) and not confirm_stop_updates(self, svc, exiting=True):
            event.ignore()
            return
        if not svc.shutdown_updates():
            event.ignore()
            self._ensure_toast().done(tr("正在停止 Mod 更新，请稍候"), ok=False, hold_ms=2500)
            return
        operations = getattr(self, "_plugin_operations", None)
        if operations is not None and not operations.shutdown():
            event.ignore()
            self._ensure_toast().done(tr("正在停止插件操作，请稍候"), ok=False, hold_ms=2500)
            return
        # 保存窗口尺寸和最大化状态；最大化时记录 normalGeometry，以便正确还原。
        try:
            is_max = self.isMaximized()
            self._config.set("ui.window_maximized", bool(is_max))
            geo = self.normalGeometry() if is_max else self.geometry()
            if geo.width() > 0 and geo.height() > 0:
                self._config.set("ui.window_width", int(geo.width()))
                self._config.set("ui.window_height", int(geo.height()))
        except Exception as e:
            logger.warning("持久化窗口尺寸失败: %s", e)
        # 退出前、停止监控和关闭数据库前再采集一次运行时变体，
        # 读取加载器在游戏退出时保存到 d3dx_user.ini 的最新值。
        try:
            self._mod_manager.variants.collect(trigger="app_exit")
        except Exception as e:
            logger.warning("退出前变体采集失败: %s", e)
        # 停止文件监控
        roots = getattr(self, "_deployment_root_watch", None)
        if roots is not None:
            roots.close()
        self._file_watcher.stop()
        self._deploy_watcher.stop()
        self._deploy_state_poll.stop()
        self._source_state_poll.stop()
        self._mod_manager.stop_ini_sync()
        self._mod_manager.variants.stop_watch()  # 停 d3dx_user.ini 监视
        # 停用全部插件（调用 deactivate + 注销订阅/命令）
        if getattr(self, "_plugin_manager", None) is not None:
            try:
                self._plugin_manager.shutdown()
            except Exception as e:
                logger.warning("关闭插件系统失败: %s", e)
        # 关闭前刷新待持久化的预览图重排
        self._overview_page._flush_pending_preview_reorder()
        # 停止总览页后台图片/详情线程
        self._image_memory_monitor.stop()
        for attr in ("_pixmap_worker", "_panel_preview_worker", "_key_bindings_worker"):
            worker = getattr(self._overview_page, attr, None)
            if worker is not None and worker.isRunning():
                worker.stop()
                worker.wait(500)
        # 停止后台图片加载线程（画廊页）
        if hasattr(self._gallery_page, "_pixmap_worker"):
            self._gallery_page._pixmap_worker.stop()
            self._gallery_page._pixmap_worker.wait(500)
        # 关闭 GameBanana 后台资源（worker loop + httpx client）
        if hasattr(self, "_gb_store_page") and self._gb_store_page is not None:
            try:
                self._gb_store_page.shutdown()
            except Exception as e:
                logger.warning("关闭 GB 后台失败: %s", e)
        # 关闭图片加载器的共享事件循环，并等待剩余请求完成取消和资源释放。
        if hasattr(self, "_gb_image_loader") and self._gb_image_loader is not None:
            try:
                self._gb_image_loader.shutdown()
            except Exception as e:
                logger.warning("关闭 ImageLoader 失败: %s", e)
        self._appearance_disposed = True
        queue = getattr(self, "_appearance_queue", None)
        if queue is not None:
            queue.dispose()
        if hasattr(self, "_bg_blur_throttle"):
            self._bg_blur_throttle.stop()
        if getattr(self, "_bg_overlay", None) is not None:
            if hasattr(self, "_background_controller"):
                self._background_controller.dispose()
            self._bg_overlay.dispose()
        logger.info("应用关闭")
        super().closeEvent(event)

    # 预设

    def _refresh_preset_combo(self, rename_id: str | None = None) -> None:
        """刷新总览页预设选择器。rename_id 给出时进入该预设的内联重命名"""
        gid = self._game_service.current_game_id
        if not gid:
            return
        pm = self._mod_manager.preset_manager
        pm.ensure_default(gid)
        presets = pm.get_all_presets(gid)
        cur = pm.get_current(gid)
        self._overview_page.set_presets(presets, cur.id, rename_id=rename_id)

    @_invalidates_library_probe
    def _on_preset_apply(self, preset_id: str, *, after_success=None) -> None:
        """用户点了某个预设的「应用」按钮"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if not preset_id:
            return
        pm = self._mod_manager.preset_manager
        gid = self._game_service.current_game_id
        if pm.get_current(gid).id == preset_id:
            if after_success is not None:
                after_success()
            return
        if self._preset_apply_worker is not None or getattr(self, "_preset_apply_preparing", False):
            return
        if self._deploy_workers or getattr(self, "_disable_worker", None) is not None:
            QMessageBox.information(self, tr("暂时无法切换预设"), tr("Mod 正在后台部署，请等待部署完成后重试。"))
            return
        from src.ui.dialogs.preset_progress import PresetProgressDialog

        target = pm.get_preset(preset_id)
        dialog = PresetProgressDialog(target.display_name if target is not None else tr("所选预设"), self)
        selector = getattr(self._overview_page, "_preset_selector", None)
        if selector is not None:
            selector.setEnabled(False)
        self._preset_apply_progress = dialog
        self._preset_apply_game_id = gid
        self._preset_apply_preparing = True
        self._preset_apply_cancelled = False
        self._preset_apply_completed = after_success
        self._preset_journal_error = None
        self._preset_apply_plan = None
        self._preset_preparation = None
        token = self._preset_apply_token = object()
        self._game_page.setEnabled(False)
        dialog.canceled.connect(self._cancel_preset_apply)
        dialog.show()
        call_later(0, self, lambda: self._begin_preset_preparation(preset_id, token))

    def _begin_preset_preparation(self, preset_id, token) -> None:
        """在有效任务令牌下准备预设相关 Mod，独占保护涉及的库路径并启动后台预检。"""
        if self._preset_apply_token is not token:
            return
        if self._preset_apply_cancelled:
            self._clear_preset_apply_ui()
            return
        from src.ui.preset_deploy_worker import DeploymentPhaseWorker

        try:
            pm = self._mod_manager.preset_manager
            target = pm.get_preset(preset_id)
            current = pm.get_current(self._mod_manager.current_game_id)
            mod_ids = set(target.enabled_mod_ids) | set(current.enabled_mod_ids)
            mod_ids.update(
                mod.mod_id for mod in self._mod_manager._index.get_enabled_mods(self._mod_manager.current_game_id)
            )
            preparation = self._mod_manager.create_deployment_preparation(
                trigger="preset_apply_before_io", mod_ids=mod_ids
            )
            self._preset_preparation = preparation
            self._mod_manager.protect_library_operation(
                "preset-apply",
                mod_ids=tuple(mod.mod_id for mod in preparation.mods),
                paths=tuple(p for mod in preparation.mods for p in (mod.path, mod.deployed_path)),
                exclusive=True,
            )
            worker = DeploymentPhaseWorker(preparation.prepare, self)
            self._set_preset_worker(worker, lambda w: self._on_preset_prepared(w, preset_id))
        except Exception as error:
            self._finish_preset_apply_failure(None, error)

    def _set_preset_worker(self, worker, callback) -> None:
        """接管新阶段工作线程，释放上一阶段对象并绑定仅消费一次的完成回调。"""
        previous = self._preset_apply_worker
        self._preset_apply_worker = worker
        if previous is not None:
            previous.deleteLater()
        worker._owner_finished = callback
        worker._owner_consumed = False
        worker.finished.connect(lambda w=worker: self._consume_preset_worker(w))
        worker.start(QThread.Priority.LowPriority)

    def _consume_preset_worker(self, worker) -> None:
        """仅消费当前阶段工作线程的完成结果一次，回调异常交由统一失败流程结算。"""
        if self._preset_apply_worker is not worker or worker._owner_consumed:
            return
        worker._owner_consumed = True
        try:
            worker._owner_finished(worker)
        except Exception as error:
            self._finish_preset_apply_failure(self._preset_apply_plan, error)

    def _on_preset_prepared(self, worker, preset_id) -> None:
        """准备成功且未取消时建立部署计划、登记执行阶段并启动后台文件操作。"""
        if worker.error is not None:
            self._finish_preset_apply_failure(None, worker.error)
            return
        if self._preset_apply_cancelled:
            self._clear_preset_apply_ui()
            return
        pm = self._mod_manager.preset_manager
        plan = pm.prepare_preset_apply(preset_id, preparation=self._preset_preparation)
        self._preset_apply_plan = plan
        self._preset_apply_preparing = False
        pm.mark_preset_apply_executing(plan.operation_id)
        from src.ui.preset_deploy_worker import PresetDeployWorker

        worker = PresetDeployWorker(plan, self._mod_manager._storage, self, preparation=self._preset_preparation)
        self._preset_preparation.patch_sink = worker.persist_patch_on_owner
        worker.patch_ready.connect(lambda request, w=worker: self._persist_preset_patch(w, request))
        worker.progress.connect(self._on_preset_apply_progress)
        worker.phase.connect(self._on_preset_apply_phase)
        worker.step_completed.connect(lambda steps, op=plan.operation_id: self._record_preset_steps(op, steps))
        self._preset_apply_progress.show_phase("deploy", tr("正在执行预设部署…"))
        self._set_preset_worker(worker, self._on_preset_execution_finished)

    def _cancel_preset_apply(self) -> None:
        """标记取消并请求可中断阶段停止，必须完成的回滚或发布阶段继续执行。"""
        self._preset_apply_cancelled = True
        worker = self._preset_apply_worker
        if worker is not None and not getattr(worker, "_must_finish", False):
            worker.requestInterruption()
        dialog = self._preset_apply_progress
        if dialog is not None:
            dialog.show_cancelling()

    def _on_preset_execution_finished(self, worker) -> None:
        """移除后台持久化入口，再按执行错误、日志写入和取消状态选择提交或回滚。"""
        self._preset_preparation.patch_sink = None
        plan = self._preset_apply_plan
        journal_error = getattr(self, "_preset_journal_error", None)
        if worker.error is not None:
            error = worker.error
            if journal_error is not None:
                from src.core.preset_apply_service import PresetApplyError

                error = PresetApplyError(
                    tr("保存预设执行进度失败：{journal_error}", journal_error=journal_error),
                    completed=getattr(error, "completed", ()),
                    rollback_errors=getattr(error, "rollback_errors", ()),
                )
            self._finish_preset_apply_failure(plan, error)
        elif journal_error is not None:
            self._rollback_preset_apply(plan, worker.completed, journal_error)
        elif self._preset_apply_cancelled:
            self._rollback_preset_apply(plan, worker.completed, InterruptedError("preset apply cancelled"))
        else:
            self._finish_preset_apply_success(plan, worker.completed)

    def _record_preset_steps(self, operation_id, steps) -> None:
        """为当前预设操作持久化已完成步骤，写入失败时记录原因并中断后续部署。"""
        plan = self._preset_apply_plan
        if plan is None or plan.operation_id != operation_id:
            return
        try:
            self._mod_manager.preset_manager.record_preset_apply_steps(operation_id, steps)
        except Exception as error:
            self._preset_journal_error = error
            worker = self._preset_apply_worker
            if worker is not None:
                worker.requestInterruption()

    def _persist_preset_patch(self, worker, request) -> None:
        """在数据库所属线程保存运行时日志补丁，无论成功或失败都唤醒等待结果的工作线程。"""
        if request.done.is_set():
            return
        try:
            self._mod_manager.preset_manager._get_index().update_journal_runtime_patch(
                request.operation_id, request.patch_json
            )
        except Exception as error:
            request.error = error
        finally:
            request.done.set()

    def _on_preset_apply_progress(self, index: int, total: int, action, frac: float) -> None:
        """把预设文件操作的项目进度、动作名称和目标目录同步到仍存在的进度窗口。"""
        dialog = self._preset_apply_progress
        if dialog is None:
            return
        verbs = {"park": tr("停用"), "restore": tr("恢复"), "materialize_active": tr("首次部署")}
        name = Path(action.active_path).name
        dialog.set_item_progress(index, total, verbs.get(action.action, action.action), name, frac)

    def _on_preset_apply_phase(self, label) -> None:
        """切换到进度未知的预设处理阶段，并更新阶段说明。"""
        dialog = self._preset_apply_progress
        if dialog is not None:
            dialog.setRange(0, 0)
            dialog.setLabelText(label)

    @_invalidates_library_probe
    def _finish_preset_apply_success(self, plan, completed) -> None:
        """提交预设数据库状态，失败时回滚文件操作；提交成功后执行不可取消的同步阶段。"""
        preparation = self._preset_preparation
        dialog = self._preset_apply_progress
        if dialog is not None:
            dialog.show_phase("finalize", tr("正在提交预设状态…"))
            dialog.setCancelButton(None)
        try:
            affected = self._mod_manager.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
        except Exception as exc:
            self._rollback_preset_apply(plan, completed, exc)
            return
        from src.ui.preset_deploy_worker import DeploymentPhaseWorker

        if dialog is not None:
            dialog.setLabelText(tr("预设已提交，正在完成同步…"))
        worker = DeploymentPhaseWorker(lambda: preparation.after_commit(plan), self)
        worker._must_finish = True
        self._set_preset_worker(worker, lambda w: self._finish_preset_publication(w, plan, affected))

    def _finish_preset_publication(self, worker, plan, affected) -> None:
        """发布已提交预设的引用和界面状态，清理切换资源后调用仍有效的后续操作。"""
        follow_up = getattr(self, "_preset_apply_completed", None) if not self._preset_apply_cancelled else None
        try:
            # 数据库已经提交，后续界面或同步错误不能触发回滚。
            if worker.error is not None:
                logger.warning("预设已提交，后续同步失败：%s", worker.error)
            preparation = self._preset_preparation
            preparation.publish_ini_refs(self._mod_manager, plan)
            self._mod_manager.preset_manager.finish_prepared_apply(plan, affected, preparation)
            if self._preset_apply_progress is not None:
                self._preset_apply_progress.setRange(0, 1000)
                self._preset_apply_progress.setValue(1000)
            target = self._mod_manager.preset_manager.get_preset(plan.to_preset_id)
            self._toast_done(tr("已切换到「{name}」", name=target.display_name) if target else tr("预设已切换"))
        finally:
            self._clear_preset_apply_ui()
        if follow_up is not None:
            follow_up()

    def _rollback_preset_apply(self, plan, completed, error) -> None:
        """启动必须完成的后台回滚阶段，并在结束后统一报告原始错误和恢复结果。"""
        from src.core.preset_apply_service import PresetApplyError
        from src.ui.preset_deploy_worker import DeploymentPhaseWorker

        if self._preset_apply_progress is not None:
            self._preset_apply_progress.show_rollback()
        worker = DeploymentPhaseWorker(lambda: self._preset_preparation.rollback(plan, completed), self)
        worker._must_finish = True

        def finished(w):
            """合并原始失败原因与回滚异常，再交给预设失败流程保存恢复状态。"""
            errors = (str(w.error),) if w.error else w.result
            wrapped = PresetApplyError(str(error), completed=completed, rollback_errors=errors)
            self._finish_preset_apply_failure(plan, wrapped)

        self._set_preset_worker(worker, finished)

    @_invalidates_library_probe
    def _finish_preset_apply_failure(self, plan, error) -> None:
        """记录切换失败与恢复状态，释放界面资源，并按取消和回滚结果决定是否提示。"""
        details = str(error)
        rollback_errors = getattr(error, "rollback_errors", ())
        try:
            if plan is not None:
                preparation = getattr(self, "_preset_preparation", None)
                self._mod_manager.preset_manager.fail_preset_apply(plan, error, preparation=preparation)
                if preparation is not None:
                    preparation.publish_ini_refs(self._mod_manager, plan, notify_variants=False)
        except Exception as failure:
            logger.exception("预设失败状态保存异常")
            details += tr("\n\n保存恢复状态失败：{failure}", failure=failure)
        finally:
            self._clear_preset_apply_ui()
        if rollback_errors:
            details += tr("\n\n回滚未完成：\n") + "\n".join(rollback_errors)
        if not getattr(self, "_preset_apply_cancelled", False) or rollback_errors:
            from src.core.operation_preflight import OperationPreflightError

            cause = error
            while cause is not None and not isinstance(cause, OperationPreflightError):
                cause = cause.__cause__
            if isinstance(cause, OperationPreflightError):
                self._mod_manager.report_operation_failure(cause)
            else:
                QMessageBox.critical(self, tr("预设切换失败"), details)

    @_invalidates_library_probe
    def _clear_preset_apply_ui(self) -> None:
        """工作线程停止后释放预设操作保护及进度窗口，恢复选择控件和延后任务。"""
        worker = self._preset_apply_worker
        if worker is not None and worker.isRunning():
            return
        self._mod_manager.release_library_operation("preset-apply")
        if self._preset_apply_progress is not None:
            try:
                self._preset_apply_progress.canceled.disconnect(self._cancel_preset_apply)
            except TypeError:
                pass
            self._preset_apply_progress.hide()
            self._preset_apply_progress.deleteLater()
        self._preset_apply_progress = None
        self._preset_apply_worker = None
        self._preset_apply_preparing = False
        self._preset_apply_token = None
        self._preset_apply_game_id = ""
        self._preset_apply_plan = None
        self._preset_preparation = None
        self._preset_apply_completed = None
        if worker is not None:
            worker.deleteLater()
        selector = getattr(self._overview_page, "_preset_selector", None)
        if selector is not None:
            selector.setEnabled(True)
        self._game_page.setEnabled(True)
        resume = getattr(self, "_resume_deferred_library_environment", None)
        if resume is not None:
            resume()

    def _stop_preset_apply(self, timeout_ms=5000) -> bool:
        """关闭事件无法投递排队信号时，仍处理已完成的工作步骤。"""
        self._cancel_preset_apply()
        deadline = time.monotonic() + timeout_ms / 1000
        while self._preset_apply_worker is not None:
            worker = self._preset_apply_worker
            request = getattr(worker, "pending_patch", None)
            if request is not None:
                self._persist_preset_patch(worker, request)
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if not worker.wait(min(50, remaining)):
                if time.monotonic() >= deadline:
                    return False
                continue
            self._consume_preset_worker(worker)
        self._clear_preset_apply_ui()
        return True

    @_invalidates_library_probe
    def _on_preset_applied(self, _affected_ids: list) -> None:
        """发布已提交差异，文件监控另行对账。"""
        self._refresh_preset_combo()
        mods = [mod for mid in _affected_ids if (mod := self._mod_manager.get_mod(mid)) is not None]
        self._overview_page.update_mods_in_place(mods)
        chars = self._mod_manager.get_characters_for_ui(self._game_service.current_game_id)
        self._overview_page.set_characters(chars, refresh=False)
        if self._stack.currentIndex() == self.PAGE_GALLERY and hasattr(self._gallery_page, "refresh_from_index"):
            self._gallery_page.refresh_from_index()

    @_invalidates_library_probe
    def _on_preset_rename(self, preset_id: str, new_name: str) -> None:
        """内联重命名提交"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        pm = self._mod_manager.preset_manager
        if not new_name.strip():
            QMessageBox.warning(self, tr("提示"), tr("名称不能为空"))
            return
        if not pm.update_preset_meta(preset_id, name=new_name):
            QMessageBox.warning(self, tr("提示"), tr("此预设已不存在，请刷新后重试"))
        # preset_list_changed 信号会触发刷新

    @_invalidates_library_probe
    def _on_preset_create(self) -> None:
        """新建预设 → 默认名 + 立即进入重命名模式

        新预设是**空预设**（character_mods={}），不继承任何状态。
        - 切换到新预设：全部 Mod 被关闭
        - 在新预设下启用 Mod：自动写入新预设
        - 若想复制现有状态：选中预设后 Ctrl+C / Ctrl+V
        """
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        gid = self._game_service.current_game_id
        pm = self._mod_manager.preset_manager
        base_name = pm.generate_unique_name(gid, tr("新建预设"))
        try:
            new_p = pm.create_preset(gid, base_name)
        except ValueError as e:
            QMessageBox.warning(self, tr("提示"), str(e))
            return
        # preset_list_changed 已经触发常规刷新；这里再调一次带 rename_id 的刷新让选择器进入编辑模式
        self._refresh_preset_combo(rename_id=new_p.id)

    @_invalidates_library_probe
    def _on_preset_delete_batch(self, preset_ids: list) -> None:
        """确认显式选择后异步切换并删除。"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        if not preset_ids:
            return
        gid = self._game_service.current_game_id
        if self._preset_apply_worker is not None:
            return
        pm = self._mod_manager.preset_manager
        deletable = [
            preset
            for pid in dict.fromkeys(preset_ids)
            if (preset := pm.get_preset(pid)) is not None and preset.game_id == gid and not preset.is_default
        ]
        if not deletable:
            QMessageBox.warning(self, tr("提示"), tr("默认预设不可删除"))
            return
        from src.ui.dialogs.preset_progress import confirm_preset_delete

        cur_id = pm.get_current(gid).id
        if not confirm_preset_delete(self, deletable, cur_id):
            return
        if (
            gid != self._game_service.current_game_id
            or _plugin_operation_busy(self)
            or _deployment_operation_busy(self)
        ):
            return
        ids = [preset.id for preset in deletable]
        if cur_id in ids:
            default = pm.ensure_default(gid)
            self._on_preset_apply(default.id, after_success=lambda: self._start_preset_delete(ids, gid))
        else:
            self._start_preset_delete(ids, gid)

    def _start_preset_delete(self, preset_ids, game_id):
        """为当前游戏建立预设删除进度，锁定游戏与预设选择，再延后启动准备阶段。"""
        if game_id != self._game_service.current_game_id:
            return
        from src.ui.dialogs.preset_progress import PresetProgressDialog

        pm = self._mod_manager.preset_manager
        names = [preset.name for pid in preset_ids if (preset := pm.get_preset(pid)) is not None]
        dialog = PresetProgressDialog(
            names[0] if len(names) == 1 else tr("{count} 个预设", count=len(names)), self, deleting=True
        )
        self._preset_delete_progress = dialog
        self._preset_delete_game_id = game_id
        self._preset_delete_candidate = None
        self._preset_delete_worker = None
        self._preset_delete_preparing = True
        self._preset_delete_cancelled = False
        self._overview_page._preset_selector.setEnabled(False)
        self._game_page.setEnabled(False)
        dialog.canceled.connect(self._cancel_preset_delete)
        dialog.show()
        call_later(0, self, lambda: self._begin_preset_delete(preset_ids, game_id))

    def _begin_preset_delete(self, preset_ids, game_id):
        """准备预设删除候选并保护相关库路径，在后台写入待提交的预设变更。"""
        if self._preset_delete_cancelled:
            self._finish_preset_delete()
            return
        from src.core.preset_delete import write_prepared_preset_delete
        from src.ui.preset_deploy_worker import DeploymentPhaseWorker

        try:
            candidate = self._mod_manager.preset_manager.prepare_delete_presets(preset_ids, game_id)
            self._preset_delete_candidate = candidate
            self._mod_manager.protect_library_operation(
                "preset-delete",
                paths=(candidate.target, self._mod_manager._storage.get_deploy_dir()),
                mod_ids=tuple(record.mod_id for record in candidate.cleanup_records),
                exclusive=True,
            )
            worker = DeploymentPhaseWorker(lambda: write_prepared_preset_delete(candidate), self)
            self._preset_delete_worker = worker
            self._preset_delete_preparing = False
            self._preset_delete_progress.show_phase("deploy", tr("正在保存预设变更…"))
            worker.finished.connect(self._preset_delete_written)
            worker.start(QThread.Priority.LowPriority)
        except Exception as error:
            self._finish_preset_delete(error)

    def _preset_delete_written(self):
        """接收当前删除任务的写入结果，提交预设变更后启动不可取消的无用缓存清理。"""
        worker = self.sender()
        if worker is not self._preset_delete_worker:
            return
        candidate = self._preset_delete_candidate
        if self._preset_delete_cancelled or worker.error is not None:
            self._finish_preset_delete(None if self._preset_delete_cancelled else worker.error)
            return
        try:
            self._mod_manager.preset_manager.commit_delete_presets(candidate)
            self._preset_delete_progress.setCancelButton(None)
            if not candidate.cleanup_records:
                self._finish_preset_delete()
                return
            from src.ui.preset_deploy_worker import PresetCleanupWorker

            cleanup = PresetCleanupWorker(
                candidate.cleanup_records, self._mod_manager._storage.get_deploy_dir(), self._mod_manager._storage, self
            )
            cleanup._must_finish = True
            self._preset_delete_worker = cleanup
            worker.deleteLater()
            self._preset_delete_progress.show_phase("finalize", tr("正在清理不再使用的缓存…"))
            cleanup.progress.connect(self._on_preset_delete_progress)
            cleanup.finished.connect(self._preset_delete_cleanup_finished)
            cleanup.start(QThread.Priority.LowPriority)
        except Exception as error:
            self._finish_preset_delete(error)

    def _on_preset_delete_progress(self, done, total, name):
        """将预设缓存清理的完成数量和当前项目同步到删除进度窗口。"""
        dialog = self._preset_delete_progress
        if dialog is not None:
            dialog.setRange(0, total)
            dialog.setValue(done)
            dialog.progress_label.setText(tr("{done} / {total} 项", done=done, total=total))
            dialog.setLabelText(tr("清理缓存 · {name}", name=name))

    def _preset_delete_cleanup_finished(self):
        """发布当前任务已移除的预设部署记录，保留清理失败项并完成删除结算。"""
        worker = self.sender()
        if worker is not self._preset_delete_worker:
            return
        try:
            if worker.error is not None:
                raise worker.error
            removed, errors = worker.result
            self._mod_manager.preset_manager.publish_deleted_preset_deployments(removed, self._preset_delete_game_id)
            if errors:
                logger.warning("部分预设缓存未清理，将保留到下次清理: %s", errors)
            self._finish_preset_delete()
        except Exception as error:
            self._finish_preset_delete(error)

    def _cancel_preset_delete(self):
        """仅在预设删除尚未提交时登记取消，并通知候选写入与工作线程停止。"""
        candidate = getattr(self, "_preset_delete_candidate", None)
        if candidate is not None and candidate.committed:
            return
        self._preset_delete_cancelled = True
        if candidate is not None:
            candidate.cancelled.set()
        worker = getattr(self, "_preset_delete_worker", None)
        if worker is not None:
            worker.requestInterruption()

    def _finish_preset_delete(self, error=None):
        """丢弃未提交候选、释放操作保护和进度窗口，按提交状态提示结果并继续关闭或延后任务。"""
        from src.core.preset_delete import discard_prepared_preset_delete

        candidate = getattr(self, "_preset_delete_candidate", None)
        worker = getattr(self, "_preset_delete_worker", None)
        if candidate is not None and not candidate.committed:
            discard_prepared_preset_delete(candidate)
        self._preset_delete_worker = None
        self._preset_delete_preparing = False
        if worker is not None:
            worker.deleteLater()
        self._mod_manager.release_library_operation("preset-delete")
        dialog = self._preset_delete_progress
        self._preset_delete_progress = None
        dialog.hide()
        dialog.deleteLater()
        self._overview_page._preset_selector.setEnabled(True)
        self._game_page.setEnabled(True)
        if error is not None:
            QMessageBox.critical(
                self,
                tr("预设删除失败") if not candidate or not candidate.committed else tr("缓存清理未完成"),
                str(error),
            )
        elif candidate is not None and candidate.committed:
            self._toast_done(tr("已删除 {count} 个预设", count=len(candidate.removed_presets)))
        self._preset_delete_candidate = None
        if getattr(self, "_preset_delete_close_pending", False):
            self._preset_delete_close_pending = False
            call_later(0, self, self.close)
        else:
            self._resume_deferred_library_environment()

    @_invalidates_library_probe
    def _on_preset_duplicate_batch(self, preset_ids: list) -> None:
        """Ctrl+V 粘贴：为每个 id 复制一份，命名带 copy 后缀避免冲突"""
        if _plugin_operation_busy(self) or _deployment_operation_busy(self):
            return
        gid = self._game_service.current_game_id
        pm = self._mod_manager.preset_manager
        for src_id in preset_ids:
            src = pm.get_preset(src_id)
            if not src:
                continue
            new_name = pm.generate_unique_name(gid, src.display_name + tr(" 副本"))
            try:
                # copied_from 让运行时变体偏好把源预设下各 Mod 的变体状态一并复制过去
                pm.create_preset(gid, new_name, snapshot=src.character_mods, copied_from=src.id)
            except ValueError as e:
                logger.warning("复制预设失败 %s: %s", src_id, e)
