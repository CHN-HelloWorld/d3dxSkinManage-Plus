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
