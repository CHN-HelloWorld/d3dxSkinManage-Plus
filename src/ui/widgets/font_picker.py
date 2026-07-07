"""已安装字体选择器及搜索结果的字体预览。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QStyledItemDelegate

from src.ui.fonts import font_catalog


class FontPreviewDelegate(QStyledItemDelegate):
    """只为实际绘制行解析字体，不在整张弹窗布局时解析。"""

    def __init__(self, view) -> None:
        """订阅字体目录加载完成通知，以便目录就绪后重绘字体选择列表。"""
        super().__init__(view)
        self._catalog = font_catalog()
        self._catalog.ready.connect(view.viewport().update)

    def initStyleOption(self, option, index) -> None:
        """将列表项字体设为对应字体族并保留系统字体回退，同时更新文字测量信息。"""
        super().initStyleOption(option, index)
        if not self._catalog.is_ready:
            return
        system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        family = index.data(Qt.ItemDataRole.UserRole) or system.family()
        font = QFont(option.font)
        font.setFamilies(list(dict.fromkeys((family, system.family()))))
        option.font = font
        option.fontMetrics = QFontMetrics(font)

    def sizeHint(self, option, index) -> QSize:
        # 使用一致行高，尺寸计算不初始化每一种预览字体。
        """返回固定建议宽度和足以容纳字体高度的最小列表项尺寸。"""
        return QSize(260, max(32, option.fontMetrics.height() + 12))


def install_font_previews(combo: QComboBox) -> None:
    # 显式给下拉框设置代理，避免后续样式变化恢复默认代理。
    """为下拉列表与自动补全列表安装字体预览委托，并保留引用以维持 Qt 对象生命周期。"""
    views = (combo.view(), combo.completer().popup())
    delegates = tuple(FontPreviewDelegate(view) for view in views)
    combo.setItemDelegate(delegates[0])
    views[1].setItemDelegate(delegates[1])
    # 视图不拥有代理，须保留 Python 包装对象和补全弹窗引用。
    combo._font_preview_views = views
    combo._font_preview_delegates = delegates
    for view in views:
        view.setUniformItemSizes(True)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
