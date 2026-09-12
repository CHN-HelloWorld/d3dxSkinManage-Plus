"""设置页 - 游戏自定义配置、主题立即生效、i18n"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from time import perf_counter

import shiboken6
from PySide6.QtCore import QObject, QRect, QSignalBlocker, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from src.core.archive_concurrency import (
    MAX_ARCHIVE_WORKERS,
    effective_archive_workers,
    recommended_archive_workers,
)
from src.core.config import Config
from src.core.deploy_concurrency import (
    MAX_DEPLOY_CONCURRENCY,
    effective_deploy_concurrency,
    recommended_deploy_concurrency,
)
from src.core.mod_update_concurrency import (
    MAX_UPDATE_CONCURRENCY,
    effective_update_concurrency,
    recommended_update_concurrency,
)
from src.ui import theme
from src.ui.fonts import installed_families
from src.ui.shortcut_manager import ShortcutSettings
from src.ui.widgets.background_settings import BackgroundSettingsMixin
from src.ui.widgets.font_picker import install_font_previews
from src.ui.widgets.image_memory_settings import ImageMemorySettings
from src.ui.widgets.int_line_edit import IntLineEdit
from src.ui.widgets.settings_components import (
    CacheSplit,
    ChoiceSegments,
    SettingsRow,
    ThemeChoices,
    plain_widget,
    settings_note,
    style_button,
    text_label,
)
from src.ui.widgets.settings_navigation import SettingsCategoryPage
from src.ui.widgets.settings_shell import SettingsShellMixin
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import get_i18n, t
from src.utils.i18n import t as tr
from src.utils.language_pack import normalize_language_code
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class _ProxyTestWorker(QObject):
    """在独立线程测试代理连通性与延迟，结果通过信号返回。"""

    finished = Signal(bool, str)

    def __init__(self, proxy: str | None) -> None:
        """保存待检测的代理地址，将连接测试留给后台执行入口。"""
        super().__init__()
        self._proxy = proxy

    def run(self) -> None:
        """在后台检测代理连通性和 HTTP 延迟，并将成功结果或异常说明统一发回界面。"""
        # 先通过 TCP 握手检查代理端口，再经代理请求 generate_204，测量实际 HTTP 往返延迟。
        from src.utils.proxy import measure_proxy_latency

        try:
            ok, msg = measure_proxy_latency(self._proxy, timeout=6.0)
        except Exception as e:
            ok, msg = False, tr("测试失败：{e}", e=e)
        self.finished.emit(ok, msg)


class _CacheSizeWorker(QObject):
    """后台统计缓存目录总占用；rglob 大目录可能上千文件，放线程避免卡 UI。"""

    finished = Signal(float)  # 占用 MB

    def run(self) -> None:
        """尽力累计可读取缓存文件的大小，以 MiB 返回统计结果。"""
        from src.utils.paths import get_cache_dir

        total = 0
        try:
            for f in get_cache_dir().rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        self.finished.emit(total / (1024 * 1024))


class _UsageBar(QWidget):
    """占用条（下载数据行）：整条 = 总占用，左侧高亮段 = 其中「已入库可清理」的比例。

    颜色在 paintEvent 里现取 `theme.color`，故换主题自动跟随，无需登记重涂器。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建固定高度的透明用量条，初始可清理比例为零。"""
        super().__init__(parent)
        self._ratio = 0.0
        self.setFixedHeight(6)
        self.setMinimumWidth(200)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_ratio(self, ratio: float) -> None:
        """将用量比例限制在零到一之间，并请求重绘。"""
        self._ratio = max(0.0, min(1.0, float(ratio or 0.0)))
        self.update()

    def paintEvent(self, event):
        """绘制下载空间底轨及可清理已安装归档所占比例，保持两端圆角。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect()
        radius = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.color("settings.line")))
        p.drawRoundedRect(r, radius, radius)
        # 用独立颜色标识可清理的已安装资源包。
        if self._ratio > 0:
            w = max(2, int(r.width() * self._ratio))
            p.setBrush(QColor(theme.color("settings.teal")))
            p.save()
            p.setClipRect(QRect(r.left(), r.top(), w, r.height()))
            p.drawRoundedRect(r, radius, radius)
            p.restore()


