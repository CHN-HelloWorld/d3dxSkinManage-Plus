"""通过可编辑 JSON 语言包提供 Qt 翻译入口。"""

from __future__ import annotations

from PySide6.QtCore import QTranslator

from src.utils.i18n import I18n


class LanguagePackTranslator(QTranslator):
    """将应用语言包接入 Qt 翻译器查询链。"""

    def __init__(self, catalog: I18n, parent=None):
        """保存语言目录，并将翻译器绑定到指定 Qt 父对象。"""
        super().__init__(parent)
        self.catalog = catalog

    def isEmpty(self) -> bool:
        """始终声明翻译器可用，使 Qt 查询自定义语言目录。"""
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        # None 表示无译文，允许 Qt 尝试其他翻译器；空字符串会真正隐藏文字。
        """查询 Qt 源文案的译文；缺失时返回 None，允许后续翻译器继续查找。"""
        return self.catalog.qt_text(source_text)
