"""在下拉框文本左侧绘制固定的功能图标。

图标绘制时读取当前主题色，选项列表保持纯文本。
左侧间距通过 leadingIcon 属性交给全局样式管理，避免局部样式影响下拉箭头绘制。
"""

from __future__ import annotations

from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QComboBox, QListView, QStyledItemDelegate, QWidget

from src.ui import icons


class LeadingIconComboBox(QComboBox):
    """收起态在文本左侧绘制固定图标的下拉框（图标不进下拉项）。"""

    # 图标几何：x 起点 + 尺寸，需与 fluent.qss 中 leadingIcon 的 padding-left 对应
    _ICON_X = 8

    def __init__(
        self,
        icon_name: str,
        color_token: str = "text.secondary",
        icon_px: int = 16,
        parent: QWidget | None = None,
    ) -> None:
        """记录前置图标参数并安装等高列表视图，使用属性标记为图标预留文字空间。"""
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_color = color_token
        self._icon_px = icon_px
        # 让全局 qss 的 QComboBox[leadingIcon="true"] 规则给文本左侧留出图标位
        self.setProperty("leadingIcon", True)
        # 各页面共用列表代理，避免下拉面板准备样式前后的行尺寸不一致。
        view = QListView(self)
        view.setUniformItemSizes(True)
        self.setView(view)
        self.setItemDelegate(QStyledItemDelegate(view))

    def showPopup(self) -> None:
        # 计算弹窗高度前先解析样式和字体。
        """在显示下拉窗口前完成样式与项目布局，确保弹出列表尺寸准确。"""
        view = self.view()
        view.window().ensurePolished()
        view.ensurePolished()
        view.doItemsLayout()
        super().showPopup()

    def paintEvent(self, e) -> None:
        # 先让 QComboBox 画出本体（边框/当前文本/下拉箭头），再叠加前置图标
        """先执行组合框默认绘制，再在左侧垂直居中绘制主题图标。"""
        super().paintEvent(e)
        pm = icons.pixmap(self._icon_name, self._icon_px, self._icon_color)
        painter = QPainter(self)
        y = (self.height() - self._icon_px) // 2
        painter.drawPixmap(self._ICON_X, y, pm)
        painter.end()
