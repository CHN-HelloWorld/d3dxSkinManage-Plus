"""校验可编辑语言包，不依赖 Qt 或应用配置。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter

PACK_FILENAME = "strings.json"
MAX_PACK_BYTES = 8 * 1024 * 1024
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\Z")
_QT_FIELD = re.compile(r"%(?:L?\d+|n)")
_PERCENT_FIELD = re.compile(r"%\([^)]+\)[#0 +\-]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")


def N_(source: str) -> str:
    """标记待提取原文，不加载配置或 Qt。"""
    return source


def valid_language_code(value: object) -> bool:
    """判断语言代码是否为长度不超过 64 的合法字符串。"""
    return isinstance(value, str) and len(value) <= 64 and _CODE.fullmatch(value) is not None


def normalize_language_code(value: str) -> str:
    """统一语言代码中的下划线与连字符，保持目录身份唯一。"""
    if not valid_language_code(value):
        return ""
    parts = value.replace("_", "-").split("-")
    return "-".join(
        [
            parts[0].lower(),
            *(
                part.upper()
                if len(part) == 2 and part.isalpha()
                else part.title()
                if len(part) == 4 and part.isalpha()
                else part
                for part in parts[1:]
            ),
        ]
    )


def _unique_object(pairs):
    """构建 JSON 对象并拒绝重复键，防止后出现的翻译静默覆盖前项。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def format_fields(text: str) -> set[str]:
    """提取格式化占位符名称，同时解析嵌套格式说明中的字段。"""
    fields = set()
    for _literal, name, spec, _conversion in Formatter().parse(text):
        if name is not None:
            fields.add(name)
            if spec:
                fields.update(format_fields(spec))
    return fields


def _field_formats(text: str) -> set[tuple[str, str | None, str]]:
    """收集占位符名称、类型转换和格式说明，供译文契约比对。"""
    return {
        (name, conversion, spec) for _literal, name, spec, conversion in Formatter().parse(text) if name is not None
    }


def translation_error(source: str, translation: str, *, qt: bool = False) -> str:
    """在译文进入界面前校验占位符。"""
    if qt:
        if set(_QT_FIELD.findall(source)) != set(_QT_FIELD.findall(translation)):
            return "Qt placeholders do not match"
    try:
        original = format_fields(source)
    except ValueError:
        return ""  # 非模板文本可直接包含花括号。
    try:
        translated = format_fields(translation)
    except ValueError as error:
        return f"Invalid format template: {error}"
    if original != translated:
        return f"Placeholders do not match: {sorted(original)} != {sorted(translated)}"
    if _field_formats(source) != _field_formats(translation):
        return "Keep each placeholder's conversion and format specifier unchanged"
    if set(_PERCENT_FIELD.findall(source)) != set(_PERCENT_FIELD.findall(translation)):
        return "Named percent placeholders do not match"
    return ""


@dataclass
class LanguagePack:
    """保存语言元数据、各翻译域及解析诊断的语言包结果。"""

    metadata: dict = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    gamebanana: dict[str, str] = field(default_factory=dict)
    qt: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


def read_language_pack(path: Path) -> LanguagePack:
    """拒绝损坏的语言包；单条译文错误仅影响该条目。"""
    if path.stat().st_size > MAX_PACK_BYTES:
        raise ValueError(f"Language pack exceeds {MAX_PACK_BYTES} bytes")
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(data, dict):
        raise ValueError("Language pack root must be a JSON object")
    if not any(key in data for key in ("_meta", "messages", "gamebanana", "qt")):
        data = {"messages": data}
    unknown = set(data) - {"$schema", "_meta", "messages", "gamebanana", "qt"}
    if unknown:
        raise ValueError(f"Unknown language pack sections: {', '.join(sorted(unknown))}")
    metadata = data.get("_meta", {})
    if not isinstance(metadata, dict):
        raise ValueError("_meta must be a JSON object")
    version = metadata.get("format", 1)
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported language pack format")
    for name in ("name", "fallback"):
        if name in metadata and not isinstance(metadata[name], str):
            raise ValueError(f"_meta.{name} must be text")
    if metadata.get("fallback") and not valid_language_code(metadata["fallback"]):
        raise ValueError("_meta.fallback must be a language folder code")
    pack = LanguagePack(metadata=metadata)
    for domain in ("messages", "gamebanana", "qt"):
        table = data.get(domain, {})
        if not isinstance(table, dict):
            raise ValueError(f"{domain} must be a JSON object")
        target = getattr(pack, domain)
        for source, translated in table.items():
            if not isinstance(translated, str):
                pack.diagnostics.append(f"{domain}.{source}: translation must be text")
                continue
            if not translated:
                continue
            error = translation_error(source, translated, qt=domain == "qt") if domain != "gamebanana" else ""
            if error:
                pack.diagnostics.append(f"{domain}.{source}: {error}")
                continue
            target[source] = translated
    return pack
