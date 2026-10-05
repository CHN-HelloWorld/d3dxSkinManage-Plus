"""按应用语言包契约读取插件独立的翻译文件。"""

from __future__ import annotations

from pathlib import Path

from src.utils.i18n import get_i18n
from src.utils.language_pack import PACK_FILENAME, normalize_language_code, read_language_pack
from src.utils.translation_catalog import TranslationCatalog


class _PluginCatalog(TranslationCatalog):
    """限制语言包路径位于插件目录内的翻译目录。"""

    def __init__(self, manifest) -> None:
        """绑定插件根目录和语言子目录，再初始化默认翻译状态。"""
        self._plugin_root = manifest.directory
        self._root = self._plugin_root / "languages"
        super().__init__(directory=Path("."))

    def _discover(self):
        """扫描插件语言包，拒绝越出包目录的路径并报告重复语言代码。"""
        root = self._root
        found = {}
        try:
            if not root.resolve().is_relative_to(self._plugin_root.resolve()):
                self._report(f"Plugin language directory escapes its package: {root}")
                return {}
            for folder in sorted(root.iterdir(), key=lambda entry: entry.name.casefold()):
                code = normalize_language_code(folder.name)
                if not code or not folder.is_dir():
                    continue
                path = folder / PACK_FILENAME
                if not path.is_file():
                    continue
                if not path.resolve().is_relative_to(root.resolve()):
                    self._report(f"Plugin language file escapes its package: {path}")
                    continue
                if code in found:
                    self._report(f"Duplicate plugin language folder: {folder}")
                    continue
                found[code] = path
        except FileNotFoundError:
            pass
        except OSError as error:
            self._report(f"{root}: {error}")
        return found

    def _read_pack(self, path):
        """解析语言包并累计诊断；文件缺失或读取失败时返回 None。"""
        if path is None:
            return None
        try:
            pack = read_language_pack(path)
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            self._report(f"{path}: {error}")
            return None
        for diagnostic in pack.diagnostics:
            self._report(f"{path}: {diagnostic}")
        return pack


class PluginLocalizer:
    """延迟加载插件语言包，并在翻译请求时跟随宿主当前语言。"""

    def __init__(self, manifest) -> None:
        """保存插件清单，延迟到首次翻译时构造目录。"""
        self._manifest = manifest
        self._catalog = None
        self._loaded_language = None

    @property
    def language(self) -> str:
        """读取宿主当前语言代码。"""
        return get_i18n().current_language

    def t(self, source: str, /, **values) -> str:
        """首次调用时加载插件目录，语言变化时切换目录后翻译并格式化文本。"""
        language = self.language
        if self._catalog is None:
            self._catalog = _PluginCatalog(self._manifest)
        if language != self._loaded_language:
            self._catalog.set_language(language)
            self._loaded_language = language
        return self._catalog.t(source, **values)
