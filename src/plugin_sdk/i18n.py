"""插件自身的翻译接口，不导入 Qt 或宿主私有模块。"""

from __future__ import annotations

from typing import Protocol


def N_(source: str) -> str:
    """标记原文消息并保持其存储值稳定。"""
    return source


class _TranslationsLike(Protocol):
    """插件翻译门面依赖的语言代码与文本格式化协议。"""

    @property
    def language(self) -> str:
        """返回当前生效的语言代码。"""
        ...

    def t(self, source: str, /, **values) -> str:
        """翻译源文本并使用命名值填充占位符。"""
        ...


class TranslationFacade:
    """读取宿主语言设置，通过当前插件的语言包翻译。"""

    def __init__(self, translations: _TranslationsLike | None = None) -> None:
        """绑定插件翻译服务；未提供时使用中文原文。"""
        self._translations = translations

    @property
    def language(self) -> str:
        """返回插件翻译语言；无翻译服务时使用 zh-CN。"""
        return self._translations.language if self._translations is not None else "zh-CN"

    def t(self, source: str, /, **values) -> str:
        """委托翻译服务格式化文本；无服务时格式化原文，格式错误则保留原文。"""
        if self._translations is not None:
            return self._translations.t(source, **values)
        try:
            return source.format(**values) if values else source
        except (KeyError, IndexError, ValueError, TypeError, AttributeError, OverflowError):
            return source
