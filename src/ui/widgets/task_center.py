"""显示导入、纳管和导出的批次进度。

独立模式定位于父内容区右下角，不参与布局尺寸分配；嵌入模式由宿主布局管理。
调用方推送批次和单项状态，面板不持有工作线程。折叠时显示总进度及操作按钮，
展开后显示可滚动的逐项明细。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons, theme
from src.ui.widgets.ring_progress import RingProgressButton
from src.utils.i18n import N_
from src.utils.i18n import t as tr

# 常量

PANEL_WIDTH = 340
ITEM_DOT_PX = 7  # 状态点直径
SLIDE_MS = 200
EXPAND_MS = 190  # 明细区展开/收起动画时长
LIST_MAX_HEIGHT = 200  # 明细区高度上限；超出即出滚动条
RESULT_HOLD_MS = 3500  # 成功后自动收起的等待时间。
# 任务浮层不占键盘焦点，按鼠标是否进入过选择终态自动收起的等待时间。
DISMISS_AFTER_LEAVE_MS = 800
DISMISS_UNTOUCHED_MS = 3500

# 供调用方传递单项状态的字符串常量。
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_ROLLED_BACK = "rolled_back"

_DOT_COLORS = {
    ST_QUEUED: "text.disabled",
    ST_RUNNING: "accent",
    ST_DONE: "success",
    ST_FAILED: "danger",
    ST_ROLLED_BACK: "warning",
}


# 省略号标签


class _ElideLabel(QLabel):
    """按可用宽度省略尾部文本的单行标签。

    保留完整文本，尺寸变化时重新计算显示内容，避免固定宽度面板直接裁切长名称。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        """保存完整文字并允许标签缩小，按当前可用宽度生成显示文本。"""
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self._sync()

    def setText(self, text: str) -> None:
        """更新完整文字缓存，并重新计算尾部省略结果。"""
        self._full = text or ""
        self._sync()

    def full_text(self) -> str:
        """返回未经省略的原始文本，供提示或状态处理使用。"""
        return self._full

    def _sync(self) -> None:
        """按字体度量和当前宽度更新显示文本，尚无可用宽度时保留完整内容。"""
        avail = max(0, self.width())
        if avail <= 0:
            super().setText(self._full)
            return
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))

    def resizeEvent(self, event) -> None:
        """尺寸变化后重新计算单行文字的省略位置。"""
        super().resizeEvent(event)
        self._sync()


# 逐项行


class _ItemRow(QWidget):
    """显示单项任务名称、状态标记及状态文案。"""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        """创建初始排队状态的任务行，使名称宽度让位于状态文案。"""
        super().__init__(parent)
        self._status = ST_QUEUED
        self._dot_color = "text.disabled"
        self._icon_name = ""
        self._icon_color = ""

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(7)

        self._dot = QLabel()
        self._dot.setFixedSize(ITEM_DOT_PX, ITEM_DOT_PX)
        lay.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        # 保持水平尺寸策略为 Ignored，使完整文本宽度不参与最小宽度计算，
        # 从而让标签按面板实际分配的宽度省略文本。
        self._name = _ElideLabel(name)
        lay.addWidget(self._name, 1)

        self._state_label = QLabel(tr("排队中"))
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._state_label, 0)

        self._apply_style()

    def set_status(self, status: str, text: str = "", *, done_text: str = N_("已完成")) -> None:
        """记录任务状态，选择默认或指定文案，并同步状态标记与文字颜色。"""
        self._status = status
        self._dot_color = _DOT_COLORS.get(status, "text.disabled")
        label = text or {
            ST_QUEUED: tr("排队中"),
            ST_DONE: tr(done_text),
            ST_FAILED: tr("失败"),
            ST_ROLLED_BACK: tr("已回滚"),
        }.get(status, "")
        self._state_label.setText(label)
        self._apply_style()

    def _apply_style(self) -> None:
        """按任务状态及可选自定义图标更新状态标记、名称和文案样式。"""
        if self._icon_name:
            self._dot.setFixedSize(14, 14)
            self._dot.setStyleSheet("background:transparent;")
            self._dot.setPixmap(icons.pixmap(self._icon_name, 14, self._icon_color or self._dot_color))
        else:
            self._dot.setFixedSize(ITEM_DOT_PX, ITEM_DOT_PX)
            self._dot.clear()
            dot_hex = theme.color(self._dot_color)
            self._dot.setStyleSheet(f"background:{dot_hex}; border-radius:{ITEM_DOT_PX // 2}px;")
        name_hex = theme.color("text.primary" if self._status == ST_RUNNING else "text.secondary")
        st_hex = theme.color(_DOT_COLORS.get(self._status, "text.tertiary"))
        self._name.setStyleSheet(f"color:{name_hex}; font-size:12px; background:transparent;")
        self._state_label.setStyleSheet(f"color:{st_hex}; font-size:11px; background:transparent;")

    def retheme(self) -> None:
        """使用当前主题重新应用任务行的状态样式。"""
        self._apply_style()

    def set_icon(self, name: str, color_token: str = "") -> None:
        """设置任务行的自定义状态图标及可选色标，并立即刷新显示。"""
        self._icon_name, self._icon_color = name, color_token
        self._apply_style()


# 面板主体


class TaskCenterStack:
    """同一父容器内多张任务浮层的右下角堆叠排版器。

    面板按进入顺序由下向上排列；一个面板收起后，其上方面板下移补位。
    仅管理纵向偏移，保留面板层级和内容。面板通过 ``note_shown / note_hidden``
    报告显隐状态，通过 ``offset_for`` 查询位置偏移。``_order`` 索引 0 对应最早进入的底部面板。
    """

    _GAP = 10  # 相邻两张浮层的垂直间距

    def __init__(self) -> None:
        """初始化面板显示顺序及明细高度协调的重入保护状态。"""
        self._order: list[TaskCenterPanel] = []
        self._fitting_details = False
