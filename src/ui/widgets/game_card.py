"""游戏页卡片，支持激活游戏及拖动排序。

双击请求激活，左键长按创建携带游戏编号的拖动；落点排序由游戏页面处理。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QDrag, QMouseEvent, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.core.game_service import GameConfig
from src.ui import icons, theme, win_dnd_admin
from src.ui.widgets.image_utils import fill_blur_rounded
from src.utils.i18n import t as tr

# 拖拽排序时 QDrag 携带的 mime 类型（GamePage 落点据此识别）
GAME_ID_MIME = "application/x-d3dx-game-id"


class GameCard(QFrame):
    """单个游戏卡片"""

    clicked = Signal(str)  # game_id（单击 → 选中）
    double_clicked = Signal(str)  # game_id（双击 → 激活环境）

    CARD_WIDTH = 180
    CARD_HEIGHT = 210
    _LONG_PRESS_MS = 220  # 左键按住超过此时长进入拖拽，借此与单击/双击区分

    def __init__(
        self,
        game: GameConfig | None = None,
        parent: QWidget | None = None,
        card_width: int = 180,
        card_height: int = 210,
    ) -> None:
        """初始化游戏卡片的选中、激活、拖放及草稿状态，创建长按计时器并绑定可选游戏。"""
        super().__init__(parent)
        self._game: GameConfig | None = None
        self._selected = False
        self._active = False
        self._hover = False
        self._drop_target = False  # 外部图片拖入悬停高亮
        self._draft = False  # 草稿卡片使用虚线边框，不允许拖动或启用。
        self._card_width = card_width
        self._card_height = card_height
        self._preview_height = self._preview_height_for(card_height)
        self._cached_pixmap: QPixmap | None = None
        self._press_pos = QPoint()
        self._maybe_click = False
        self._dragging = False
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press)
        self._setup_ui()
        if game:
            self.set_game(game)

    def _setup_ui(self) -> None:
        """构建游戏预览、当前环境徽标、排序抓手及双语名称区域。"""
        self.setFixedSize(self._card_width, self._card_height)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("gameCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._preview_container = QWidget()
        self._preview_container.setFixedHeight(self._preview_height)
        pc = QVBoxLayout(self._preview_container)
        pc.setContentsMargins(0, 0, 0, 0)
        pc.setSpacing(0)

        self._preview_label = QLabel(tr("无预览"))
        self._preview_label.setFixedHeight(self._preview_height)
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setObjectName("gamePreview")
        self._preview_label.setStyleSheet(
            f"QLabel#gamePreview {{ background-color: {theme.surface_color('bg.subtle', 'placeholder')};"
            f" color: {theme.color('text.tertiary')};"
            f" border-radius: {theme.radius('card')}px; font-size: 13px; }}"
        )
        pc.addWidget(self._preview_label)

        # 「当前」激活角标（右上）
        self._active_badge = QLabel(tr("当前"), self._preview_container)
        self._active_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._active_badge.setFixedSize(40, 18)
        self._active_badge.setStyleSheet(
            f"background-color: {theme.color('success')}; color: white;"
            f" font-size: 9px; font-weight: bold; border-radius: 4px;"
        )
        self._active_badge.move(self._card_width - 48, 6)
        self._active_badge.hide()

        # 拖拽手柄（hover 显示，左上）
        self._grip = QLabel(self._preview_container)
        self._grip.setPixmap(icons.pixmap("actions/reorder", 16, "text.on_accent"))
        self._grip.setFixedSize(20, 20)
        self._grip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._grip.setStyleSheet("background-color: rgba(0,0,0,0.35); border-radius: 4px;")
        self._grip.move(4, 4)
        self._grip.hide()

        layout.addWidget(self._preview_container)

        info = QWidget()
        # 文字区域显式透明，避免全局控件底色遮挡卡片背景。
        info.setObjectName("gameCardInfo")
        info.setStyleSheet("#gameCardInfo { background-color: transparent; }")
        il = QVBoxLayout(info)
        il.setContentsMargins(2, 2, 2, 0)
        il.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        self._status_indicator = QLabel()
        self._status_indicator.setFixedSize(6, 18)
        name_row.addWidget(self._status_indicator)
        self._name_label = QLabel(tr("未命名"))
        self._name_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        name_row.addWidget(self._name_label, 1)
        il.addLayout(name_row)
        self._en_label = QLabel("")
        self._en_label.setStyleSheet(
            f"color: {theme.surface_text_color('text.secondary')}; font-size: {theme.font('size_secondary')}px;"
        )
        il.addWidget(self._en_label)
        layout.addWidget(info)

    # 数据绑定。
    def set_game(self, game: GameConfig) -> None:
        """绑定游戏配置，更新省略显示的名称、预览图和状态外观。"""
        self._game = game
        self._elide(self._name_label, game.name or tr("未命名"), self._card_width - 24)
        self._elide(self._en_label, game.name_en or "", self._card_width - 12)
        self._load_pixmap()
        self._update_status()

    def _load_pixmap(self) -> None:
        """读取现有游戏预览并生成模糊填充圆角图，缺失或无效时显示名称缩写或占位文字。"""
        g = self._game
        if g and g.preview and Path(g.preview).exists():
            pm = QPixmap(g.preview)
            if not pm.isNull():
                self._cached_pixmap = pm
                self._preview_label.setText("")
                # 模糊填充：完整显示整图，比例与卡片不一致时用同图高斯模糊垫底留白，四角圆角，
                # 与 Mod 卡片观感一致。
                self._preview_label.setPixmap(
                    fill_blur_rounded(pm, self._card_width - 8, self._preview_height, radius=theme.radius("card"))
                )
                return
        self._cached_pixmap = None
        self._preview_label.setPixmap(QPixmap())
        self._preview_label.setText(g.name[:2] if g and g.name else tr("无预览"))

    @staticmethod
    def _elide(label: QLabel, text: str, avail: int) -> None:
        """按标签字体和可用宽度省略长文本，同时将完整文本保留为提示。"""
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(label.font())
        label.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, max(20, avail)))
        label.setToolTip(text or "")

    # 交互状态。
    def set_selected(self, sel: bool) -> None:
        """更新卡片选中状态并重新应用状态样式。"""
        self._selected = sel
        self._update_status()

    def set_active(self, active: bool) -> None:
        """更新当前游戏环境标志、徽标可见性及状态样式。"""
        self._active = active
        self._active_badge.setVisible(active)
        self._update_status()

    def _update_status(self) -> None:
        """综合当前环境、选中、悬停、投放目标和草稿状态更新状态条及卡片背景边框。"""
        if self._active:
            self._status_indicator.setStyleSheet(f"background-color: {theme.color('success')}; border-radius: 3px;")
            border = f"2px solid {theme.color('success')}"
        elif self._selected:
            self._status_indicator.setStyleSheet(f"background-color: {theme.color('accent')}; border-radius: 3px;")
            border = f"2px solid {theme.color('accent')}"
        else:
            self._status_indicator.setStyleSheet(
                f"background-color: {theme.color('stroke.divider')}; border-radius: 3px;"
            )
            border = f"1px solid {theme.color('stroke.card')}"
        # 选中时使用强调色亚克力，未选中时使用统一卡片背景。

        # 未选中卡片使用独立表面色，在浅色和深色主题下与页面保持可辨识层次。
        if self._selected:
            bg = theme.surface_color("accent.subtle", "card")
        else:
            bg = theme.surface_color("bg.card", "card")
        # 仅在未启用且未选中时显示悬停着色，避免覆盖启用和选中边框。
        if self._hover and not self._active and not self._selected:
            border = f"1px solid {theme.color('accent')}"
            bg = theme.surface_color("bg.subtle", "card")
        # 拖入图片悬停：虚线强调边框，提示「松手设为此游戏预览图」（优先级最高，盖过 hover/选中）
        if self._drop_target:
            border = f"2px dashed {theme.color('accent')}"
            bg = theme.surface_color("accent.subtle", "card")
        # 草稿使用虚线强调边框及浅底色，并保持选中状态。
        if self._draft:
            border = f"2px dashed {theme.color('accent')}"
            bg = theme.surface_color("accent.subtle", "card")
        self.setStyleSheet(
            f"#gameCard {{ border: {border}; background-color: {bg}; border-radius: {theme.radius('card')}px; }}"
        )

    def set_drop_target(self, on: bool) -> None:
        """外部图片拖拽悬停在本卡片上时的高亮（由 _CardGrid.dragMoveEvent 调用）。"""
        if self._drop_target == on:
            return
        self._drop_target = on
        self._update_status()

    def set_draft(self, on: bool) -> None:
        """设置草稿外观并禁止拖动排序。"""
        if self._draft == on:
            return
        self._draft = on
        self._update_status()
