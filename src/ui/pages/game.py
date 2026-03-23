"""「游戏」页 —— 独立顶级页：卡片式增删改 + 预览图 + 激活。

有游戏或正在新建时，左=卡片/列表视图，右=可调宽的 GameDetailPanel（不可拖到隐藏）。
没有游戏时显示全宽引导页，点击添加后恢复编辑界面。
激活游戏 = 设置 GameService.current_game_id 并发 game_changed，全局生效。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config
from src.core.game_service import GameConfig, GameService
from src.ui import anim, icons, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.game_card import GAME_ID_MIME, GameCard
from src.ui.widgets.game_detail_panel import GameDetailPanel
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_cache_dir, get_data_dir, is_image


def _cleanup_clipboard_temp(path: str) -> None:
    """清理剪贴板图片暂存目录中的文件。

    仅处理 cache/clipboard 内的路径，保留用户选择的外部原图；删除失败时跳过。
    """
    try:
        p = Path(path).resolve()
        clip_dir = (get_cache_dir() / "clipboard").resolve()
        if clip_dir in p.parents and p.is_file():
            p.unlink(missing_ok=True)
    except OSError:
        pass


class _EmptyGameArt(QWidget):
    """绘制随主题变化的游戏图标和柔和椭圆背景。"""

    def __init__(self) -> None:
        """为无游戏状态的装饰图预留固定尺寸。"""
        super().__init__()
        self.setFixedSize(120, 94)

    def paintEvent(self, event) -> None:
        """按当前主题绘制空库背景和游戏图标，并保持高分屏图标清晰。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.color("accent.subtle")))
        painter.drawEllipse(QRectF(3, 7, 114, 87))
        ratio = self.devicePixelRatioF()
        symbol = icons.pixmap("actions/game", round(65 * ratio), "accent")
        symbol.setDevicePixelRatio(ratio)
        painter.drawPixmap(QPoint(27, 14), symbol)


class _CardGrid(QWidget):
    """卡片网格容器：承载 GameCard，接收长按拖拽排序的落点，以及外部图片拖入换预览图。"""

    def __init__(
        self, on_reorder, on_blank=None, on_image_drop=None, on_context=None, parent: QWidget | None = None
    ) -> None:
        """建立游戏卡片网格，保存排序、空白点击、预览图片拖入及右键菜单回调。"""
        super().__init__(parent)
        self._on_reorder = on_reorder
        self._on_blank = on_blank
        self._on_image_drop = on_image_drop  # 图片回调接收游戏标识和本地图片路径。
        self._on_context = on_context  # 右键菜单回调接收全局位置和命中卡片，空白区域传 None。
        self._drop_hover: GameCard | None = None
        self.setAcceptDrops(True)
        self.grid = QGridLayout(self)
        self.grid.setSpacing(12)
        self.grid.setContentsMargins(12, 12, 12, 12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    def mousePressEvent(self, e) -> None:
        """将落在网格空白区域的左键点击交给取消选择回调。"""
        # 卡片自行接收点击，传到容器的左键事件用于取消选择。
        if self._on_blank and e.button() == Qt.MouseButton.LeftButton:
            self._on_blank()
        super().mousePressEvent(e)

    def contextMenuEvent(self, e) -> None:
        """定位右键命中的游戏卡片，将卡片或空白区域及全局坐标交给菜单回调。"""
        # 按右键位置分别显示游戏菜单或空白区域菜单。
        if self._on_context is not None:
            card = self._card_at(e.pos())
            self._on_context(e.globalPos(), card)
            e.accept()
            return
        super().contextMenuEvent(e)

    def _card_at(self, pos) -> GameCard | None:
        """命中测试：返回落点 pos 下的可见 GameCard（无则 None）。"""
        for i in range(self.grid.count()):
            w = self.grid.itemAt(i).widget()
            if isinstance(w, GameCard) and w.isVisible() and w.geometry().contains(pos):
                return w
        return None
