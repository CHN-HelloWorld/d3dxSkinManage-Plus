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
