"""应用翻译入口，语言包使用 JSON 数据文件。"""

from pathlib import Path

from src.utils.language_pack import N_ as N_
from src.utils.paths import get_config_dir
from src.utils.translation_catalog import TranslationCatalog


class I18n(TranslationCatalog):
    """使用应用的路径规则定位语言目录。"""

    def __init__(self, *, directory: Path | None = None) -> None:
        """从指定目录载入翻译目录；未指定时使用应用配置下的 languages。"""
        super().__init__(directory=directory if directory is not None else get_config_dir() / "languages")


# 全局快捷函数
_i18n = I18n()


def t(source: str, /, **kwargs) -> str:
    """获取翻译文本的快捷函数"""
    return _i18n.t(source, **kwargs)


def set_language(lang: str) -> bool:
    """设置语言的快捷函数"""
    return _i18n.set_language(lang)


def get_i18n() -> I18n:
    """获取 I18n 实例"""
    return _i18n


def localize_category_name(name: str) -> str:
    """仅在显示时翻译外部分类名。"""
    return _i18n.category_name(name)
