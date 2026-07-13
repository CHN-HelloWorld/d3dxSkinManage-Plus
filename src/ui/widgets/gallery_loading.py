"""按视口大小生成画廊占位，开销限制在可见范围。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.ui.widgets.spinner import ShimmerPlaceholder, Spinner
from src.utils.i18n import t


class GalleryLoadingPanel(QWidget):
    """在 Mod 目录加载期间显示进度说明和随可见区域排列的骨架卡片。"""

    def __init__(self, card_size, parent=None):
        """创建穿透鼠标事件的加载提示层，记录骨架卡片尺寸并构建旋转图标和说明文字。"""
        super().__init__(parent)
        self._card_size = card_size
        self._cards = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._header = QWidget(self)
        row = QHBoxLayout(self._header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(Spinner(size=24, line_width=2))
        labels = QVBoxLayout()
        labels.setSpacing(4)
        self.title = QLabel(t("正在加载 Mod 数据…"))
        self.title.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.detail = QLabel(t("正在准备 Mod 库，完成后将显示预览"))
        labels.addWidget(self.title)
        labels.addWidget(self.detail)
        row.addLayout(labels)
        row.addStretch()

    def set_progress(self, count: int) -> None:
        """根据已读取数量更新提示，负数表示尚未获得可展示的计数。"""
        self.detail.setText(
            t("正在准备 Mod 库，完成后将显示预览")
            if count < 0
            else t("已读取 {count} 个 Mod，正在整理画廊…", count=f"{count:,}")
        )

    def resizeEvent(self, event):
        """按可用宽高排列最多二十四张骨架卡片，复用已创建控件并隐藏多余实例。"""
        super().resizeEvent(event)
        self._header.setGeometry(12, 8, max(0, self.width() - 24), 56)
        width, height = self._card_size
        cols = max(1, (self.width() - 24) // (width + 12))
        rows = max(0, (self.height() - 76 + height + 11) // (height + 12))
        count = min(24, cols * rows)
        while len(self._cards) < count:
            card = QWidget(self)
            card.setFixedSize(width, height)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(4, 4, 4, 8)
            layout.setSpacing(10)
            layout.addWidget(ShimmerPlaceholder(radius=8), 1)
            name = ShimmerPlaceholder()
            name.setFixedSize(width * 2 // 3, 14)
            layout.addWidget(name)
            status = ShimmerPlaceholder()
            status.setFixedSize(width // 3, 10)
            layout.addWidget(status)
            self._cards.append(card)
        for i, card in enumerate(self._cards):
            card.setVisible(i < count)
            if i < count:
                card.move(12 + (i % cols) * (width + 12), 76 + (i // cols) * (height + 12))
