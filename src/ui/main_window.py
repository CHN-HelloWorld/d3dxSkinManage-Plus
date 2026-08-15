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
