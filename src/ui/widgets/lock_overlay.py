"""无激活游戏时覆盖在内容页上的「请先激活游戏环境」提示遮罩。

无激活游戏时仍允许进入总览、画廊和 Mod 商店，由遮罩引导用户前往游戏页激活环境。
遮罩拦截鼠标事件，避免操作下方空页。MainWindow._update_lock_overlay 根据当前页和
激活状态控制显隐与几何。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from src.ui import icons, theme
from src.utils.i18n import t as tr


class LockOverlay(QWidget):
    """半透明遮罩 + 居中提示卡片；按钮点击发 go_to_game_requested。"""

    go_to_game_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建未激活游戏时的页面锁定层并构建引导内容。"""
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        """构建游戏激活说明卡片，将导航按钮连接到前往游戏页的请求信号。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addStretch(1)

        card = self._card = QFrame()
        card.setObjectName("lockOverlayCard")
        card.setMaximumWidth(440)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(36, 30, 36, 30)
        cl.setSpacing(14)

        icon = self._icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("background: transparent;")
        cl.addWidget(icon)

        title = self._title = QLabel(tr("请先激活一个游戏环境"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(title)

        hint = self._hint = QLabel(tr("总览、画廊与在线 Mod 需要先在「游戏」页激活一个游戏环境后才能使用。"))
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(hint)

        btn = QPushButton(tr("前往「游戏」页激活"))
        btn.setProperty("accent", True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self.go_to_game_requested.emit)
        cl.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)

        root.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)
        self.retheme()

    def retheme(self) -> None:
        """同步常驻提示卡片、文字、图标与遮罩的当前主题和背景材质。"""
        self._card.setStyleSheet(
            f"QFrame#lockOverlayCard {{ background-color: {theme.surface_color('bg.card', 'panel')};"
            f" border: 1px solid {theme.color('stroke.card')};"
            f" border-radius: {theme.radius('large')}px; }}"
        )
        self._title.setStyleSheet(
            f"color: {theme.color('text.primary')}; font-size: 16px; font-weight: bold; background: transparent;"
        )
        self._hint.setStyleSheet(f"color: {theme.color('text.secondary')}; font-size: 13px; background: transparent;")
        icons.apply_pixmap(self._icon, "actions/game", 48, "text.secondary")
        self.update()

    def paintEvent(self, _event) -> None:
        # 半透明遮罩：暗化（暗色主题）/ 提亮（亮色主题）底层空页，保留一点上下文。
        """根据当前明暗主题绘制半透明背景遮罩，突出中央激活引导。"""
        painter = QPainter(self)
        if theme.current_theme() == "dark":
            painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
        else:
            painter.fillRect(self.rect(), QColor(255, 255, 255, 150))
        painter.end()
