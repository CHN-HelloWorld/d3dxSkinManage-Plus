"""本地分类、角色和 Mod 卡片共用的类型标识。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from src.ui import icons, theme
from src.utils.i18n import t as tr


class CardTypeBadge(QFrame):
    """在卡片左上角显示随卡片宽度缩放的类型图标与名称。"""

    def __init__(self, kind: str, card_width: int, parent: QWidget) -> None:
        """创建类型徽标的图标和文字布局，应用初始类型与尺寸并置于卡片上层。"""
        super().__init__(parent)
        self.setObjectName("cardTypeBadge")
        self._layout = QHBoxLayout(self)
        self.icon_label = QLabel()
        self.text_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self.icon_label)
        self._layout.addWidget(self.text_label)
        self._style_key = None
        self.update_for_card(kind, card_width)
        self.move(4, 4)
        self.raise_()

    def update_for_card(self, kind: str, card_width: int) -> None:
        # 小卡片保持可读，大卡片限制标识尺寸，边距和图标随文字匹配。
        """根据卡片类型和宽度更新徽标字体、间距、图标及背景色，样式键不变时跳过重建。"""
        font_size = max(11, min(14, round(12 * card_width / 160)))
        background = theme.card_badge_bg(kind)
        style_key = (kind, font_size, background)
        if self._style_key == style_key:
            return
        self._style_key = style_key
        scale = font_size / 12
        vertical_padding = 3 if font_size >= 13 else 2
        self._layout.setContentsMargins(
            round(6 * scale),
            vertical_padding,
            round(8 * scale),
            vertical_padding,
        )
        self._layout.setSpacing(round(4 * scale))
        icon_size = round(15 * scale)
        icon_name = "actions/mod" if kind == "mod" else f"meta/{kind}"
        self.icon_label.setFixedSize(icon_size, icon_size)
        self.icon_label.setPixmap(icons.colored_pixmap_from(icon_name, icon_size, "#ffffff"))
        self.text_label.setText({"mod": "Mod", "character": tr("角色"), "category": tr("类别")}[kind])
        self.setStyleSheet(
            f"#cardTypeBadge {{ background-color: {background}; border-radius: 4px; }}"
            f"#cardTypeBadge QLabel {{ color: white; font-size: {font_size}px; font-weight: bold;"
            " background: transparent; }"
        )
        self.adjustSize()
