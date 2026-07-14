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