class SettingsPage(BackgroundSettingsMixin, SettingsShellMixin, QWidget):
    """设置页面"""

    settings_changed = Signal()
    theme_changed = Signal(str)
    font_changed = Signal(str)
    language_changed = Signal(str)
    background_changed = Signal()  # 背景配置变化后重新应用相关样式。
    bg_playback_requested = Signal(str)
    bg_overlay_refresh = Signal()  # 暗度 / 模糊 / 填充调整 → 只轻量刷新 overlay（不全窗换肤）
    bg_scrim_live = Signal(int)  # 背景暗度滑块拖动实时值（轻量、拖动中即时反馈）
    bg_blur_live = Signal(int)  # 背景模糊滑块拖动实时值（拖动中即时反馈，主窗节流）
    app_update_check_requested = Signal()  # 用户点「检查程序更新」
    # 下载数据清理：本页只管 UI，「已入库」集合要查 DB（连接线程绑定，只能主线程），
    # 故统计与删除都由宿主 MainWindow 代办 —— 这两个信号是唯一出口。
    download_usage_requested = Signal()  # 请求刷新「下载占用」统计
    download_purge_requested = Signal()  # 用户已在确认弹窗点「清理」→ 执行删除
    # 清理磁盘缓存后通知宿主同时清除客户端内存缓存，避免继续使用旧结果。
    api_cache_clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, shortcuts: ShortcutSettings | None = None):
        """建立设置控件与分批换肤队列，先回填配置，再连接自动保存以避免初始化写盘。"""
        super().__init__(parent)
        self._config = Config()
        self._shortcut_settings = shortcuts if shortcuts is not None else ShortcutSettings(self)
        # 登记主题相关的样式更新函数，切换主题时复用现有控件。
        self._reappliers: list = []
        self._retheme_pending: deque[Callable[[], None]] = deque()
        self._retheme_finished: Callable[[], None] | None = None
        self._retheme_generation = 0
        self._retheme_timer = QTimer(self)
        self._retheme_timer.setSingleShot(True)
        self._retheme_timer.timeout.connect(self._apply_retheme_batch)
        self._setup_ui()
        self._load_settings()
        # 加载配置后再连接自动保存信号，避免回填控件时触发写入。
        self._wire_autosave()

    def _restyle(self, widget, css_fn):
        """给 widget 套用 `css_fn` 生成的样式，并登记到重涂器——换主题时重跑 css_fn
        （css_fn 内部读 theme.color，故每次取当前主题色）。返回 widget 便于链式。"""

        def apply():
            """按当前主题计算控件样式，仅在样式文本变化时重新应用。"""
            css = css_fn()
            if widget.styleSheet() != css:
                widget.setStyleSheet(css)

        apply()
        self._reappliers.append(apply)
        return widget

    def retheme(self, *, on_finished: Callable[[], None] | None = None) -> None:
        """就地重涂；提供回调时分批刷新，让通知动画和输入继续处理。

        图标由 `icons.retheme_icons` 统一重涂；这里只管内联色样式。任一失败不影响其余。
        """
        self._retheme_timer.stop()
        self._retheme_generation += 1
        self._retheme_pending = deque(
            [
                *self._reappliers,
                self._load_background_settings,
                self._shortcuts_page.retheme,
            ]
        )
        self._retheme_finished = on_finished
        if on_finished is None:
            self._apply_retheme_batch(synchronous=True)
        else:
            self._retheme_timer.start(0)

    def _apply_retheme_batch(self, *, synchronous=False) -> None:
        """分批刷新样式并核对主题代次，异步模式按时间预算让出事件循环，完成后调用回调。"""
        generation = self._retheme_generation
        deadline = perf_counter() + 0.006
        while self._retheme_pending:
            fn = self._retheme_pending.popleft()
            try:
                fn()
            except Exception:
                pass
            if (not shiboken6.isValid(self)) or generation != self._retheme_generation:
                return
            if not synchronous and self._retheme_pending and perf_counter() >= deadline:
                self._retheme_timer.start(8)
                return
        callback, self._retheme_finished = self._retheme_finished, None
        if callback is not None:
            callback()

    def _build_general_page(self) -> QWidget:
        """构建 Mod 管理、统一代理、下载归档和图片缓存设置，并启动占用统计。"""
        # Mod 管理
        management_card, gml = self._make_group(tr("Mod 管理"))
        self._delete_method_combo = QComboBox()
        self._delete_method_combo.addItem(tr("放入回收站（可恢复）"), "recycle_bin")
        self._delete_method_combo.addItem(tr("直接删除（不可恢复）"), "permanent")
        self._delete_method_combo.setMinimumWidth(210)
        self._add_row(
            gml,
            tr("删除方式"),
            self._delete_method_combo,
            tr("用于单独或批量删除 Mod。"),
            details=tr(
                "删除类别仍按原方式直接删除。选择回收站时若 Windows 长路径或磁盘不支持，会再次询问是否改为直接删除。"
            ),
        )

        self._deploy_concurrency_input, self._deploy_concurrency_recommend_btn = self._add_concurrency_row(
            gml,
            tr("并发数量"),
            tr("同时部署的任务数量，范围 1–{MAX_DEPLOY_CONCURRENCY}。", MAX_DEPLOY_CONCURRENCY=MAX_DEPLOY_CONCURRENCY),
            maximum=MAX_DEPLOY_CONCURRENCY,
            recommended=recommended_deploy_concurrency(),
        )
        self._deploy_concurrency_input.setAccessibleName(tr("部署并发数量"))
        self._deploy_concurrency_recommend_btn.clicked.connect(self._set_recommended_deploy_concurrency)

        # 统一代理配置，在线 Mod 可独立决定是否启用。
        # 「代理总开关」控制代理是否全局可用 + 下方配置项的显隐；
        # 「Mod 商店是否使用代理」由 Mod 商店页的「启用代理」开关控制（需总开关已开）。
        gp, pg = self._make_group(tr("统一代理配置"), icon="globe")
        self._proxy_active_check = ToggleSwitch()
        self._proxy_active_check.toggled.connect(self._on_proxy_active_toggled)
        self._add_row(pg, tr("代理总开关"), self._proxy_active_check, tr("开启后，程序更新会使用此代理。"))

        # 总开关子配置：整块随总开关显示/隐藏
        self._proxy_cfg_sub = QWidget()
        self._proxy_cfg_sub.setStyleSheet("background: transparent;")
        sub = QVBoxLayout(self._proxy_cfg_sub)
        sub.setContentsMargins(0, 0, 0, 0)
        sub.setSpacing(0)

        self._proxy_system_check = ToggleSwitch()
        self._proxy_system_check.toggled.connect(self._on_proxy_system_toggled)
        self._add_row(sub, tr("使用系统代理"), self._proxy_system_check, tr("自动从 Windows 设置读取代理。"))

        # 手动配置区域（系统代理开启时隐藏）
        self._proxy_manual_widget = QWidget()
        self._proxy_manual_widget.setStyleSheet("background: transparent;")
        manual_rows = QVBoxLayout(self._proxy_manual_widget)
        manual_rows.setContentsMargins(0, 0, 0, 0)
        manual_rows.setSpacing(0)

        self._proxy_type_combo = QComboBox()
        self._proxy_type_combo.addItem("HTTP", "http")
        self._proxy_type_combo.addItem("HTTPS", "https")
        self._proxy_type_combo.addItem("SOCKS5", "socks5")
        self._proxy_type_combo.setMinimumWidth(140)
        self._add_row(manual_rows, tr("协议"), self._proxy_type_combo)

        self._proxy_host_input = QLineEdit()
        self._proxy_host_input.setPlaceholderText("127.0.0.1")
        self._proxy_host_input.setFixedWidth(210)
        self._add_row(manual_rows, tr("代理地址"), self._proxy_host_input, tr("通常使用本机地址 127.0.0.1。"))
        self._proxy_port_input = QLineEdit()
        self._proxy_port_input.setPlaceholderText("7890")
        self._proxy_port_input.setFixedWidth(72)
        self._add_row(manual_rows, tr("端口"), self._proxy_port_input, tr("与代理软件的端口一致。"))
        sub.addWidget(self._proxy_manual_widget)

        test_layout = QHBoxLayout()
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.setSpacing(8)
        self._proxy_test_btn = QPushButton(tr("测试连接"))
        self._proxy_test_btn.setFixedWidth(100)
        self._proxy_test_btn.clicked.connect(self._on_test_proxy)
        test_layout.addWidget(self._proxy_test_btn)
        self._proxy_test_label = QLabel("")
        self._proxy_test_label.setStyleSheet(
            f"color: {theme.surface_text_color('text.tertiary')}; background: transparent;"
        )
        test_layout.addWidget(self._proxy_test_label)
        self._add_row(sub, tr("连接测试"), test_layout, tr("经代理访问外网测量往返延迟。"))
        pg.addWidget(self._proxy_cfg_sub)

        # 下载归档与可再生图片缓存分别管理。
        gd, gdl = self._make_group(tr("下载空间"), icon="import")
        dl_summary = plain_widget()
        dl_metric = QVBoxLayout(dl_summary)
        dl_metric.setContentsMargins(0, 0, 20, 0)
        dl_metric.setSpacing(8)
        dl_metric.addWidget(text_label(tr("安装包总占用"), "smallHelp"))
        self._dl_total_label = text_label(tr("统计中…"), "metric")
        self._dl_total_unit = text_label("", "smallHelp")
        total_metric = QHBoxLayout()
        total_metric.setSpacing(5)
        total_metric.addWidget(self._dl_total_label, 0, Qt.AlignmentFlag.AlignBaseline)
        total_metric.addWidget(self._dl_total_unit, 0, Qt.AlignmentFlag.AlignBaseline)
        total_metric.addStretch(1)
        dl_metric.addLayout(total_metric)
        dl_metric.addWidget(text_label(tr("已下载的源压缩包"), "muted"))
        dl_metric.addStretch(1)
        dl_controls = plain_widget()
        dl_info = QVBoxLayout(dl_controls)
        dl_info.setContentsMargins(0, 0, 0, 0)
        dl_info.setSpacing(12)
        self._dl_usage_label = text_label(tr("正在统计下载数据…"), "help", wrap=True)
        self._dl_usage_label.setTextFormat(Qt.TextFormat.RichText)
        dl_info.addWidget(self._dl_usage_label)
        self._dl_usage_bar = _UsageBar()
        dl_info.addWidget(self._dl_usage_bar)
        legend = QHBoxLayout()
        self._dl_installed_label = text_label("", "muted")
        self._dl_pending_label = text_label("", "muted")
        legend.addWidget(self._dl_installed_label)
        legend.addStretch(1)
        legend.addWidget(self._dl_pending_label)
        dl_info.addLayout(legend)
        self._reappliers.append(self._render_download_usage)

        dl_btns = QHBoxLayout()
        dl_btns.setContentsMargins(0, 0, 0, 0)
        dl_btns.setSpacing(8)
        self._dl_purge_btn = style_button(QPushButton(tr("清理已安装")), icon="clean")
        self._dl_purge_btn.setEnabled(False)  # 统计回来且确有可清理项才放开
        self._dl_purge_btn.clicked.connect(self._on_purge_downloads)
        dl_btns.addWidget(self._dl_purge_btn)
        self._dl_open_btn = style_button(QPushButton(tr("打开目录")), icon="folder")
        self._dl_open_btn.clicked.connect(self._on_open_download_dir)
        dl_btns.addWidget(self._dl_open_btn)
        dl_btns.addStretch(1)
        dl_info.addLayout(dl_btns)
        self._dl_purge_result_label = text_label("", "muted", wrap=True)
        self._dl_purge_result_label.hide()
        dl_info.addWidget(self._dl_purge_result_label)
        dl_body = CacheSplit(dl_summary, dl_controls)
        gdl.addWidget(dl_body)
        self._reappliers.append(dl_body.retheme)

        download_policy, policy_rows = self._make_group(tr("安装后处理"), icon="box")
        self._dl_auto_delete_check = ToggleSwitch()
        self._add_row(
            policy_rows,
            tr("安装后自动删除安装包"),
            self._dl_auto_delete_check,
            tr("成功入库后删除源安装包，减少重复占用。"),
            details=tr(
                "关闭时保留安装包，便于重装或离线留档。"
                "「清理已安装」只删除已入库的源安装包，Mod 库中的完整副本会继续保留。"
                "未安装、安装失败或手动放入的下载均会保留。"
            ),
        )
        self._image_memory_settings = ImageMemorySettings(self._config)
        self._image_memory_settings.settings_changed.connect(self.settings_changed.emit)
        self._reappliers.append(self._image_memory_settings.retheme)

        # 磁盘限制和清理独立于内存图像预算。
        gc, gcl = self._make_group(
            t("磁盘缓存"), icon="disk", badge="DISK", description=tr("保留已下载的在线图片，减少重复下载。")
        )
        from src.ui.widgets.cache_cap_slider import CacheCapSlider

        self._img_cache_cap = CacheCapSlider()
        # 进度条当前值 = 实际缓存上限；右侧输入框+单位 = 自定义最大值（量程上界，固定）。两者各存一键。
        self._img_cache_cap.value_changed.connect(lambda mb: self._autosave("gamebanana.image_cache_max_mb", int(mb)))
        self._img_cache_cap.range_changed.connect(lambda mb: self._autosave("gamebanana.image_cache_range_mb", int(mb)))
        disk_summary = QWidget()
        summary_layout = QVBoxLayout(disk_summary)
        summary_layout.setContentsMargins(0, 0, 20, 0)
        summary_layout.setSpacing(10)
        summary_layout.addWidget(text_label(tr("当前磁盘占用"), "smallHelp"))
        metric = QHBoxLayout()
        metric.setContentsMargins(0, 0, 0, 0)
        metric.setSpacing(5)
        self._disk_usage_number = text_label(tr("统计中…"), "metric")
        self._disk_usage_unit = text_label("", "smallHelp")
        metric.addWidget(self._disk_usage_number, 0, Qt.AlignmentFlag.AlignBaseline)
        metric.addWidget(self._disk_usage_unit, 0, Qt.AlignmentFlag.AlignBaseline)
        metric.addStretch(1)
        summary_layout.addLayout(metric)
        summary_layout.addWidget(text_label(tr("含在线图片与其他可再生缓存"), "muted", wrap=True))
        summary_layout.addStretch(1)
        disk_body = CacheSplit(disk_summary, self._img_cache_cap)
        gcl.addWidget(disk_body)
        self._img_cache_cap.usage_changed.connect(self._update_disk_metric)
        self._reappliers.extend([self._img_cache_cap.retheme, disk_body.retheme])

        # 磁盘缓存清理入口。
        self._cache_clean_btn = style_button(QPushButton(t("清理磁盘缓存")), icon="clean")
        self._cache_clean_btn.clicked.connect(self._on_clean_all_cache)
        self._cache_clean_label = text_label("", "muted")
        self._cache_clean_label.setWordWrap(True)
        gc.add_footer(tr("清理可再生缓存，保留已下载的 Mod 压缩包。"), self._cache_clean_btn, self._cache_clean_label)
        # 首次进入即异步统计一次当前占用
        self._refresh_cache_usage()
        return SettingsCategoryPage(
            tr("常规"),
            [
                ("management", tr("Mod 管理"), tr("调整删除方式与部署时的任务并发。"), [management_card]),
                (
                    "network",
                    tr("网络代理"),
                    tr("在这里统一配置连接，再为在线 Mod 单独启用。"),
                    [gp, settings_note(tr("在线 Mod 还需在「在线 Mod → 在线 Mod 代理」中单独启用。"))],
                ),
                (
                    "downloads",
                    tr("下载数据"),
                    tr("查看下载占用，管理已经安装的源压缩包。"),
                    [
                        gd,
                        download_policy,
                        settings_note(tr("下载数据与可再生缓存分别管理；清理图片缓存不会删除这些安装包。")),
                    ],
                ),
                (
                    "cache",
                    tr("图片与缓存"),
                    tr("让图片加载更快，也让空间使用更清楚。"),
                    [self._image_memory_settings, gc],
                ),
            ],
        )
