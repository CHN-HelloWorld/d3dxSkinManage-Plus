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

    def _update_disk_metric(self, mb: float) -> None:
        """将缓存占用转换为合适的容量单位，负值表示统计尚未完成。"""
        if mb < 0:
            self._disk_usage_number.setText(tr("统计中…"))
            self._disk_usage_unit.clear()
            return
        unit, factor = ("TB", 1024**2) if mb >= 1024**2 else ("GB", 1024) if mb >= 1024 else ("MB", 1)
        self._disk_usage_number.setText(f"{mb / factor:.2f}".rstrip("0").rstrip("."))
        self._disk_usage_unit.setText(unit)

    def _build_background_page(self) -> QWidget:
        """个性化：外观（主题/语言/字体）+ 整窗背景图。"""
        page = plain_widget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)

        # 外观（从「常规」迁来）
        ga, gla = self._make_group(tr("界面主题"), icon="palette")
        self._theme_combo = QComboBox()
        self._theme_combo.addItem(t("深色"), "dark")
        self._theme_combo.addItem(t("浅色"), "light")
        self._theme_combo.addItem(t("跟随系统"), "system")
        self._theme_combo.setMinimumWidth(190)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._theme_choices = ThemeChoices(self._theme_combo)
        gla.addWidget(self._theme_choices)
        self._add_caption(gla, tr("跟随系统时，按 Windows 的应用主题自动切换。"))
        language_card, language_rows = self._make_group(tr("语言与字体"), icon="settings")

        self._language_combo = QComboBox()
        self._refresh_languages()
        self._language_combo.setMinimumWidth(190)
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)
        self._add_row(language_rows, t("显示语言"), self._language_combo, t("选择应用内显示的语言，重启软件后生效。"))
        language_actions = plain_widget()
        language_actions.setProperty("settingsFitControlText", True)
        language_action_layout = QFormLayout(language_actions)
        language_action_layout.setContentsMargins(0, 0, 0, 0)
        language_action_layout.setHorizontalSpacing(8)
        language_action_layout.setVerticalSpacing(8)
        language_action_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        language_action_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        open_languages = style_button(QPushButton(t("打开语言包文件夹")), icon="folder")
        open_languages.clicked.connect(self._open_language_folder)
        refresh_languages = style_button(QPushButton(t("刷新列表")), icon="refresh")
        refresh_languages.clicked.connect(self._refresh_languages)
        language_action_layout.addRow(open_languages, refresh_languages)
        self._add_row(
            language_rows,
            t("自定义语言包"),
            language_actions,
            t("每个语言文件夹内编辑 strings.json；添加或修改后重启软件生效。"),
        )

        self._font_combo = QComboBox()
        self._font_combo.setAccessibleName(t("界面字体"))
        self._font_combo.setEditable(True)
        self._font_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._font_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._font_combo.setMinimumContentsLength(20)
        self._font_combo.setMinimumWidth(240)
        self._font_combo.setMaximumWidth(300)
        self._font_combo.setMaxVisibleItems(16)
        self._font_combo.addItem(t("系统默认"), "")
        for family in installed_families():
            self._font_combo.addItem(family, family)
        self._font_combo.lineEdit().setPlaceholderText(t("输入字体名称搜索"))
        completer = self._font_combo.completer()
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        install_font_previews(self._font_combo)
        # 弹层没有设置卡片承托底色，不能继承页面的 transparent 背景。
        # 下拉列表的回退处理放在 combo 上，让亚克力过滤器给 view 设置的内联样式仍可优先。
        for popup_host in (self._font_combo, completer.popup()):
            self._restyle(
                popup_host,
                lambda: (
                    f"QAbstractItemView {{ background-color: {theme.color('bg.card')};"
                    f" color: {theme.color('text.primary')}; }}"
                ),
            )
        self._add_row(
            language_rows,
            t("界面字体"),
            self._font_combo,
            t("选择系统已安装的字体，可输入名称搜索。选择后立即生效，无需重启。"),
        )

        g, gl = self._make_group(tr("背景素材"), icon="image")
        self._build_background_sources(gl)
        v.addWidget(g)
        self._build_dynamic_background_panels(v)

        # 子配置：整张面板随总开关显示/隐藏
        self._bg_sub, sub = self._make_group(tr("显示效果"), icon="palette")

        self._bg_image_row, self._bg_path_edit = self._build_bg_asset(
            "image", tr("图片"), tr("图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*)")
        )
        self._bg_source_rows.addWidget(self._bg_image_row)
        self._build_background_image_status(self._bg_source_rows)

        self._bg_fit_combo = QComboBox()
        self._bg_fit_combo.addItem(tr("填充（裁剪铺满）"), "cover")
        self._bg_fit_combo.addItem(tr("适应（完整显示）"), "contain")
        self._bg_fit_combo.addItem(tr("平铺"), "tile")
        self._bg_fit_combo.setMinimumWidth(190)
        self._bg_fit_combo.currentIndexChanged.connect(lambda _i: self._on_bg_params_changed())
        self._add_row(sub, tr("填充方式"), self._bg_fit_combo, tr("按窗口比例居中裁剪或完整显示。"))

        scrim_row = QHBoxLayout()
        scrim_row.setContentsMargins(0, 0, 0, 0)
        scrim_row.setSpacing(8)
        self._bg_scrim_slider = QSlider(Qt.Orientation.Horizontal)
        self._bg_scrim_slider.setRange(0, 80)
        self._bg_scrim_slider.setFixedWidth(170)
        self._bg_scrim_slider.valueChanged.connect(lambda val: self._bg_scrim_label.setText(f"{val}%"))
        self._bg_scrim_slider.valueChanged.connect(self.bg_scrim_live)  # 拖动实时反馈
        self._bg_scrim_slider.valueChanged.connect(self._on_bg_scrim_changed)
        self._bg_scrim_slider.sliderReleased.connect(lambda: self._save_bg_scrim(self._bg_scrim_slider.value()))
        self._bg_scrim_label = QLabel("45%")
        self._bg_scrim_label.setFixedWidth(40)
        scrim_row.addWidget(self._bg_scrim_slider)
        scrim_row.addWidget(self._bg_scrim_label)
        self._add_row(
            sub,
            tr("背景遮罩"),
            scrim_row,
            tr("深色主题压暗背景，浅色主题柔化提亮；数值越大，背景越淡出。两种主题分别记忆。"),
        )

        blur_row = QHBoxLayout()
        blur_row.setContentsMargins(0, 0, 0, 0)
        blur_row.setSpacing(8)
        self._bg_blur_slider = QSlider(Qt.Orientation.Horizontal)
        self._bg_blur_slider.setRange(0, 100)
        self._bg_blur_slider.setFixedWidth(170)
        self._bg_blur_slider.valueChanged.connect(
            lambda val: self._bg_blur_label.setText(tr("关闭") if val == 0 else f"{val}%")
        )
        self._bg_blur_slider.valueChanged.connect(self.bg_blur_live)  # 拖动实时反馈
        self._bg_blur_slider.sliderReleased.connect(self._on_bg_params_changed)
        self._bg_blur_label = QLabel("60%")
        self._bg_blur_label.setFixedWidth(40)
        blur_row.addWidget(self._bg_blur_slider)
        blur_row.addWidget(self._bg_blur_label)
        self._add_row(sub, tr("背景模糊"), blur_row, tr("柔化背景细节，保持边缘自然；0 为关闭。"))

        v.addWidget(self._bg_sub)
        v.addWidget(self._bg_motion_panel)

        v.addStretch()
        return SettingsCategoryPage(
            tr("个性化"),
            [
                ("look", tr("外观"), tr("选择界面主题、语言与字体。"), [ga, language_card]),
                ("background", tr("整窗背景"), tr("使用图片、视频或 Wallpaper Engine 场景。"), [page]),
            ],
        )

    def _add_concurrency_row(
        self,
        rows: QVBoxLayout,
        title: str,
        desc: str,
        *,
        maximum: int,
        recommended: int,
    ) -> tuple[IntLineEdit, QPushButton]:
        """创建有范围约束的并发输入与自动推荐按钮，用户编辑时撤销自动推荐状态。"""
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        count = IntLineEdit(1, maximum)
        count.setFixedWidth(80)
        count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        count.setAccessibleName(title)
        count.setToolTip(tr("1–{maximum} 个任务；直接编辑数量可手动设置。", maximum=maximum))
        controls.addWidget(count)
        controls.addWidget(text_label(tr("个任务"), "muted"))
        recommend = style_button(QPushButton(tr("自动推荐")))
        recommend.setCheckable(True)
        # 用户重输相同数字也表达了手动选择；程序化加载 setText 不发 textEdited。
        count.textEdited.connect(lambda _text: recommend.setChecked(False))
        recommend.setToolTip(tr("按设备推荐 {recommended} 个任务；选中时使用自动推荐。", recommended=recommended))
        controls.addWidget(recommend)
        self._add_row(rows, title, controls, desc)
        return count, recommend

    def _build_import_page(self) -> QWidget:
        """构建导入导出格式、附带内容和并发设置，根据本机归档工具能力禁用不可用格式。"""
        g, gl = self._make_group(tr("导入行为"), icon="import")

        # 导入本体使用 ZIP、7z 或 RAR；未强制统一格式时保留外部归档原格式。
        self._import_format_combo = QComboBox()
        self._import_format_combo.addItem(tr("ZIP 压缩包 (通用)"), "zip")
        self._import_format_combo.addItem(tr("7z 压缩包 (体积更小，推荐)"), "7z")
        self._import_format_combo.addItem(tr("RAR 压缩包"), "rar")
        self._import_format_combo.setMinimumWidth(200)
        from src.services.archive import find_winrar_rar_exe

        if find_winrar_rar_exe() is None:
            self._set_combo_item_enabled(self._import_format_combo, 2, False, tr("（需安装 WinRAR 后可用）"))
        self._add_row(
            gl,
            tr("Mod 导入格式"),
            self._import_format_combo,
            tr("决定 Mod 在库内以哪种压缩格式保存；未强制统一时，外来压缩包保留原格式。"),
        )

        self._import_strict_format_check = ToggleSwitch()
        self._import_strict_format_row = self._add_row(
            gl,
            tr("强制统一为导入格式"),
            self._import_strict_format_check,
            tr("关闭时保留外来压缩包的原格式，导入更快。"),
            details=tr("开启后异格式压缩包会解压并重新压缩为上面选择的格式，需要额外处理时间。"),
        )

        import_perf, import_perf_rows = self._make_group(tr("处理性能"), icon="memory")
        self._parallel_spin, self._parallel_recommend_btn = self._add_concurrency_row(
            import_perf_rows,
            tr("导入并发"),
            tr("同时处理的压缩任务数量，范围 1–{MAX_ARCHIVE_WORKERS}。", MAX_ARCHIVE_WORKERS=MAX_ARCHIVE_WORKERS),
            maximum=MAX_ARCHIVE_WORKERS,
            recommended=recommended_archive_workers(),
        )
        self._parallel_recommend_btn.clicked.connect(self._set_recommended_workers)

        self._on_import_format_changed()

        g2, gl2 = self._make_group(tr("打包方式"), icon="box")
        # RAR 创建依赖 WinRAR；未安装时禁用该导出选项，与压缩存储的检查一致。
        self._export_format_combo = QComboBox()
        self._export_format_combo.addItem(tr("ZIP 压缩包 (便于分享/再导入)"), "zip")
        self._export_format_combo.addItem(tr("7z 压缩包 (体积更小)"), "7z")
        self._export_format_combo.addItem(tr("RAR 压缩包"), "rar")
        self._export_format_combo.addItem(tr("文件夹 (即开即用)"), "folder")
        self._export_format_combo.setMinimumWidth(200)
        from src.services.archive import find_winrar_rar_exe

        if find_winrar_rar_exe() is None:
            self._set_combo_item_enabled(self._export_format_combo, 2, False, tr("（需安装 WinRAR 后可用）"))
        self._add_row(gl2, tr("Mod 导出格式"), self._export_format_combo, tr("导出单个 Mod 时的打包形式。"))

        # 强制统一格式时转换不同格式的归档，否则直接复制并保留扩展名；文件夹目标不显示此设置。
        self._export_strict_format_check = ToggleSwitch()
        self._export_strict_format_row = self._add_row(
            gl2,
            tr("强制统一为导出格式"),
            self._export_strict_format_check,
            tr("关闭时直接复制原档、保留原生格式。"),
            details=tr("开启后即使源 Mod 是别的压缩格式，也会解压并重新压缩为上面选择的格式。"),
        )

        export_content, export_content_rows = self._make_group(tr("附带内容"), icon="folder")
        self._export_preserve_tree_check = ToggleSwitch()
        self._add_row(
            export_content_rows,
            tr("保留角色分类目录结构"),
            self._export_preserve_tree_check,
            tr("导出时按角色 / 类别目录组织文件。"),
        )
        self._export_include_meta_check = ToggleSwitch()
        self._add_row(
            export_content_rows,
            tr("附带元数据与预览图"),
            self._export_include_meta_check,
            tr("导出 .d3dxmeta，便于分享后一键还原。"),
        )
        export_perf, export_perf_rows = self._make_group(tr("处理性能"), icon="memory")
        self._export_parallel_spin, self._export_parallel_recommend_btn = self._add_concurrency_row(
            export_perf_rows,
            tr("导出并发"),
            tr("同时处理的打包任务数量，范围 1–{MAX_ARCHIVE_WORKERS}。", MAX_ARCHIVE_WORKERS=MAX_ARCHIVE_WORKERS),
            maximum=MAX_ARCHIVE_WORKERS,
            recommended=recommended_archive_workers(),
        )
        self._export_parallel_recommend_btn.clicked.connect(self._set_recommended_export_workers)

        # 初始按当前格式决定「强制统一」行显隐（文件夹格式时隐藏）
        self._on_export_format_changed()

        return SettingsCategoryPage(
            tr("导入与导出"),
            [
                ("import", tr("导入"), tr("设置存储格式和处理方式。"), [g, import_perf]),
                (
                    "export",
                    tr("导出"),
                    tr("设置打包格式和附带内容，方便分享与留档。"),
                    [g2, export_content, export_perf],
                ),
            ],
        )

    def _build_store_page(self) -> QWidget:
        """Mod 商店：启用代理（用常规页配的代理）+ Mod 更新（，与商店强相关）。"""
        g, gl = self._make_group(tr("在线 Mod 连接"), icon="globe")
        self._proxy_enable_check = ToggleSwitch()
        self._proxy_enable_check.setToolTip(tr("使用「常规 → 网络代理」里配置的代理"))
        self._proxy_enable_check.toggled.connect(self._on_proxy_enable_toggled)
        self._add_row(gl, tr("启用代理"), self._proxy_enable_check, tr("浏览、缩略图和下载使用常规页中的代理。"))
        self._proxy_config_button = style_button(QPushButton(tr("前往代理配置")), icon="arrow")
        self._proxy_config_button.clicked.connect(self._show_proxy_settings)
        self._proxy_reference_row = self._add_row(
            gl, tr("当前代理配置"), self._proxy_config_button, tr("代理总开关已关闭。")
        )

        # Mod 更新（，从原「Mod 更新」页迁来）
        g2, gl2 = self._make_group(tr("检查策略"), icon="refresh")
        self._upd_enable_check = ToggleSwitch()
        self._add_row(
            gl2, tr("自动检查 Mod 更新"), self._upd_enable_check, tr("检查从 GameBanana 导入的 Mod 是否有新版本。")
        )
        self._upd_startup_check = ToggleSwitch()
        self._add_row(gl2, tr("启动时检查"), self._upd_startup_check, tr("静默检查，带 24h 节流。"))

        update_perf, update_perf_rows = self._make_group(tr("检查性能"), icon="memory")
        self._upd_concurrency_input, self._upd_concurrency_recommend_btn = self._add_concurrency_row(
            update_perf_rows,
            tr("检查并发"),
            tr(
                "同时向 GameBanana 请求更新的任务数量，范围 1–{MAX_UPDATE_CONCURRENCY}。",
                MAX_UPDATE_CONCURRENCY=MAX_UPDATE_CONCURRENCY,
            ),
            maximum=MAX_UPDATE_CONCURRENCY,
            recommended=recommended_update_concurrency(),
        )
        self._upd_concurrency_recommend_btn.clicked.connect(self._set_recommended_update_concurrency)

        self._upd_periodic_combo = QComboBox()
        self._upd_periodic_combo.addItem(tr("关闭定时检查"), 0)
        self._upd_periodic_combo.addItem(tr("每 6 小时"), 6)
        self._upd_periodic_combo.addItem(tr("每 12 小时"), 12)
        self._upd_periodic_combo.addItem(tr("每天"), 24)
        self._upd_periodic_combo.setMinimumWidth(150)
        self._add_row(gl2, tr("定时检查频率"), self._upd_periodic_combo)

        self._upd_notify_combo = QComboBox()
        self._upd_notify_combo.addItem(tr("仅角标"), "badge")
        self._upd_notify_combo.addItem(tr("角标 + 状态栏提示"), "badge_toast")
        self._upd_notify_combo.setMinimumWidth(190)
        self._add_row(gl2, tr("通知方式"), self._upd_notify_combo)

        # 缓存占用与清理入口位于常规页。

        return SettingsCategoryPage(
            tr("在线 Mod"),
            [
                (
                    "online-proxy",
                    tr("在线 Mod 代理"),
                    tr("为 GameBanana 浏览、缩略图和下载选择连接方式。"),
                    [g, settings_note(tr("GameBanana 在国内部分时段可能无法访问，可在这里启用已配置的代理。"))],
                ),
                (
                    "mod-updates",
                    tr("Mod 更新"),
                    tr("检查已下载 Mod 的新版本，选择检查时机与提醒方式。"),
                    [
                        g2,
                        update_perf,
                        settings_note(
                            tr("检查会带你下载过的 Mod ID 请求 GameBanana 公共 API，与浏览和下载使用同一代理。")
                        ),
                    ],
                ),
            ],
        )

    def _show_proxy_settings(self) -> None:
        """切换到常规设置中的网络代理分区。"""
        self._switch_settings_section(0)
        self._stack.widget(0).select_section("network")

    def _refresh_proxy_summary(self) -> None:
        """按代理总开关和系统或手动模式，更新其他设置分区中的代理摘要。"""
        if not self._proxy_active_check.isChecked():
            summary = tr("代理总开关已关闭。")
        elif self._proxy_system_check.isChecked():
            summary = tr("使用 Windows 系统代理。")
        else:
            summary = (
                f"{self._proxy_type_combo.currentText()} · "
                f"{self._proxy_host_input.text()}:{self._proxy_port_input.text()}"
            )
        self._proxy_reference_row.description_label.setText(summary)

    @staticmethod
    def _set_combo_item_enabled(combo: QComboBox, index: int, enabled: bool, suffix: str = "") -> None:
        """启用/禁用 QComboBox 的某一项（禁用项灰显不可选）。

        Qt 的 QComboBox 无直接 API，经底层 model 的 item flags 实现。禁用时可附加
        说明后缀（如「 — 未检测到 WinRAR」），让用户明白为何不可选。
        """
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QStandardItemModel

        model = combo.model()
        if not isinstance(model, QStandardItemModel):
            return
        item = model.item(index)
        if item is None:
            return
        if enabled:
            item.setFlags(item.flags() | _Qt.ItemFlag.ItemIsEnabled)
        else:
            item.setFlags(item.flags() & ~_Qt.ItemFlag.ItemIsEnabled)
            if suffix and suffix not in item.text():
                item.setText(item.text() + suffix)

    def _on_import_format_changed(self, *_args) -> None:
        """仅在选择有效归档格式时显示强制格式统一选项。"""
        if not hasattr(self, "_import_strict_format_row"):
            return
        fmt = self._import_format_combo.currentData()
        self._import_strict_format_row.setVisible(fmt in ("zip", "7z", "rar"))

    def _on_export_format_changed(self, *_args) -> None:
        """仅在目标为归档格式时显示强制统一格式选项。"""
        if not hasattr(self, "_export_strict_format_row"):
            return
        fmt = self._export_format_combo.currentData()
        self._export_strict_format_row.setVisible(fmt in ("zip", "7z", "rar"))

    def _build_update_page(self) -> QWidget:
        """更新：仅「程序自身更新」；Mod 更新已并入「Mod 商店」页。"""
        # 程序自身更新
        from src.utils.constants import APP_VERSION

        version_card, version_rows = self._make_group(tr("当前版本"), icon="refresh")
        self._app_upd_check_btn = style_button(QPushButton(tr("检查程序更新")), icon="refresh")
        self._app_upd_check_btn.clicked.connect(lambda: self.app_update_check_requested.emit())
        self._add_row(
            version_rows,
            "d3dxSkinManage Plus",
            self._app_upd_check_btn,
            tr("当前版本 v{APP_VERSION}", APP_VERSION=APP_VERSION),
        )
        g2, gl2 = self._make_group(tr("更新偏好"), icon="settings")

        self._app_upd_startup_check = ToggleSwitch()
        self._add_row(
            gl2, tr("启动时检查"), self._app_upd_startup_check, tr("启动后静默检查，带 24h 节流；有新版本才打扰。")
        )
        self._app_upd_backup_check = ToggleSwitch()
        self._add_row(
            gl2,
            tr("更新前备份旧版本"),
            self._app_upd_backup_check,
            tr("一键更新前由更新器备份程序本体，失败可自动回滚。"),
        )

        # 下载源选择。
        from src.utils.constants import APP_UPDATE_DOWNLOAD_MIRRORS

        self._app_upd_source_combo = QComboBox()
        self._app_upd_source_combo.addItem(tr("自动（测速选源，低速切换）"), "auto")
        for key, name, _prefix in APP_UPDATE_DOWNLOAD_MIRRORS:
            self._app_upd_source_combo.addItem(tr(name), key)
        self._app_upd_source_combo.setMinimumWidth(200)
        self._add_row(
            gl2,
            tr("更新包下载源"),
            self._app_upd_source_combo,
            tr("下载更新包时优先使用的源；国内直连 GitHub 慢时可选加速镜像。"),
        )
        return SettingsCategoryPage(
            tr("更新"),
            [
                ("app-update", tr("程序更新"), tr("管理 d3dxSkinManage Plus 自身的版本更新。"), [version_card, g2]),
            ],
        )

    def _on_proxy_active_toggled(self, active: bool):
        """同步代理总开关的显示和生效状态。

        关闭时若商店仍启用代理，先请求确认；确认后同步关闭，取消时恢复总开关。
        """
        if not active and self._proxy_enable_check.isChecked():
            ret = QMessageBox.question(
                self,
                tr("在线 Mod 正在使用代理"),
                tr("「在线 Mod」当前已启用代理。关闭代理总开关会一并停用「在线 Mod」的代理。\n\n确定仍要关闭吗？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                # 取消：弹回开启
                self._proxy_active_check.blockSignals(True)
                self._proxy_active_check.setChecked(True)
                self._proxy_active_check.blockSignals(False)
                return
            # 确认：级联关闭 Mod 商店「启用代理」
            self._proxy_enable_check.blockSignals(True)
            self._proxy_enable_check.setChecked(False)
            self._proxy_enable_check.blockSignals(False)

        self._proxy_cfg_sub.setVisible(active)
        self._save_proxy_settings()
        self.settings_changed.emit()

    def _on_proxy_enable_toggled(self, enabled: bool):
        """Mod 商店「启用代理」开关：是否让 Mod 商店使用常规页所配代理。

        开启时若代理总开关未开 → 提示去常规页打开总开关，并把本开关弹回关闭。
        """
        if enabled and not self._proxy_active_check.isChecked():
            QMessageBox.information(
                self,
                tr("请先打开代理总开关"),
                tr(
                    "代理总开关尚未开启。\n\n请到「常规 → "
                    "网络代理」打开「代理总开关」并配置代理后，再启用「在线 Mod」代理。"
                ),
            )
            self._proxy_enable_check.blockSignals(True)
            self._proxy_enable_check.setChecked(False)
            self._proxy_enable_check.blockSignals(False)
            return
        self._save_proxy_settings()
        self.settings_changed.emit()

    def _on_proxy_system_toggled(self, use_system: bool):
        """系统代理开关（常规页）：仅切换手动配置区可见性。"""
        self._proxy_manual_widget.setVisible(not use_system)

    def _on_test_proxy(self):
        """在后台线程使用界面当前的代理设置测试 GameBanana 连通性。

        设置无需先保存；结果通过 Qt 信号返回，网络等待不阻塞界面事件循环。
        """
        from src.utils.proxy import get_system_proxy

        # 解析当前 UI 上的代理（不依赖已保存的 config）。测试的是「常规」页所配代理本身，
        # 与 Mod 商店「启用代理」无关——故只按系统/手动两种类型解析。
        if self._proxy_system_check.isChecked():
            proxy = get_system_proxy()
            if not proxy:
                self._proxy_test_label.setText(tr("未检测到系统代理"))
                self._proxy_test_label.setStyleSheet(f"color: {theme.color('danger')};")
                return
        else:
            ptype = self._proxy_type_combo.currentData() or "http"
            host = self._proxy_host_input.text().strip()
            port = self._proxy_port_input.text().strip()
            if not host or not port:
                self._proxy_test_label.setText(tr("请填写地址和端口"))
                self._proxy_test_label.setStyleSheet(f"color: {theme.color('danger')};")
                return
            proxy = f"{ptype}://{host}:{port}"

        # 已有代理测试尚未结束时忽略重复请求。
        # Qt 线程对象可能已删除而 Python 引用仍在，访问前需处理失效引用。
        prev_thread = getattr(self, "_proxy_test_thread", None)
        if prev_thread is not None:
            try:
                if prev_thread.isRunning():
                    return
            except RuntimeError:
                # Qt 线程对象已销毁，清除其 Python 引用后允许重新发起测试。
                self._proxy_test_thread = None
                self._proxy_test_worker = None

        self._proxy_test_btn.setEnabled(False)
        self._proxy_test_btn.setText(tr("测试中…"))
        self._proxy_test_label.setText("")

        # 后台线程测量代理延迟，结果通过 worker.finished(ok, msg) 信号
        # 回主线程，由 _on_proxy_test_finished 写 UI。
        thread = QThread(self)
        worker = _ProxyTestWorker(proxy)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_proxy_test_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # 线程结束后清除保存的引用，避免再次访问已由 Qt 删除的对象。
        thread.finished.connect(self._clear_proxy_test_refs)
        self._proxy_test_thread = thread
        self._proxy_test_worker = worker
        thread.start()

    def _clear_proxy_test_refs(self) -> None:
        """thread.finished 后清掉 self 上对 thread/worker 的引用，防止下次访问悬挂对象。"""
        self._proxy_test_thread = None
        self._proxy_test_worker = None

    def _on_proxy_test_finished(self, ok: bool, msg: str) -> None:
        """后台测试完成回主线程——写状态条 + 恢复按钮可点。"""
        color = theme.color("success") if ok else theme.color("danger")
        self._proxy_test_label.setText(msg)
        self._proxy_test_label.setStyleSheet(f"color: {color};")
        self._proxy_test_btn.setEnabled(True)
        self._proxy_test_btn.setText(tr("测试连接"))

    # 缓存占用统计 / 清理

    def _refresh_cache_usage(self) -> None:
        """后台统计缓存总占用，回主线程写进 CacheCapSlider 顶部标注。"""
        if not hasattr(self, "_img_cache_cap"):
            return
        prev = getattr(self, "_cache_size_thread", None)
        if prev is not None:
            try:
                if prev.isRunning():
                    return  # 上次统计还没完，避免叠起
            except RuntimeError:
                self._cache_size_thread = None
                self._cache_size_worker = None
        thread = QThread(self)
        worker = _CacheSizeWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_cache_size_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_cache_size_refs)
        self._cache_size_thread = thread
        self._cache_size_worker = worker
        thread.start()

    def _clear_cache_size_refs(self) -> None:
        """清空缓存统计线程与工作对象引用，允许后续重新调度统计。"""
        self._cache_size_thread = None
        self._cache_size_worker = None

    def _on_cache_size_ready(self, mb: float) -> None:
        """缓存统计完成后，将当前磁盘占用交给仍存在的容量控件显示。"""
        if hasattr(self, "_img_cache_cap"):
            self._img_cache_cap.set_current_usage_mb(mb)

    def _on_clean_all_cache(self) -> None:
        """清理可再生的磁盘缓存，通知宿主清除接口内存缓存，并更新占用显示。

        保留 data/downloads 中的安装包。QSS 图标缓存可能位于系统临时目录，
        不属于应用 cache 目录的清理范围。
        """
        import shutil

        from src.utils.paths import get_cache_dir, get_gb_image_cache_dir

        ret = QMessageBox.question(
            self,
            t("提示"),
            tr(
                "将清空所有可再生缓存（商店缩略图、GameBanana 接口响应等）。\n已下载的 Mod "
                "压缩包属于用户数据，不会被清理。\n\n确定继续？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        cache_root = get_cache_dir()
        try:
            if cache_root.exists():
                shutil.rmtree(cache_root, ignore_errors=True)
            cache_root.mkdir(parents=True, exist_ok=True)
            get_gb_image_cache_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("清理缓存失败: %s", e)
        # 磁盘清完再清内存那一份，否则本次运行内仍会命中旧副本
        self.api_cache_clear_requested.emit()
        self._cache_clean_label.setText(tr("已清理"))
        self._cache_clean_label.setStyleSheet(f"color: {theme.color('success')};")
        if hasattr(self, "_img_cache_cap"):
            self._img_cache_cap.set_current_usage_mb(0)

    # 下载数据统计 / 清理

    def showEvent(self, event):
        """每次进设置页都重算下载占用（可能刚装完 Mod、或刚在别处删过文件）。"""
        super().showEvent(event)
        call_later(0, self, self._refresh_font_layout)
        self.refresh_download_usage()

    def refresh_download_usage(self) -> None:
        """请求宿主重新统计下载占用；结果经 :meth:`set_download_usage` 回填。"""
        if hasattr(self, "_dl_usage_label"):
            self.download_usage_requested.emit()

    def set_download_usage(
        self, total_bytes: int, total_count: int, purgeable_bytes: int, purgeable_count: int
    ) -> None:
        """宿主统计完成后回填（主线程调）。"""
        self._dl_stats = (int(total_bytes), int(total_count), int(purgeable_bytes), int(purgeable_count))
        self._render_download_usage()

    def _render_download_usage(self) -> None:
        """根据下载统计更新文字和容量条，以警告色标出可清理部分，并随主题刷新颜色。"""
        if not hasattr(self, "_dl_usage_label") or not hasattr(self, "_dl_stats"):
            return
        from src.services.gamebanana.format import humanize_size

        total_b, total_n, purge_b, purge_n = getattr(self, "_dl_stats", (0, 0, 0, 0))
        amount, _, unit = humanize_size(total_b).rpartition(" ")
        self._dl_total_label.setText(amount)
        self._dl_total_unit.setText(unit)
        self._dl_installed_label.setText(tr("已安装 {value}", value=humanize_size(purge_b)))
        self._dl_pending_label.setText(tr("未安装 {value}", value=humanize_size(max(0, total_b - purge_b))))
        if total_n <= 0:
            self._dl_usage_label.setText(tr("暂无下载数据"))
        else:
            head = tr("{value} · 共 {total_n} 个安装包", value=humanize_size(total_b), total_n=total_n)
            if purge_n > 0:
                warn = theme.color("settings.teal")
                self._dl_usage_label.setText(
                    tr(
                        "{head}，其中 <span style='color:{warn}'>{purge_n} 个（{value}）已入库可清理</span>",
                        head=head,
                        warn=warn,
                        purge_n=purge_n,
                        value=humanize_size(purge_b),
                    )
                )
            else:
                self._dl_usage_label.setText(tr("{head}，均未入库", head=head))
        ratio = (purge_b / total_b) if total_b > 0 else 0.0
        self._dl_usage_bar.set_ratio(ratio)
        self._dl_usage_bar.setVisible(total_n > 0)
        self._dl_purge_btn.setEnabled(purge_n > 0)

    def set_download_purge_result(self, count: int, freed_bytes: int) -> None:
        """显示清理完成的行内反馈，并重新计算空间占用。"""
        if not hasattr(self, "_dl_purge_result_label"):
            return
        from src.services.gamebanana.format import humanize_size

        self._dl_purge_result_label.show()
        if count > 0:
            self._dl_purge_result_label.setText(tr("已释放 {value}", value=humanize_size(freed_bytes)))
            self._dl_purge_result_label.setStyleSheet(f"color: {theme.color('success')}; background: transparent;")
        else:
            self._dl_purge_result_label.setText(tr("没有可清理的文件"))
            self._dl_purge_result_label.setStyleSheet(
                f"color: {theme.surface_text_color('text.tertiary')}; background: transparent;"
            )
        self.refresh_download_usage()

    def _on_purge_downloads(self) -> None:
        """确认后交给宿主执行删除（宿主还要同步下载器的本地索引）。"""
        total_b, total_n, purge_b, purge_n = getattr(self, "_dl_stats", (0, 0, 0, 0))
        if purge_n <= 0:
            return
        from src.services.gamebanana.format import humanize_size

        keep_n = max(0, total_n - purge_n)
        keep_b = max(0, total_b - purge_b)
        msg = tr(
            (
                "将删除 {purge_n} 个已入库的安装包，释放约 {value}。\n这些 Mod "
                "在库里已有完整副本，删除不影响已安装的 Mod，也不影响更新检测。\n"
            ),
            purge_n=purge_n,
            value=humanize_size(purge_b),
        )
        if keep_n:
            msg += tr("另有 {keep_n} 个（{value}）尚未安装的下载将保留。\n", keep_n=keep_n, value=humanize_size(keep_b))
        ret = QMessageBox.question(
            self,
            t("提示"),
            msg + tr("\n确定继续？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.download_purge_requested.emit()

    def _on_open_download_dir(self) -> None:
        """请求打开下载目录，记录打开失败原因，并重新统计缓存占用。"""
        import os

        from src.utils.paths import get_gb_download_dir

        try:
            os.startfile(str(get_gb_download_dir()))
        except OSError as e:
            logger.warning("打开下载目录失败: %s", e)
        self._refresh_cache_usage()

    def _load_settings(self):
        """阻断控件信号后统一回填配置，再刷新复合控件显示，避免加载过程触发自动保存。"""
        # 加载时阻断自动保存，避免混用尚未更新的控件值；全部回填后由重置入口统一发布。
        with ExitStack() as blockers:
            for widget in self.findChildren(QWidget):
                blockers.enter_context(QSignalBlocker(widget))
            self._populate_settings()
        for widget_type in (SettingsRow, ChoiceSegments, ThemeChoices):
            for widget in self.findChildren(widget_type):
                widget.refresh()

    def _populate_settings(self):
        """将各类配置映射到控件，恢复自动并发策略，并按依赖顺序设置缓存范围和背景参数。"""
        self._image_memory_settings.reload()
        self._load_font_setting()
        # 主题
        theme = self._config.get("general.theme", "dark")
        for i in range(self._theme_combo.count()):
            if self._theme_combo.itemData(i) == theme:
                self._theme_combo.setCurrentIndex(i)
                break

        # 语言
        lang = normalize_language_code(self._config.get("general.language", "zh-CN"))
        for i in range(self._language_combo.count()):
            if self._language_combo.itemData(i) == lang:
                self._language_combo.setCurrentIndex(i)
                break

        delete_method = self._config.get("mods.delete_method", "permanent")
        for i in range(self._delete_method_combo.count()):
            if self._delete_method_combo.itemData(i) == delete_method:
                self._delete_method_combo.setCurrentIndex(i)
                break

        concurrency = self._config.get("mods.deploy_concurrency")
        self._deploy_concurrency_auto = not (type(concurrency) is int and 1 <= concurrency <= MAX_DEPLOY_CONCURRENCY)
        self._deploy_concurrency_input.setValue(effective_deploy_concurrency(concurrency))
        self._deploy_concurrency_recommend_btn.setChecked(self._deploy_concurrency_auto)

        import_format = self._config.get("import.format", "7z")
        for i in range(self._import_format_combo.count()):
            if self._import_format_combo.itemData(i) == import_format:
                self._import_format_combo.setCurrentIndex(i)
                break
        self._import_strict_format_check.setChecked(self._config.get("import.strict_format", False))
        self._on_import_format_changed()
        workers = self._config.get("import.parallel_workers")
        self._import_workers_auto = not (type(workers) is int and 1 <= workers <= MAX_ARCHIVE_WORKERS)
        self._parallel_spin.setValue(effective_archive_workers(workers))
        self._parallel_recommend_btn.setChecked(self._import_workers_auto)

        # 导出设置
        export_format = self._config.get("export.format", "zip")
        for i in range(self._export_format_combo.count()):
            if self._export_format_combo.itemData(i) == export_format:
                self._export_format_combo.setCurrentIndex(i)
                break
        self._export_preserve_tree_check.setChecked(self._config.get("export.preserve_tree", True))
        self._export_include_meta_check.setChecked(self._config.get("export.include_metadata", False))
        self._export_strict_format_check.setChecked(self._config.get("export.strict_format", False))
        # 文件夹导出不使用格式转换设置。
        self._on_export_format_changed()
        workers = self._config.get("export.parallel_workers")
        self._export_workers_auto = not (type(workers) is int and 1 <= workers <= MAX_ARCHIVE_WORKERS)
        self._export_parallel_spin.setValue(effective_archive_workers(workers))
        self._export_parallel_recommend_btn.setChecked(self._export_workers_auto)

        # 代理设置
        self._load_proxy_settings()

        # Mod 更新设置
        uc = "gamebanana.update_check"
        concurrency = self._config.get(f"{uc}.concurrency")
        self._upd_concurrency_auto = not (type(concurrency) is int and 1 <= concurrency <= MAX_UPDATE_CONCURRENCY)
        self._upd_concurrency_input.setValue(effective_update_concurrency(concurrency))
        self._upd_concurrency_recommend_btn.setChecked(self._upd_concurrency_auto)
        self._upd_enable_check.setChecked(self._config.get(f"{uc}.enabled", True))
        self._upd_startup_check.setChecked(self._config.get(f"{uc}.on_startup", True))
        ph = int(self._config.get(f"{uc}.periodic_hours", 0))
        for i in range(self._upd_periodic_combo.count()):
            if self._upd_periodic_combo.itemData(i) == ph:
                self._upd_periodic_combo.setCurrentIndex(i)
                break
        ns = self._config.get(f"{uc}.notify_style", "badge")
        for i in range(self._upd_notify_combo.count()):
            if self._upd_notify_combo.itemData(i) == ns:
                self._upd_notify_combo.setCurrentIndex(i)
                break

        # 先设置容量范围再设置当前值，避免值被旧范围截断；配置加载不触发自动保存。
        _img_range = int(self._config.get("gamebanana.image_cache_range_mb", 4096) or 4096)
        _img_cap = int(self._config.get("gamebanana.image_cache_max_mb", 0) or 0)
        self._img_cache_cap.set_range_max_mb(_img_range)
        self._img_cache_cap.set_value_mb(_img_cap)

        # 下载数据：安装后自动删除源安装包（默认开）
        self._dl_auto_delete_check.setChecked(bool(self._config.get("gamebanana.delete_archive_after_install", True)))

        # 程序更新设置
        self._app_upd_startup_check.setChecked(self._config.get("app_update.on_startup", True))
        self._app_upd_backup_check.setChecked(self._config.get("app_update.backup_before_update", True))
        src = self._config.get("app_update.download_source", "auto")
        for i in range(self._app_upd_source_combo.count()):
            if self._app_upd_source_combo.itemData(i) == src:
                self._app_upd_source_combo.setCurrentIndex(i)
                break

        self._load_background_settings()

    def _load_background_settings(self) -> None:
        """切换主题/恢复默认只读取对应遮罩档位，不触发滑块的实时编辑通道。"""
        bg = self._config.get("appearance.background", {}) or {}
        with ExitStack() as blockers:
            for widget in (self._bg_fit_combo, self._bg_scrim_slider, self._bg_blur_slider):
                blockers.enter_context(QSignalBlocker(widget))
            with QSignalBlocker(self._bg_path_edit):
                self._bg_path_edit.setText(bg.get("path", "") or "")
            resolved = theme.resolve_auto(self._config.get("general.theme", "dark"))
            scrim = int(bg.get("scrim_dark", 45) if resolved == "dark" else bg.get("scrim_light", 30))
            self._bg_scrim_slider.setValue(scrim)
            self._bg_scrim_label.setText(f"{scrim}%")
            blur = int(bg.get("blur", 60))
            self._bg_blur_slider.setValue(blur)
            self._bg_blur_label.setText(tr("关闭") if blur == 0 else f"{blur}%")
            self._bg_fit_combo.setCurrentIndex(self._bg_fit_combo.findData(bg.get("fit", "cover")))
            self._bg_sub.setVisible(bool(bg.get("enabled")))
        self._load_dynamic_background_settings(bg)

    # 实时保存接线
    def _on_deploy_concurrency_changed(self, value: int) -> None:
        """将部署并发切换为手动值，取消推荐选中状态并立即保存。"""
        self._deploy_concurrency_auto = False
        self._deploy_concurrency_recommend_btn.setChecked(False)
        self._autosave("mods.deploy_concurrency", value)

    def _set_recommended_deploy_concurrency(self) -> None:
        """显示本机推荐部署并发，以零值保存自动策略并通知宿主更新。"""
        self._deploy_concurrency_auto = True
        self._deploy_concurrency_input.setValue(recommended_deploy_concurrency())
        self._deploy_concurrency_recommend_btn.setChecked(True)
        self._autosave("mods.deploy_concurrency", 0)

    def _autosave(self, key: str, value) -> None:
        """写单个配置项并通知主窗热生效。"""
        self._config.set(key, value)
        self.settings_changed.emit()

    def _autosave_proxy(self, *_args) -> None:
        """代理各字段互相关——任一变更即整组保存 + 通知热生效。"""
        self._save_proxy_settings()
        self.settings_changed.emit()

    def _wire_autosave(self) -> None:
        """连接各项设置的即时保存与变化通知，按控件语义区分实时修改和编辑完成。"""
        self._font_combo.currentIndexChanged.connect(self._on_font_changed)
        # 字体已卸载时 UI 回落到首项；再次选择首项也应能明确保存「系统默认」。
        self._font_combo.activated.connect(self._on_font_changed)
        self._font_combo.lineEdit().editingFinished.connect(self._on_font_edit_finished)
        uc = "gamebanana.update_check"
        self._delete_method_combo.currentIndexChanged.connect(
            lambda _i: self._autosave("mods.delete_method", self._delete_method_combo.currentData())
        )
        self._deploy_concurrency_input.valueChanged.connect(self._on_deploy_concurrency_changed)
        self._deploy_concurrency_input.editingFinished.connect(
            lambda: (
                self._on_deploy_concurrency_changed(self._deploy_concurrency_input.value())
                if self._deploy_concurrency_auto and not self._deploy_concurrency_recommend_btn.isChecked()
                else None
            )
        )
        self._dl_auto_delete_check.toggled.connect(
            lambda v: self._autosave("gamebanana.delete_archive_after_install", v)
        )
        self._import_format_combo.currentIndexChanged.connect(
            lambda _i: (
                self._autosave("import.format", self._import_format_combo.currentData()),
                self._on_import_format_changed(),
            )
        )
        self._import_strict_format_check.toggled.connect(lambda v: self._autosave("import.strict_format", v))
        self._parallel_spin.valueChanged.connect(self._on_import_workers_changed)
        self._export_parallel_spin.valueChanged.connect(self._on_export_workers_changed)
        self._parallel_spin.editingFinished.connect(
            lambda: (
                self._on_import_workers_changed(self._parallel_spin.value())
                if self._import_workers_auto and not self._parallel_recommend_btn.isChecked()
                else None
            )
        )
        self._export_parallel_spin.editingFinished.connect(
            lambda: (
                self._on_export_workers_changed(self._export_parallel_spin.value())
                if self._export_workers_auto and not self._export_parallel_recommend_btn.isChecked()
                else None
            )
        )
        self._export_format_combo.currentIndexChanged.connect(
            lambda _i: (
                self._autosave("export.format", self._export_format_combo.currentData()),
                self._on_export_format_changed(),
            )
        )
        self._export_preserve_tree_check.toggled.connect(lambda v: self._autosave("export.preserve_tree", v))
        self._export_include_meta_check.toggled.connect(lambda v: self._autosave("export.include_metadata", v))
        self._export_strict_format_check.toggled.connect(lambda v: self._autosave("export.strict_format", v))
        self._upd_enable_check.toggled.connect(lambda v: self._autosave(f"{uc}.enabled", v))
        self._upd_concurrency_input.valueChanged.connect(self._on_update_concurrency_changed)
        self._upd_concurrency_input.editingFinished.connect(
            lambda: (
                self._on_update_concurrency_changed(self._upd_concurrency_input.value())
                if self._upd_concurrency_auto and not self._upd_concurrency_recommend_btn.isChecked()
                else None
            )
        )
        self._upd_startup_check.toggled.connect(lambda v: self._autosave(f"{uc}.on_startup", v))
        self._upd_periodic_combo.currentIndexChanged.connect(
            lambda _i: self._autosave(f"{uc}.periodic_hours", self._upd_periodic_combo.currentData())
        )
        self._upd_notify_combo.currentIndexChanged.connect(
            lambda _i: self._autosave(f"{uc}.notify_style", self._upd_notify_combo.currentData())
        )
        # 程序更新偏好
        self._app_upd_startup_check.toggled.connect(lambda v: self._autosave("app_update.on_startup", v))
        self._app_upd_backup_check.toggled.connect(lambda v: self._autosave("app_update.backup_before_update", v))
        self._app_upd_source_combo.currentIndexChanged.connect(
            lambda _i: self._autosave("app_update.download_source", self._app_upd_source_combo.currentData())
        )
        # 代理：地址/端口用 editingFinished（避免逐字保存+热重建代理抖动）；
        # 「启用代理」开关有「未配置则回弹」逻辑，单独走 _on_proxy_enable_toggled（已在 build 接），
        # 不再接 _autosave_proxy，否则回弹时会把 enabled=True 误存。
        self._proxy_system_check.toggled.connect(self._autosave_proxy)
        self._proxy_type_combo.currentIndexChanged.connect(self._autosave_proxy)
        self._proxy_host_input.editingFinished.connect(self._autosave_proxy)
        self._proxy_port_input.editingFinished.connect(self._autosave_proxy)

    @staticmethod
    def _get_recommended_workers() -> int:
        """返回按当前设备能力计算的归档处理推荐并发数。"""
        return recommended_archive_workers()

    def _on_import_workers_changed(self, value: int) -> None:
        """将导入并发标记为手动选择并保存，不改动导出并发策略。"""
        self._import_workers_auto = False
        self._parallel_recommend_btn.setChecked(False)
        self._autosave("import.parallel_workers", value)

    def _on_export_workers_changed(self, value: int) -> None:
        """将导出并发标记为手动选择并保存，不改动导入并发策略。"""
        self._export_workers_auto = False
        self._export_parallel_recommend_btn.setChecked(False)
        self._autosave("export.parallel_workers", value)

    def _set_recommended_workers(self):
        """保存自动模式，设备推荐值只用于显示。"""
        self._import_workers_auto = True
        self._parallel_spin.setValue(self._get_recommended_workers())
        self._parallel_recommend_btn.setChecked(True)
        self._autosave("import.parallel_workers", 0)

    def _set_recommended_export_workers(self) -> None:
        """恢复导出并发的自动策略，显示当前建议值并以零值保存配置。"""
        self._export_workers_auto = True
        self._export_parallel_spin.setValue(recommended_archive_workers())
        self._export_parallel_recommend_btn.setChecked(True)
        self._autosave("export.parallel_workers", 0)

    def _on_update_concurrency_changed(self, value: int) -> None:
        """保存手动 Mod 更新并发值，并取消自动推荐选中状态。"""
        self._upd_concurrency_auto = False
        self._upd_concurrency_recommend_btn.setChecked(False)
        self._autosave("gamebanana.update_check.concurrency", value)

    def _set_recommended_update_concurrency(self) -> None:
        """恢复 Mod 更新并发的自动策略，刷新建议值并显式通知任务调度器。"""
        self._upd_concurrency_auto = True
        self._upd_concurrency_recommend_btn.setChecked(True)
        self._upd_concurrency_input.setValue(recommended_update_concurrency())
        # 保留自动模式；setValue 不发 valueChanged，需显式保存并通知调度器。
        self._autosave("gamebanana.update_check.concurrency", 0)

    def _reset_settings(self):
        """恢复程序设置默认值，游戏配置由独立存储管理。

        写入成功后才刷新界面并发布设置变化；失败时保留当前状态。
        """
        answer = QMessageBox.question(
            self,
            t("恢复默认"),
            t(
                "将恢复所有程序设置为默认值，包括外观、快捷键及导入、导出偏好。\n"
                "支持自动推荐的设置将恢复为自动模式，按当前设备选择合适的值。\n"
                "游戏配置和已导入的 Mod 不受影响。\n\n确定恢复默认设置吗？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        old_theme = self._config.get("general.theme")
        old_language = self._config.get("general.language")
        if not self._config.reset():
            QMessageBox.warning(
                self,
                tr("恢复默认失败"),
                tr("设置文件写入失败，已保持原有设置。\n\n请检查 config 目录是否只读或被其它程序占用，然后重试。"),
            )
            return
        self._load_settings()
        self._shortcut_settings.reload()
        if old_theme != self._config.get("general.theme"):
            self.theme_changed.emit(self._config.get("general.theme"))
        self.font_changed.emit(self._config.get("appearance.font_family", ""))
        # 所有控件已同步默认策略后，再通知主窗全量热生效。
        self.background_changed.emit()
        self.settings_changed.emit()
        if old_language != self._config.get("general.language"):
            self.language_changed.emit(self._config.get("general.language"))

    def _on_bg_scrim_changed(self, value: int) -> None:
        """非拖动输入立即保存遮罩值，滑块拖动则交由释放事件统一保存。"""
        # 拖动在松开时保存；方向键、滚轮和轨道点击不会触发 sliderReleased。
        if not self._bg_scrim_slider.isSliderDown():
            self._save_bg_scrim(value)

    def _save_bg_scrim(self, value: int) -> None:
        """按当前解析后的明暗主题分别保存背景遮罩强度。"""
        # 遮罩强度按当前主题分别记一档。
        th = theme.resolve_auto(self._config.get("general.theme", "dark"))
        key = "scrim_dark" if th == "dark" else "scrim_light"
        self._config.set(f"appearance.background.{key}", value)

    def _on_bg_params_changed(self):
        """保存主题遮罩及全局模糊、适配方式，并请求刷新背景层。"""
        self._save_bg_scrim(self._bg_scrim_slider.value())
        # 模糊 / 填充全局。
        self._config.set("appearance.background.blur", self._bg_blur_slider.value())
        self._config.set("appearance.background.fit", self._bg_fit_combo.currentData())
        # 拖动已实时应用暗度和模糊，释放时仅保存配置并更新适配方式，不重复刷新全窗主题。
        self.bg_overlay_refresh.emit()

    def _load_font_setting(self) -> None:
        """回填已保存字体，找不到时显示列表首项，并阻断回填期间的控件信号。"""
        index = self._font_combo.findData(self._config.get("appearance.font_family", ""))
        with QSignalBlocker(self._font_combo):
            self._font_combo.setCurrentIndex(max(0, index))
            self._font_combo.setEditText(self._font_combo.itemText(self._font_combo.currentIndex()))

    def _on_font_edit_finished(self) -> None:
        """延后提交字体输入，等待 Qt 完成本轮补全选择后再规范化文本。"""
        # 回车接受补全时，editingFinished 早于 completer.activated；同步复原搜索词
        # 会把尚待接受的候选项也换回旧值。等 Qt 完成本次选取后再归一化文本。
        call_later(0, self, self._commit_font_edit)

    def _commit_font_edit(self) -> None:
        """只接受列表中完整匹配的字体名称，无效搜索恢复已保存选择。"""
        # 搜索框只接受列表中的完整字体名；未完成/无匹配的搜索不改变已保存的选择。
        index = self._font_combo.findText(self._font_combo.currentText(), Qt.MatchFlag.MatchFixedString)
        if index < 0:
            self._load_font_setting()
            return
        with QSignalBlocker(self._font_combo):
            self._font_combo.setCurrentIndex(index)
            self._font_combo.setEditText(self._font_combo.itemText(index))
        self._on_font_changed(index)

    def _on_font_changed(self, index: int) -> None:
        """原子保存新字体后通知宿主应用，失败时恢复原选择并显示错误。"""
        family = self._font_combo.itemData(index)
        if family is None or family == self._config.get("appearance.font_family", ""):
            return
        if not self._config.set_atomic("appearance.font_family", family):
            self._load_font_setting()
            QMessageBox.warning(self, t("错误"), t("字体设置保存失败，已保持原来的字体。请检查配置目录是否可写。"))
            return
        self.font_changed.emit(family)

    def _on_theme_changed(self, index: int):
        """保存有效主题选择，重新加载对应背景参数，并通知宿主切换外观。"""
        theme = self._theme_combo.currentData()
        if theme:
            # 立即生效
            self._config.set("general.theme", theme)
            self._load_background_settings()
            self.theme_changed.emit(theme)

    def _on_language_changed(self, index: int):
        """原子保存不同于当前配置的语言选择，失败时恢复列表，成功后通知宿主。"""
        lang = self._language_combo.currentData()
        if not lang or lang == self._config.get("general.language"):
            return
        if not self._config.set_atomic("general.language", lang):
            self._refresh_languages()
            QMessageBox.warning(self, t("错误"), t("语言设置保存失败，请检查设置文件是否只读后重试。"))
            return
        self.language_changed.emit(lang)

    def _refresh_languages(self) -> None:
        """重建语言列表，保留已配置但文件缺失的语言占位项，并阻断选择变化信号。"""
        selected = normalize_language_code(self._config.get("general.language", "zh-CN"))
        with QSignalBlocker(self._language_combo):
            self._language_combo.clear()
            for code, name in get_i18n().available_languages:
                self._language_combo.addItem(name, code)
            index = self._language_combo.findData(selected)
            if index < 0 and selected:
                self._language_combo.addItem(t("{code}（未找到语言包）", code=selected), selected)
                index = self._language_combo.count() - 1
            self._language_combo.setCurrentIndex(max(0, index))

    def _open_language_folder(self) -> None:
        """确保语言目录存在后交给系统打开，目录创建失败时显示错误。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        directory = get_i18n().directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        except OSError as error:
            QMessageBox.warning(self, t("错误"), t("无法打开语言包文件夹：{error}", error=error))

    # 代理设置

    def _load_proxy_settings(self):
        """将已校验的代理配置加载到界面，缺省值由 Config 提供。"""
        active = bool(self._config.get("gamebanana.proxy_config.active"))
        enabled = bool(self._config.get("gamebanana.proxy_config.enabled"))
        mode = self._config.get("gamebanana.proxy_config.mode")
        parsed_type = self._config.get("gamebanana.proxy_config.type")
        parsed_host = self._config.get("gamebanana.proxy_config.host")
        parsed_port = str(self._config.get("gamebanana.proxy_config.port"))

        # 回填配置期间暂停信号，避免触发级联确认或自动保存。
        self._proxy_active_check.blockSignals(True)
        self._proxy_active_check.setChecked(active)
        self._proxy_active_check.blockSignals(False)
        self._proxy_enable_check.blockSignals(True)
        self._proxy_enable_check.setChecked(enabled)
        self._proxy_enable_check.blockSignals(False)
        self._proxy_system_check.setChecked(mode == "system")

        for i in range(self._proxy_type_combo.count()):
            if self._proxy_type_combo.itemData(i) == parsed_type:
                self._proxy_type_combo.setCurrentIndex(i)
                break
        self._proxy_host_input.setText(parsed_host)
        self._proxy_port_input.setText(parsed_port)

        # 同步显隐：总开关控制整块、系统代理控制手动区
        self._proxy_cfg_sub.setVisible(active)
        self._on_proxy_system_toggled(self._proxy_system_check.isChecked())
        self._refresh_proxy_summary()

    def _save_proxy_settings(self):
        """整组保存代理总开关、在线开关、运行模式与连接参数，并刷新代理摘要。"""
        active = self._proxy_active_check.isChecked()
        enabled = self._proxy_enable_check.isChecked()
        mode = "system" if self._proxy_system_check.isChecked() else "manual"
        ptype = self._proxy_type_combo.currentData() or "http"
        host = self._proxy_host_input.text().strip()
        port = self._proxy_port_input.text().strip()

        self._config.set("gamebanana.proxy_config.active", bool(active))
        self._config.set("gamebanana.proxy_config.enabled", bool(enabled))
        self._config.set("gamebanana.proxy_config.mode", mode)
        self._config.set("gamebanana.proxy_config.type", ptype)
        self._config.set("gamebanana.proxy_config.host", host)
        self._config.set("gamebanana.proxy_config.port", port)
        self._refresh_proxy_summary()
