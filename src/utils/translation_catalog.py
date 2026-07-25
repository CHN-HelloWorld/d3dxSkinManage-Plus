"""从 config/languages/<locale>/strings.json 加载原文到译文的映射。"""

from __future__ import annotations

import logging
from pathlib import Path

from src.utils.language_pack import PACK_FILENAME, LanguagePack, normalize_language_code, read_language_pack

logger = logging.getLogger("d3dxSkinManage-Plus")


class TranslationCatalog:
    """独立的翻译目录，由 get_i18n 提供应用实例。

    每种语言对应配置目录中的一个文件。读取和翻译操作不会写入语言包。"""

    def __init__(self, *, directory: Path) -> None:
        """绑定语言包目录并载入默认中文翻译，初始化各翻译域和诊断列表。"""
        self.directory = Path(directory)
        self._current_lang = "zh-CN"
        self._strings: dict[str, str] = {}
        self._categories: dict[str, str] = {}
        self._qt: dict[str, str] = {}
        self.diagnostics: list[str] = []
        self.set_language("zh-CN")

    def _report(self, message: str) -> None:
        """记录尚未出现的语言包诊断并输出警告，避免重复刷写日志。"""
        if message not in self.diagnostics:
            self.diagnostics.append(message)
            logger.warning("Language pack: %s", message)

    def _discover(self) -> dict[str, Path]:
        """发现语言目录中的有效包文件，排除越界路径并报告重复代码及读取错误。"""
        found: dict[str, Path] = {}
        root = self.directory
        try:
            folders = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except FileNotFoundError:
            return found
        except OSError as error:
            self._report(f"{root}: {error}")
            return found
        for folder in folders:
            code = normalize_language_code(folder.name)
            if not code:
                continue
            path = folder / PACK_FILENAME
            try:
                if folder.is_dir() and path.is_file() and path.resolve().is_relative_to(root.resolve()):
                    if code in found:
                        self._report(f"Duplicate language folder {folder}; using {found[code].parent}")
                    else:
                        found[code] = path
            except OSError as error:
                self._report(f"{path}: {error}")
        return found

    def _read_pack(self, path: Path | None) -> LanguagePack | None:
        """读取并收集语言包诊断；缺失路径或解析失败时返回 None。"""
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

    def set_language(self, lang: str) -> bool:
        """加载或重新加载语言；无法使用时回退中文。"""
        self.diagnostics = []
        discovered = self._discover()
        requested = normalize_language_code(lang)
        selected = self._read_pack(discovered.get(requested)) if requested else None
        accepted = selected is not None
        if selected is None:
            if requested != "zh-CN":
                self._report(f"Language {lang!r} is unavailable; using zh-CN")
            requested = "zh-CN"
            selected = self._read_pack(discovered.get(requested)) or LanguagePack()
        layers = [selected]
        seen = {requested}
        fallback = normalize_language_code(selected.metadata.get("fallback", ""))
        while fallback and fallback not in seen and len(seen) < 16:
            seen.add(fallback)
            layer = self._read_pack(discovered.get(fallback))
            if layer is None:
                self._report(f"Fallback language {fallback!r} is unavailable")
                break
            layers.append(layer)
            fallback = normalize_language_code(layer.metadata.get("fallback", ""))
        self._strings, self._categories, self._qt = {}, {}, {}
        for layer in reversed(layers):
            self._strings.update(layer.messages)
            self._categories.update({key.casefold(): value for key, value in layer.gamebanana.items()})
            self._qt.update(layer.qt)
        self._current_lang = requested
        return accepted

    def t(self, source: str, /, **kwargs) -> str:
        """翻译并格式化源文本；译文格式化失败时依次尝试原文格式化和原文返回。"""
        text = self._strings.get(source) or source
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError, OverflowError):
            try:
                return source.format(**kwargs)
            except (KeyError, IndexError, ValueError, TypeError, AttributeError, OverflowError):
                return source

    def category_name(self, name: str) -> str:
        """仅翻译显示文字，不替换分类在接口中的原始名称。"""
        return self._categories.get(name.casefold(), name)

    def qt_text(self, source: str) -> str | None:
        # Qt 插件可通过 QObject.tr 或 QCoreApplication.translate 使用同一原文映射，
        # 无需导入宿主私有接口。
        """按 Qt 翻译域、普通文本域的顺序查找译文；未命中返回 None。"""
        return self._qt.get(source, self._strings.get(source))

    @property
    def current_language(self) -> str:
        """返回当前生效的语言代码。"""
        return self._current_lang

    @property
    def available_languages(self) -> list[tuple[str, str]]:
        """列出可读取语言包的代码和名称，保证中文存在并优先展示中文、英文。"""
        languages = []
        for code, path in self._discover().items():
            pack = self._read_pack(path)
            if pack is not None:
                languages.append((code, pack.metadata.get("name") or code))
        if not any(code == "zh-CN" for code, _name in languages):
            languages.append(("zh-CN", "简体中文"))
        return sorted(languages, key=lambda item: (item[0] != "zh-CN", item[0] != "en-US", item[0]))
