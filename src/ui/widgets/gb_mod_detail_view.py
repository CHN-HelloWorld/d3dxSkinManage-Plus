"""GameBanana Mod 详情视图。

概览、描述和更新分别显示在标签页中；预览与文件列表使用独立滚动区域，
通过分隔条调整高度，避免异步图片加载改变下载按钮的位置。
选择新的 Mod 时先同步清空旧详情，再接收对应请求结果。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QMetaMethod, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.format import humanize_size
from src.services.gamebanana.models import (
    GbModFile,
    GbModProfile,
    GbModRecord,
    GbModUpdateEntry,
)
from src.ui import anim, icons, theme
from src.ui.widgets.spinner import ShimmerPlaceholder
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


def _format_count(n: int) -> str:
    """把下载量等大数压缩成 1.2k / 3.4M 这种短表示，避免文件名旁边数字过长。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def _html_escape(s: str) -> str:
    """将更新标题、版本和文件名等纯文本转义后写入 HTML。"""
    import html as _h

    return _h.escape(s or "", quote=True)


class _AspectRatioImage(QLabel):
    """按布局宽度等比显示预览图。

    使用 heightForWidth 向父布局提供目标高度，由布局统一分配尺寸。
    resizeEvent 仅缩放像素，不设置固定高度，避免子控件反复触发上层布局。
    尺寸稳定 80 毫秒后使用平滑变换提高显示质量。
    """

    _SMOOTH_DELAY_MS = 80
    _DEFAULT_ASPECT_H_OVER_W = 0.5625  # 图片到达前按 16:9 估算初始尺寸。

    def __init__(self, parent: QWidget | None = None) -> None:
        """建立由图片宽高比决定布局高度的预览控件，初始化加载占位及平滑缩放定时器。"""
        super().__init__(parent)
        self._orig: QPixmap | None = None
        self._aspect_h_over_w: float = self._DEFAULT_ASPECT_H_OVER_W
        self.setScaledContents(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 横向使用可用宽度，纵向由 heightForWidth 提供对应高度。
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(120, 80)
        # 图像到达前显示微光占位，完成后交叉淡出至实际图像。
        self._shimmer = ShimmerPlaceholder(self, radius=6)
        self._shimmer.hide()
        # 尺寸稳定八十毫秒后进行平滑重绘。
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setSingleShot(True)
        self._smooth_timer.setInterval(self._SMOOTH_DELAY_MS)
        self._smooth_timer.timeout.connect(self._rescale_smooth)

    def start_loading(self) -> None:
        """图像请求开始前显示加载占位，图像到达后自动收起。"""
        self._shimmer.setGeometry(self.rect())
        self._shimmer.show()
        self._shimmer.raise_()

    # 向布局提供宽度对应的目标高度。
    def hasHeightForWidth(self) -> bool:
        """告知布局系统图片高度随分配宽度变化。"""
        return True

    def heightForWidth(self, w: int) -> int:
        """按当前图片宽高比计算内容高度，并保留最低可读高度。"""
        return max(80, int((w - 4) * self._aspect_h_over_w))

    def sizeHint(self) -> QSize:
        """以初始建议宽度和当前图片比例提供布局尺寸。"""
        # 提供首次布局的建议尺寸。
        return QSize(200, self.heightForWidth(200))

    def minimumSizeHint(self) -> QSize:
        """提供加载前后共用的最小建议预览尺寸。"""
        return QSize(120, 80)

    # pixmap 入口
    def setPixmap(self, pm: QPixmap) -> None:
        """保存原始图像并更新宽高比，通知布局后平滑显示图片，有效图片到达时结束加载占位。"""
        self._orig = pm
        if pm and not pm.isNull() and pm.width() > 0:
            self._aspect_h_over_w = pm.height() / pm.width()
        # 图片宽高比变化后通知布局重新计算高度。
        self.updateGeometry()
        # 立即以平滑缩放显示图片。
        self._rescale(smooth=True)
        # 图片准备完成后结束加载占位，不依赖当前可见性。
        if pm is not None and not pm.isNull():
            self._shimmer.reveal()

    # 尺寸变化只更新像素，避免设置固定高度再次触发布局。
    def resizeEvent(self, event) -> None:
        """尺寸变化时快速更新图片和占位区域，并延后进行平滑缩放。"""
        super().resizeEvent(event)
        # 拖动期间使用快速缩放匹配当前尺寸。
        self._rescale(smooth=False)
        # 尺寸稳定后使用平滑缩放。
        self._smooth_timer.start()
        # 骨架微光占位跟随当前尺寸铺满
        if self._shimmer.isVisible():
            self._shimmer.setGeometry(self.rect())

    def _rescale_smooth(self) -> None:
        """在尺寸稳定后按当前区域对原图执行高质量缩放。"""
        self._rescale(smooth=True)

    def _rescale(self, *, smooth: bool = True) -> None:
        """以指定变换质量等比缩放原图到可用区域，图片无效时保留当前显示。"""
        if self._orig is None or self._orig.isNull():
            return
        target_w = max(self.width() - 4, 1)
        target_h = max(self.height() - 4, 1)
        mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
        # 布局按图片宽高比分配高度；过渡期间即使尺寸尚未匹配，
        # KeepAspectRatio 仍能保持画面比例并居中显示。
        scaled = self._orig.scaled(
            QSize(target_w, target_h),
            Qt.AspectRatioMode.KeepAspectRatio,
            mode,
        )
        super().setPixmap(scaled)


class GbModDetailView(QFrame):
    """展示在线资源的预览、文件、描述和更新记录，通过信号交付下载与安装操作。"""

    back_requested = Signal()
    file_download_requested = Signal(GbModFile)
    file_install_requested = Signal(GbModFile)
    preview_clicked = Signal(str)

    PREVIEW_FETCH_MAX = 1600  # 远程图片抓取上限边长（缩放上限，不是显示宽度）

    def __init__(
        self,
        *,
        image_loader,
        has_local_md5: Callable[[str], bool] | None = None,
        is_file_installed: Callable[[int, int, str], bool] | None = None,
        updates_fetcher: Callable[[GbModProfile], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """绑定图片及安装状态查询接口，建立详情标签页、文件状态缓存和连接提示覆盖层。"""
        super().__init__(parent)
        self.setObjectName("gbDetailPanel")  # 详情浮层位于页面内部，由控件自行绘制模糊背景。
        # 纯色主题使用浏览网格的模糊快照及半透明叠色；
        # 快照由商店页面获取，并随滚动及位置变化更新。
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 圆角之外保持透明，背景和边框统一由 paintEvent 绘制。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._backdrop: QPixmap | None = None
        self._image_loader = image_loader
        self._has_local_md5 = has_local_md5 or (lambda md5: False)
        # 按 Mod 编号及文件编号或摘要判断具体文件是否已安装，不影响同一 Mod 的其他文件。
        self._is_file_installed = is_file_installed or (lambda mod_id, file_id, md5: False)
        # 首次打开空更新页时由上层异步请求更新，完成后刷新当前内容。
        # 没有请求回调时只渲染已有记录。
        self._updates_fetcher = updates_fetcher
        self._updates_fetch_inflight = False  # 防止 Tab 反复切换重复拉取
        self._profile: GbModProfile | None = None
        # 记录当前期望的 Mod 标识；异步详情返回时需匹配此标识，
        # 防止过期响应覆盖用户已经切换到的内容。
        self._pending_mod_id: int = 0
        self._desc_loaded = False
        self._updates_loaded = False
        self._file_rows: list[QFrame] = []
        # 按文件编号保存按钮引用、下载状态及进度。
        # 详情内容切换时保留状态，重新打开后继续显示实际下载进度。
        self._file_buttons: dict[int, QPushButton] = {}
        self._file_states: dict[int, str] = {}  # "idle"|"downloading"|"done"|"failed"
        self._file_progress: dict[int, int] = {}  # 0-100
        # 只设置最小可读宽度，最大宽度由页面可用空间决定，保证浮层始终靠右对齐。
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 返回按钮独占首行；标题和作者位于第二行，使长标题可换行且不挤压其他控件。
        top_box = QVBoxLayout()
        top_box.setContentsMargins(8, 6, 8, 6)
        top_box.setSpacing(4)

        # 第一行：返回按钮（左对齐）
        back_row = QHBoxLayout()
        back_row.setContentsMargins(0, 0, 0, 0)
        self.back_btn = QToolButton()
        self.back_btn.setText(tr("返回"))
        icons.apply_icon(self.back_btn, "actions/back")
        self.back_btn.setIconSize(QSize(16, 16))
        self.back_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.back_btn.setAutoRaise(True)
        self.back_btn.clicked.connect(self.back_requested.emit)
        back_row.addWidget(self.back_btn)
        back_row.addStretch(1)
        top_box.addLayout(back_row)

        # 标题和作者区域，长标题自动换行。
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:bold; font-size:14pt;")
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 最小宽度为零，使窄面板中的标题能够按分配宽度换行，避免额外水平滚动。
        self.title_label.setMinimumWidth(0)
        title_row.addWidget(self.title_label, 1)
        self.author_label = QLabel("")
        self.author_label.setStyleSheet(f"color:{theme.color('text.tertiary')};")
        # 作者与标题首行顶端对齐，不随标题换行改变位置。
        self.author_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.author_label, 0, Qt.AlignmentFlag.AlignTop)
        top_box.addLayout(title_row)
        layout.addLayout(top_box)

        # Tab 区
        self.tabs = QTabWidget()
        self.tabs.setObjectName("gbDetailTabs")
        self._overview_widget = self._build_overview()
        self._description_widget = self._build_description()
        self._updates_widget = self._build_updates()
        self.tabs.addTab(self._overview_widget, tr("概览"))
        self.tabs.addTab(self._description_widget, tr("描述"))
        self.tabs.addTab(self._updates_widget, tr("更新"))
        self._updates_tab_index = self.tabs.indexOf(self._updates_widget)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)

        # 使用可移动下划线显示当前标签；样式表不再重复绘制静态底边。
        self._tab_indicator = QWidget(self.tabs.tabBar())
        self._tab_indicator.setObjectName("gbTabIndicator")
        self._tab_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._tab_indicator.setStyleSheet(
            f"#gbTabIndicator {{ background-color: {theme.color('accent')}; border-radius: 1px; }}"
        )
        self._tab_indicator.hide()
        # TabBar 自身的几何变化（首次布局 / 宽度变化 / 可见性切换）都要重新定位指示条。
        self.tabs.tabBar().installEventFilter(self)
        self.tabs.currentChanged.connect(lambda _i: self._update_tab_indicator(animate=True))

        # 请求详情期间显示加载覆盖层，成功后隐藏，失败时显示重试提示。
        from src.ui.widgets.spinner import LoadingIndicator

        self._loading_overlay = LoadingIndicator(tr("正在连接 GameBanana…"), self)
        self._loading_overlay.hide()

        # 内容容器保持透明，以显示面板绘制的模糊背景。
        # 滚动视口需在自身样式中设置透明，标签容器使用局部标识规则，避免影响其他子控件。
        self.setStyleSheet(
            "#gbDetailTabs, #gbDetailTabs QTabBar,"
            " #gbDetailTabs QStackedWidget { background: transparent; }"
            "#gbDetailTabs::pane { background: transparent; border: none; }"
            # 标签和工具按钮保持透明，避免全局 QWidget 背景遮挡面板。
            # QPushButton 和 QTextBrowser 保留各自底色。
            "#gbDetailPanel QLabel,"
            " #gbDetailPanel QToolButton { background: transparent; }"
        )

    def resizeEvent(self, event):
        """详情面板尺寸变化后重新定位加载覆盖层。"""
        super().resizeEvent(event)
        self._reposition_loading_overlay()

    def showEvent(self, event):
        """首次布局完成后同步可见加载提示及标签下划线的位置。"""
        super().showEvent(event)
        # 首次显示完成布局后，重新定位可见的加载覆盖层。
        if self._loading_overlay.isVisible():
            self._reposition_loading_overlay()
        # 首次布局完成后直接定位标签下划线。
        self._update_tab_indicator(animate=False)

    def _reposition_loading_overlay(self) -> None:
        """将加载覆盖层限制在标签内容区域内，保留标签栏的交互能力。"""
        # 尺寸尚未就绪时延迟定位，等待布局完成后再显示覆盖层。
        if self.width() <= 0 or self.height() <= 0:
            return
        try:
            content = self.tabs.currentWidget()
            if content is not None and content.width() > 0:
                tl = content.mapTo(self, content.rect().topLeft())
                self._loading_overlay.setGeometry(
                    tl.x(),
                    tl.y(),
                    content.width(),
                    content.height(),
                )
                return
            # 无法获取内容区域时使用整个面板作为覆盖范围。
            top = self.tabs.geometry().top()
            self._loading_overlay.setGeometry(
                0,
                top,
                self.width(),
                max(0, self.height() - top),
            )
        except (AttributeError, RuntimeError):
            pass

    def _show_connecting(self) -> None:
        """设置连接提示文案并进入覆盖层定位与显示流程。"""
        self._loading_overlay.set_text(tr("正在连接 GameBanana…"))
        self._position_and_show_loading()

    def _position_and_show_loading(self) -> None:
        """等待面板获得可见尺寸后显示并启动加载指示器，布局未就绪时延后重试。"""
        # 首次显示前尺寸可能尚未确定，将覆盖层定位延迟至本轮布局完成。
        if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            call_later(0, self, self._position_and_show_loading)
            return
        self._reposition_loading_overlay()
        self._loading_overlay.show()
        self._loading_overlay.raise_()
        # 重新加载时显式启动指示器，避免复用已显示的失败覆盖层而不触发 showEvent。
        try:
            self._loading_overlay.spinner.start()
        except (AttributeError, RuntimeError):
            pass

    def _hide_connecting(self) -> None:
        """隐藏详情连接或加载状态覆盖层。"""
        self._loading_overlay.hide()

    def show_load_failed(self) -> None:
        """将详情加载状态替换为失败提示。"""
        self._loading_overlay.set_text(tr("连接失败，请检查网络后重新点击该 Mod"))
        self._reposition_loading_overlay()
        self._loading_overlay.show()
        self._loading_overlay.raise_()
        try:
            self._loading_overlay.spinner.stop()
        except (AttributeError, RuntimeError):
            pass

    def _build_overview(self) -> QWidget:
        """为预览和文件分别建立滚动区，恢复分隔比例并提供站点页面入口。"""
        # 预览和文件列表使用独立滚动区，由分隔条调整高度，
        # 防止异步图片加载改变文件列表和下载按钮的位置。
        self.overview_splitter = QSplitter(Qt.Orientation.Vertical)
        self.overview_splitter.setObjectName("gbOvSplitter")
        self.overview_splitter.setStyleSheet("#gbOvSplitter { background: transparent; }")
        self.overview_splitter.setChildrenCollapsible(False)
        self.overview_splitter.setHandleWidth(6)

        # 上：图片预览滚动区
        self.preview_scroll_area = QScrollArea()
        self.preview_scroll_area.setWidgetResizable(True)
        self.preview_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 始终保留垂直滚动条，避免其显隐改变视口宽度，
        # 继而反复触发图片高度计算与滚动条状态变化。
        self.preview_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        # 滚动区域透明显示外层面板背景。
        self.preview_scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.previews_container = QWidget()
        self.previews_container.setObjectName("gbOvPreviews")
        # 在滚动区自身设置带控件标识的透明样式，以覆盖全局底色规则。
        self.previews_container.setStyleSheet("#gbOvPreviews { background: transparent; }")
        self.previews_layout = QVBoxLayout(self.previews_container)
        self.previews_layout.setContentsMargins(8, 8, 8, 8)
        self.previews_layout.setSpacing(8)
        # 内容加载时添加末尾伸缩项，清空预览时一并移除。
        self.preview_scroll_area.setWidget(self.previews_container)
        # 视口透明规则使用控件标识提高优先级，避免被全局底色覆盖。
        _pv = self.preview_scroll_area.viewport()
        _pv.setObjectName("gbPrevVp")
        _pv.setAutoFillBackground(False)
        _pv.setStyleSheet("#gbPrevVp { background: transparent; }")
        self.preview_scroll_area.setMinimumHeight(120)
        # 滚动位置通过指数缓动更新。
        from src.ui.smooth_scroll import SmoothScroller

        SmoothScroller.attach(self.preview_scroll_area)
        self.overview_splitter.addWidget(self.preview_scroll_area)

        # 下：「可下载文件」标题 + 文件滚动区
        files_wrap = QWidget()
        files_wrap.setObjectName("gbOvFilesWrap")
        files_wrap.setStyleSheet("#gbOvFilesWrap { background: transparent; }")
        files_outer = QVBoxLayout(files_wrap)
        files_outer.setContentsMargins(0, 0, 0, 0)
        files_outer.setSpacing(0)

        self.separator_label = QLabel(tr("可下载文件"))
        self.separator_label.setStyleSheet("font-weight:bold; padding:6px 8px;")
        files_outer.addWidget(self.separator_label)

        self.files_scroll_area = QScrollArea()
        self.files_scroll_area.setWidgetResizable(True)
        self.files_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 文件滚动区保持透明，显示外层面板背景。
        self.files_scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.files_container = QWidget()
        self.files_container.setObjectName("gbOvFiles")
        self.files_container.setStyleSheet("#gbOvFiles { background: transparent; }")
        self.files_layout = QVBoxLayout(self.files_container)
        self.files_layout.setContentsMargins(8, 4, 8, 8)
        self.files_layout.setSpacing(4)
        self.files_scroll_area.setWidget(self.files_container)
        # 在视口自身声明透明规则，并限定到控件标识。
        _fv = self.files_scroll_area.viewport()
        _fv.setObjectName("gbFilesVp")
        _fv.setAutoFillBackground(False)
        _fv.setStyleSheet("#gbFilesVp { background: transparent; }")
        SmoothScroller.attach(self.files_scroll_area)
        files_outer.addWidget(self.files_scroll_area, 1)

        # 详情加载后启用站点链接，供用户查看完整页面及评论。
        self.gb_open_btn = QPushButton(tr("在 GameBanana 中查看"))
        self.gb_open_btn.setEnabled(False)
        self.gb_open_btn.clicked.connect(self._on_open_in_gamebanana)
        # 按钮居中，并与文件区域保留间距。
        gb_open_wrap = QHBoxLayout()
        gb_open_wrap.setContentsMargins(8, 6, 8, 8)
        gb_open_wrap.addStretch(1)
        gb_open_wrap.addWidget(self.gb_open_btn)
        gb_open_wrap.addStretch(1)
        files_outer.addLayout(gb_open_wrap)

        files_wrap.setMinimumHeight(100)
        self.overview_splitter.addWidget(files_wrap)

        # 预览和文件区域初始按 6:4 分配，之后保留用户调整的比例。
        self.overview_splitter.setStretchFactor(0, 6)
        self.overview_splitter.setStretchFactor(1, 4)
        # 保存预览与文件区域的像素高度，并按比例恢复。
        from src.core.config import Config as _Cfg

        _s = _Cfg.instance().get("ui.store_detail_split", [360, 240])
        self.overview_splitter.setSizes(
            [int(_s[0]), int(_s[1])] if isinstance(_s, list) and len(_s) == 2 else [360, 240]
        )
        self.overview_splitter.splitterMoved.connect(self._on_overview_splitter_moved)
        return self.overview_splitter

    def _build_description(self) -> QWidget:
        """创建使用共用图片加载器及主题材质的描述 HTML 视图，并启用平滑滚动。"""
        from src.ui.widgets.gb_html_view import GbHtmlView

        self._html_desc = GbHtmlView(
            image_loader=self._image_loader,
            theme=theme.current_theme(),
        )
        self._apply_html_acrylic(self._html_desc)
        from src.ui.smooth_scroll import SmoothScroller

        SmoothScroller.attach(self._html_desc)
        return self._html_desc

    def _build_updates(self) -> QWidget:
        """创建更新记录 HTML 视图，沿用图片加载、主题材质和平滑滚动配置。"""
        from src.ui.widgets.gb_html_view import GbHtmlView

        self._html_updates = GbHtmlView(
            image_loader=self._image_loader,
            theme=theme.current_theme(),
        )
        self._apply_html_acrylic(self._html_updates)
        from src.ui.smooth_scroll import SmoothScroller

        SmoothScroller.attach(self._html_updates)
        return self._html_updates

    # 详情面板的模糊背景绘制。
    def set_backdrop(self, pixmap) -> None:
        """接收纯色主题所需的网格模糊快照并重绘；None 表示使用基础底色。"""
        self._backdrop = pixmap
        self.update()

    def paintEvent(self, event):
        """绘制圆角背景和边框。

        背景图主题使用半透明叠色；纯色主题使用网格模糊快照，
        快照尚未就绪时使用接近不透明的底色，避免短暂显示清晰的底层内容。
        """
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            radius = float(theme.radius("card"))
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            if theme.is_background_active():
                fill = QColor(theme.color("bg.layer"))
                fill.setAlphaF(0.90)
                p.fillPath(path, fill)
            elif self._backdrop is not None and not self._backdrop.isNull():
                p.save()
                p.setClipPath(path)
                # 模糊快照填满面板，再叠加半透明底色以保证文字可读。
                p.drawPixmap(self.rect(), self._backdrop)
                tint = QColor(theme.color("bg.layer"))
                tint.setAlphaF(0.78)  # 叠色透明度兼顾模糊背景与长文本可读性。
                p.fillRect(self.rect(), tint)
                p.restore()
            else:
                # 快照未就绪时使用基础底色。
                fill = QColor(theme.color("bg.layer"))
                fill.setAlphaF(0.97)
                p.fillPath(path, fill)
            pen = QPen(QColor(theme.color("stroke.card")))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawPath(path)
            p.end()
        except Exception:
            pass

    @staticmethod
    def _apply_html_acrylic(view) -> None:
        """按主题设置描述和更新区域的阅读底色。

        背景图主题提高不透明度，纯色主题保留更多面板模糊背景。
        """
        if theme.is_background_active():
            fill = theme.rgba("bg.card", 0.90)
        else:
            fill = theme.rgba("bg.card", 0.45)
        view.setStyleSheet(f"QTextBrowser {{ background-color: {fill}; border: none; }}")

    # 公开操作接口。
    def show_loading_for(self, record: GbModRecord) -> None:
        """选择 Mod 后立即填充标题、清空旧详情并取消旧图像请求。

        记录当前目标编号，使后续回调可以拒绝过期详情响应。
        """
        self._profile = None
        self._pending_mod_id = int(record.mod_id or 0)
        self.title_label.setText(record.name)
        self.author_label.setText(f"by {record.submitter.name}")
        self._clear_previews()
        self._clear_files()
        self._html_desc.set_html("")
        self._html_updates.set_html("")
        self._desc_loaded = False
        self._updates_loaded = False
        self._updates_fetch_inflight = False
        self.tabs.setCurrentIndex(0)
        # 默认隐藏更新标签，收到详情后按更新元数据决定是否显示，避免加载期间闪烁。
        self._set_updates_tab_visible(False)
        # GB 跳转按钮等到 profile 加载好才有 page_url，先禁用
        self.gb_open_btn.setEnabled(False)
        # 详情到达前显示连接状态。
        self._show_connecting()

    def load_profile(self, profile: GbModProfile) -> None:
        """拒绝与当前目标不符的详情响应，重建预览和文件列表后显示当前标签内容。"""
        # 拒绝与当前目标编号不符的详情响应，避免快速切换后被较早请求覆盖。
        incoming = int(getattr(profile, "mod_id", 0) or 0)
        if self._pending_mod_id and incoming and incoming != self._pending_mod_id:
            return
        self._profile = profile
        self.title_label.setText(profile.name)
        self.author_label.setText(f"by {profile.submitter.name}")
        # 获取 profile 后，page_url 有效，启用「在 GameBanana 中查看」按钮
        self.gb_open_btn.setEnabled(bool(profile.page_url))
        # 根据更新标志、更新数量或已加载记录决定更新标签是否可见。
        has_updates = (
            bool(getattr(profile, "has_updates", False))
            or int(getattr(profile, "updates_count", 0) or 0) > 0
            or bool(profile.updates)
        )
        self._set_updates_tab_visible(has_updates)
        self._clear_previews()
        for url in profile.preview_images:
            thumb = _AspectRatioImage()
            thumb.setStyleSheet(f"background:{theme.surface_color('bg.subtle', 'placeholder')};")
            # 图片加载期间显示骨架占位，setPixmap 设置图片后自动隐藏。
            thumb.start_loading()
            self._image_loader.load(
                url,
                thumb,
                size=QSize(self.PREVIEW_FETCH_MAX, self.PREVIEW_FETCH_MAX),
            )
            thumb.mousePressEvent = lambda _e, u=url: self.preview_clicked.emit(u)
            self.previews_layout.addWidget(thumb)
        # 末尾伸缩项使图片靠顶部排列，避免少量图片被布局拉伸到整个视口高度。
        self.previews_layout.addStretch(1)
        self._clear_files()
        for f in profile.files:
            row = self._make_file_row(f)
            self.files_layout.addWidget(row)
            self._file_rows.append(row)
        self.files_layout.addStretch(1)
        self._desc_loaded = False
        self._updates_loaded = False
        # 内容就绪后隐藏加载覆盖层。
        self._hide_connecting()
        # 当前标签可能在请求完成前已被选中，须主动加载内容，不能只依赖标签切换信号。
        self._on_tab_changed(self.tabs.currentIndex())

    def refresh_updates_view(self) -> None:
        """更新记录到达后重新渲染更新标签页，供下次切换时直接显示。"""
        if self._profile is None:
            return
        # 收到更新内容后显示更新页签，即使初始元数据未标记存在更新。
        if self._profile.updates and not self._is_updates_tab_visible():
            self._set_updates_tab_visible(True)
        html = self._render_updates_html(self._profile.updates)
        self._html_updates.set_html(html)
        self._updates_loaded = True
        self._updates_fetch_inflight = False

    @staticmethod
    def _render_updates_html(updates: list[GbModUpdateEntry]) -> str:
        """将更新记录渲染为包含标题、时间、影响文件和正文的 HTML。

        时间使用本地时区，零时间戳不显示；较多文件名合并为简短摘要。
        """
        from datetime import datetime

        if not updates:
            return ""
        parts: list[str] = []
        for u in updates:
            title = _html_escape(u.title or tr("更新"))
            ver = _html_escape(u.version or "")
            ver_html = f' <span style="color:#888; font-weight:normal; font-size:0.85em;">{ver}</span>' if ver else ""
            parts.append(f"<h3>{title}{ver_html}</h3>")
            meta_bits: list[str] = []
            ts = int(u.date_added_ts or 0)
            if ts > 0:
                try:
                    meta_bits.append(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))
                except (OSError, ValueError, OverflowError):
                    pass
            if u.affected_files:
                names = u.affected_files
                if len(names) > 3:
                    head = " / ".join(_html_escape(n) for n in names[:3])
                    affected_str = tr("{head} 等 {count} 个", head=head, count=len(names))
                else:
                    affected_str = " / ".join(_html_escape(n) for n in names)
                meta_bits.append(tr("影响文件：") + affected_str)
            if meta_bits:
                parts.append(
                    '<div style="color:#888; font-size:0.9em; margin:4px 0 8px;">' + " · ".join(meta_bits) + "</div>"
                )
            # 更新正文由 GbHtmlView 按 HTML 白名单清理后显示。
            parts.append(u.description_html or "")
            parts.append("<hr>")
        return "\n".join(parts)

    # 更新标签可见性。
    def _set_updates_tab_visible(self, visible: bool) -> None:
        """同步隐藏标签和内容，再重新定位指示条。"""
        self.tabs.setTabVisible(self._updates_tab_index, bool(visible))
        # 标签可见性变化后重新定位下划线。
        self._update_tab_indicator(animate=False)

    def _is_updates_tab_visible(self) -> bool:
        """返回更新记录标签当前是否对用户可见。"""
        return self.tabs.isTabVisible(self._updates_tab_index)

    # 内容及文件行管理。
    def _clear_previews(self) -> None:
        """先尝试取消每张预览的异步请求，再移除布局项并延后销毁图片控件。"""
        # 删除缩略图控件前取消对应请求，防止延迟结果写入正在销毁的对象。
        cancel = getattr(self._image_loader, "cancel_for", None)
        for i in reversed(range(self.previews_layout.count())):
            it = self.previews_layout.takeAt(i)
            w = it.widget() if it else None
            if w is not None:
                if callable(cancel):
                    try:
                        cancel(w)
                    except Exception:
                        pass
                w.deleteLater()

    def _clear_files(self) -> None:
        """移除文件行及按钮引用，保留下载状态和进度以便重新进入详情时恢复。"""
        for i in reversed(range(self.files_layout.count())):
            it = self.files_layout.takeAt(i)
            if it and it.widget() is not None:
                it.widget().deleteLater()
        self._file_rows.clear()
        # 清除旧按钮引用，但保留下载状态与进度，供重新进入详情时恢复。
        self._file_buttons.clear()

    def _make_file_row(self, file: GbModFile) -> QFrame:
        """组合文件名、大小及下载量，登记操作按钮并按现有下载或安装状态初始化。"""
        row = QFrame()
        row.setObjectName("gbFileRow")
        # 在行容器局部声明透明背景，使文件列表显示面板模糊底色。
        row.setStyleSheet("#gbFileRow, #gbFileRow QLabel { background: transparent; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 4, 8, 4)  # 右侧留白，下载按钮不贴边
        h.setSpacing(8)
        # 左侧文本块：文件名（上，可换行）+ 元信息行（下：大小 · 下载量），二者
        # 紧贴在一起、靠近文件名；下载按钮在最右、与整个文本块垂直居中。
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        name = QLabel(file.file_name)
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_box.addWidget(name)

        meta = QHBoxLayout()
        meta.setSpacing(4)
        size_lbl = QLabel(humanize_size(file.size_bytes))
        size_lbl.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 9pt;")
        meta.addWidget(size_lbl)
        dl_count = int(getattr(file, "download_count", 0) or 0)
        if dl_count:
            dl_icon = QLabel()
            icons.apply_pixmap(dl_icon, "meta/downloads", 12, "text.tertiary")
            dl_icon.setStyleSheet("background: transparent;")
            meta.addWidget(dl_icon)
            dl_num = QLabel(_format_count(dl_count))
            dl_num.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 9pt;")
            meta.addWidget(dl_num)
        meta.addStretch(1)
        text_box.addLayout(meta)
        h.addLayout(text_box, 1)

        btn = QPushButton()
        btn.setFixedSize(38, 30)
        self._file_buttons[file.file_id] = btn
        self._apply_file_button_state(file)
        h.addWidget(btn)
        h.setAlignment(btn, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _apply_file_button_state(self, file: GbModFile) -> None:
        """根据下载状态和本地文件计算按钮文案、可用性及操作。

        每次重新绑定前断开旧点击信号，防止一次点击触发重复下载。
        """
        btn = self._file_buttons.get(file.file_id)
        if btn is None:
            return
        if btn.isSignalConnected(QMetaMethod.fromSignal(btn.clicked)):
            btn.clicked.disconnect()
        state = self._file_states.get(file.file_id, "idle")
        btn.setIconSize(QSize(16, 16))
        btn.setText("")  # 图标按钮：文字描述移到 tooltip
        if state == "downloading":
            pct = self._file_progress.get(file.file_id, 0)
            btn.setToolTip(tr("下载中 {pct}%", pct=pct) if pct else tr("下载中…"))
            icons.apply_icon(btn, "meta/downloads", "text.disabled")
            btn.setEnabled(False)
            return
        is_local = self._has_local_md5(file.md5_checksum)
        # 按具体文件判断安装状态，同一在线 Mod 的文件可独立安装。
        file_installed = False
        if self._profile is not None:
            try:
                file_installed = bool(
                    self._is_file_installed(
                        int(getattr(self._profile, "mod_id", 0) or 0),
                        int(file.file_id or 0),
                        file.md5_checksum or "",
                    )
                )
            except Exception:
                file_installed = False
        btn.setEnabled(True)
        if file_installed:
            # 已安装文件的按钮仅显示状态，不连接安装操作。
            btn.setToolTip(tr("已安装"))
            icons.apply_icon(btn, "status/installed", "success")
        elif state == "failed":
            btn.setToolTip(tr("下载失败，点击重试"))
            icons.apply_icon(btn, "actions/refresh", "danger")
            btn.clicked.connect(lambda _, f=file: self.file_download_requested.emit(f))
        elif is_local or state == "done":
            # 用「导入」图标而非对钩：对钩是「已安装」的专属符号，两者同图标时用户
            # 会把「已下载待安装」看成已安装。
            btn.setToolTip(tr("已下载，点击安装"))
            icons.apply_icon(btn, "actions/import", "success")
            btn.clicked.connect(lambda _, f=file: self.file_install_requested.emit(f))
        else:
            btn.setToolTip(tr("下载"))
            icons.apply_icon(btn, "meta/downloads")
            btn.clicked.connect(lambda _, f=file: self.file_download_requested.emit(f))

    def refresh_file_buttons(self) -> None:
        """在缓存或安装状态改变后，重新计算按钮文案和操作回调。

        本地 Mod 删除等操作通过商店页转发到此处，同步已打开详情页的状态。
        """
        if self._profile is None:
            return
        # 移除可能失效的完成状态，再依据当前本地文件重新计算按钮。
        stale = [fid for fid, s in self._file_states.items() if s == "done"]
        for fid in stale:
            self._file_states.pop(fid, None)
        for f in self._profile.files:
            self._apply_file_button_state(f)

    def set_file_state(
        self,
        file_id: int,
        state: str,
        *,
        progress: int | None = None,
    ) -> None:
        """记录文件下载状态及百分比进度，并刷新当前详情中的对应按钮。

        即使该文件暂不在当前详情中也保留状态，供切回时恢复。
        """
        self._file_states[file_id] = state
        if progress is not None:
            self._file_progress[file_id] = progress
        if self._profile is None:
            return
        for f in self._profile.files:
            if f.file_id == file_id:
                self._apply_file_button_state(f)
                return

    def _on_open_in_gamebanana(self) -> None:
        """使用默认浏览器打开当前 Mod 的 GameBanana 页面。

        详情缺失时按钮禁用；打开失败不影响详情浏览。
        """
        if self._profile is None:
            return
        url = (self._profile.page_url or "").strip()
        if not url:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))

    def _on_overview_splitter_moved(self, pos: int, index: int) -> None:
        """保存预览与文件区域的分栏高度。"""
        sizes = self.overview_splitter.sizes()
        if len(sizes) == 2 and sum(sizes) > 0:
            from src.core.config import Config as _Cfg

            _Cfg.instance().set("ui.store_detail_split", [int(sizes[0]), int(sizes[1])])

    def eventFilter(self, obj, event):
        """标签几何或可见性变化时直接重定位下划线。"""
        tb = getattr(self, "_tab_indicator", None)
        if (
            tb is not None
            and obj is self.tabs.tabBar()
            and event.type()
            in (
                QEvent.Type.Resize,
                QEvent.Type.Show,
                QEvent.Type.LayoutRequest,
            )
        ):
            self._update_tab_indicator(animate=False)
        return super().eventFilter(obj, event)

    def _update_tab_indicator(self, animate: bool = True) -> None:
        """将下划线移到当前标签底部。

        首次直接定位，后续切换使用几何动画；没有有效当前标签时隐藏。
        """
        ind = getattr(self, "_tab_indicator", None)
        if ind is None:
            return
        bar = self.tabs.tabBar()
        idx = self.tabs.currentIndex()
        if idx < 0 or not bar.isTabVisible(idx):
            ind.hide()
            return
        rect = bar.tabRect(idx)
        if rect.width() <= 0 or rect.height() <= 0:
            ind.hide()
            return
        # 下划线位于标签底部中央，宽度约为标签的 64%，高度为两像素。
        bar_w = max(16, int(rect.width() * 0.64))
        x = rect.x() + (rect.width() - bar_w) // 2
        y = rect.bottom() - 2
        target = QRect(x, y, bar_w, 2)
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

    def _on_tab_changed(self, idx: int) -> None:
        """首次访问时填充描述或更新 HTML，缺少更新内容时发起一次受在途状态保护的补充请求。"""
        if self._profile is None:
            return
        if idx == 1 and not self._desc_loaded:
            self._html_desc.set_html(self._profile.description_html)
            self._desc_loaded = True
        elif idx == self._updates_tab_index and not self._updates_loaded:
            # ProfilePage 仅提供更新标志和数量；首次显示更新页时通过 Updates 端点获取完整记录。

            # 请求期间先渲染已有更新记录，完成后替换内容。
            self._html_updates.set_html(self._render_updates_html(self._profile.updates))
            self._updates_loaded = True
            if not self._profile.updates and self._updates_fetcher is not None and not self._updates_fetch_inflight:
                self._updates_fetch_inflight = True
                try:
                    self._updates_fetcher(self._profile)
                except Exception:
                    self._updates_fetch_inflight = False
