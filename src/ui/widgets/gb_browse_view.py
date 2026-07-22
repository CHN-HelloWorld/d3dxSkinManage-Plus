"""组合商店筛选栏、分类导航、加载提示及卡片网格。

本模块负责布局，业务信号由 GameBananaStorePage 连接到服务调用。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, theme
from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid
from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar
from src.ui.widgets.gb_filter_bar import GbFilterBar
from src.ui.widgets.spinner import LoadingIndicator, Spinner
from src.utils.i18n import t as tr


class _LoadingBar(QFrame):
    """不占用布局空间的分页加载浮层。

    加载下一页时从内容区顶部滑入，完成或失败后滑出并隐藏。
    使用位置动画，避免重新分配布局空间改变网格位置。
    """

    BAR_HEIGHT = 40

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建固定高度且穿透鼠标的加载条，初始化旋转提示和滑入滑出动画引用。"""
        super().__init__(parent)
        self.setObjectName("gbLoadingBar")
        # 使用半透明底色和底部分隔线区分加载浮层。
        self.setStyleSheet(
            f"#gbLoadingBar {{ background-color: {theme.rgba('bg.subtle', 0.92)};"
            f" border-bottom: 1px solid {theme.color('stroke.divider')}; }}"
        )
        # 忽略鼠标事件，保持下方卡片可交互。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(8)
        row.addStretch(1)
        self._spinner = Spinner(size=18, line_width=2)
        row.addWidget(self._spinner)
        self._label = QLabel(tr("正在加载…"))
        self._label.setStyleSheet(f"color: {theme.color('text.secondary')}; font-size: 9pt; background: transparent;")
        row.addWidget(self._label)
        row.addStretch(1)
        self.setFixedHeight(self.BAR_HEIGHT)
        self.hide()
        self._anim: QPropertyAnimation | None = None

    def set_width(self, w: int) -> None:
        """跟随内容区宽度铺满顶部（x 固定 0，y 保持当前位移状态）。"""
        self.setGeometry(0, self.y(), max(1, w), self.BAR_HEIGHT)

    def drop_in(self) -> None:
        """从顶边上方下落滑入。"""
        self._spinner.start()
        self.move(0, -self.BAR_HEIGHT)  # 从内容区顶部之外开始显示动画。
        self.show()
        self.raise_()
        self._animate_y(0)

    def roll_up(self) -> None:
        """向上滑出后隐藏并停止加载动画。"""
        self._animate_y(-self.BAR_HEIGHT, on_finished=self._after_up)

    def _after_up(self) -> None:
        """加载条退场完成后停止旋转动画并隐藏控件。"""
        self._spinner.stop()
        self.hide()

    def _animate_y(self, target_y: int, on_finished=None) -> None:
        # 停止现有位置动画，避免同时写入同一属性。
        """中止旧动画并沿纵向移动加载条，禁用动画时直接定位并执行完成回调。"""
        if self._anim is not None:
            try:
                self._anim.stop()
            except RuntimeError:
                pass
            self._anim = None
        if not anim.is_enabled():
            self.move(0, target_y)
            if on_finished:
                on_finished()
            return
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(anim.DURATION["normal"])
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.setStartValue(self.pos())
        a.setEndValue(QPoint(0, target_y))
        if on_finished:
            a.finished.connect(on_finished)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._anim = a


class GbBrowseView(QFrame):
    """组合筛选栏、分类路径和网格状态页，并提供追加加载提示。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """构建筛选、路径、加载、空结果及网格视图，并连接网格追加加载状态。"""
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.filter_bar = GbFilterBar()
        outer.addWidget(self.filter_bar)

        self.breadcrumb = GbBreadcrumbBar()
        outer.addWidget(self.breadcrumb)

        self.stack = QStackedWidget()
        self.grid = GbAdaptiveGrid()
        # 首次加载使用指示器和状态文字。
        self.loading_state = LoadingIndicator(tr("正在加载…"))
        self.empty_state = QLabel(
            tr("未找到匹配的 Mod，试试其他关键词。"),
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.loading_state)
        self.stack.addWidget(self.empty_state)
        outer.addWidget(self.stack, 1)

        # 分页加载条以堆叠视图为父对象，独立于内容布局，使用垂直位移显示或隐藏。
        self.loading_bar = _LoadingBar(self.stack)

        # 顶部加载条接上 grid 的分页生命周期信号
        self.grid.loading_more_started.connect(self._on_loading_more_started)
        self.grid.loading_more_finished.connect(self.loading_bar.roll_up)

    def resizeEvent(self, event):
        """在浏览视图尺寸变化后同步悬浮加载条宽度，容忍加载条已经销毁。"""
        super().resizeEvent(event)
        # 浮条宽度跟随内容区
        try:
            self.loading_bar.set_width(self.stack.width())
        except RuntimeError:
            pass

    def _on_loading_more_started(self) -> None:
        # 显示前同步当前内容宽度。
        """将加载条调整到内容区宽度并执行入场显示。"""
        self.loading_bar.set_width(self.stack.width())
        self.loading_bar.drop_in()

    def show_loading(self) -> None:
        """切换到整页加载提示视图。"""
        self.stack.setCurrentWidget(self.loading_state)

    def show_empty(self) -> None:
        """切换到搜索无匹配结果的提示视图。"""
        self.stack.setCurrentWidget(self.empty_state)

    def show_grid(self) -> None:
        # 仅从加载或空状态进入网格时播放整页淡入；分页追加由卡片队列逐项显示。
        """显示结果网格，仅在从其他状态页切入时执行淡入动画。"""
        already_grid = self.stack.currentWidget() is self.grid
        self.stack.setCurrentWidget(self.grid)
        if not already_grid:
            anim.fade_in(self.grid)
