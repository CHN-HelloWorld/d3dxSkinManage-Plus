"""GameBanana 未收录游戏的空态视图。

当按游戏英文名在线匹配 GameBanana 失败时显示此视图，引导用户核对该游戏的英文名是否
与 GameBanana 官方名称一致，或到 GameBanana 网站手动搜索。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme
from src.utils.i18n import t as tr


class GbUnknownGameView(QFrame):
    """展示 GameBanana 未匹配游戏时的说明及设置和手动搜索入口。"""

    open_settings_requested = Signal()
    open_gamebanana_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """构建居中的未匹配提示，并将两个导航按钮连接到对应请求信号。"""
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        center = QVBoxLayout()
        center.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.message_label = QLabel(tr("GameBanana 未收录此游戏"))
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setStyleSheet("font-size:14pt; font-weight:bold;")
        center.addWidget(self.message_label)

        hint = QLabel(
            tr(
                "在线匹配是通过游戏英文名进行的——请到「游戏」页核对该游戏的英文名\n是否与 "
                "GameBanana 上的官方名称一致，或到 GameBanana 网站手动搜索。"
            )
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color:{theme.color('text.tertiary')}; padding:12px;")
        center.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.settings_btn = QPushButton(tr("打开游戏设置"))
        self.settings_btn.clicked.connect(self.open_settings_requested.emit)
        btn_row.addWidget(self.settings_btn)
        self.gamebanana_btn = QPushButton(tr("到 GameBanana 手动搜索"))
        self.gamebanana_btn.clicked.connect(self.open_gamebanana_requested.emit)
        btn_row.addWidget(self.gamebanana_btn)
        center.addLayout(btn_row)
        outer.addLayout(center)
        outer.addStretch(2)

    def set_game_name(self, name: str) -> None:
        """在未匹配提示中显示当前游戏名称。"""
        self.message_label.setText(tr("GameBanana 未收录「{name}」", name=name))
