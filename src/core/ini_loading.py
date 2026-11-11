"""按加载器的递归排除和显式包含规则筛选参与变量分析的 INI。"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache

from src.core.migoto_user_ini import decode_passthrough_text
from src.utils.i18n import t as tr


def _path(value: str) -> str:
    """统一逻辑包含路径的分隔符和大小写，不访问磁盘。"""
    return posixpath.normpath(value.replace("\\", "/")).casefold()


def _include_entries(text: str) -> Iterable[tuple[str, str]]:
    """读取 Include 段的包含指令和主 Include 段的排除模式。"""
    text = decode_passthrough_text(text)
    if "[include" not in text.casefold():
        return
    section = ""
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff").strip()
        if not line or line.startswith((";", "//")):
            continue
        if line.startswith("["):
            section = line[1:].partition("]")[0].strip().casefold()
            continue
        if not section.startswith("include"):
            continue
        key, delimiter, value = line.partition("=")
        key = key.strip().casefold()
        value = value.partition(";")[0].strip()
        if not delimiter or not value:
            continue
        if key in ("include", "include_recursive") or (key == "exclude_recursive" and section == "include"):
            yield key, value


@lru_cache(maxsize=8)
def loader_include_entries(text: str) -> tuple[tuple[str, str], ...]:
    """复用内容相同的主配置解析结果，修改配置后按新文本立即重新解析。"""
    return tuple(_include_entries(text))


@dataclass(frozen=True)
class IniLoadRules:
    """冻结单个 Mod 的排除模式及显式包含例外，供读取和事务复核共用。"""

    excludes: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    recursive: tuple[str, ...] = ()

    def accepts(self, relative: str) -> bool:
        """判断相对 INI 是否由默认递归或显式包含加载，排除匹配不区分大小写。"""
        if not self.excludes:
            return True
        path = _path(relative)
        if path in self.includes:
            return True
        for root in ("", *self.recursive):
            if root and not path.startswith(root + "/"):
                continue
            tail = path[len(root) + 1 :] if root else path
            if not any(fnmatchcase(part, pattern) for part in tail.split("/") for pattern in self.excludes):
                return True
        return False

    def to_dict(self) -> dict:
        """将冻结规则序列化到事务前提，不改变 Mod 或偏好存储格式。"""
        return {"excludes": list(self.excludes), "includes": list(self.includes), "recursive": list(self.recursive)}

    @classmethod
    def from_dict(cls, data: dict) -> IniLoadRules:
        """严格读取事务保存的规则，拒绝不完整或类型错误的记录。"""
        fields = ("excludes", "includes", "recursive")
        if not isinstance(data, dict) or set(data) != set(fields):
            raise ValueError(tr("INI 加载规则记录无效"))
        if any(not isinstance(data[key], list) or not all(isinstance(v, str) for v in data[key]) for key in fields):
            raise ValueError(tr("INI 加载规则记录无效"))
        return cls(*(tuple(data[key]) for key in fields))


def mod_ini_rules(config_text: str, prefix: str, inis: Iterable[tuple[str, str]]) -> IniLoadRules:
    """读取全局排除规则，保留主配置和生效 INI 的显式包含；不遍历其他 Mod。"""
    entries = loader_include_entries(config_text)
    excludes = tuple(dict.fromkeys(value.casefold() for key, value in entries if key == "exclude_recursive"))
    if not excludes:
        return IniLoadRules()
    prefix = _path(prefix).strip("/")
    includes, recursive = set(), set()
    for key, value in entries:
        target = _path(value)
        if prefix and target.startswith(prefix + "/"):
            relative = target[len(prefix) + 1 :]
            if key == "include":
                includes.add(relative)
            elif key == "include_recursive":
                recursive.add(relative)
    remaining = {_path(rel): text for rel, text in inis}
    while remaining:
        rules = IniLoadRules(excludes, tuple(sorted(includes)), tuple(sorted(recursive)))
        loaded = [rel for rel in remaining if rules.accepts(rel)]
        if not loaded:
            break
        for rel in loaded:
            for key, value in _include_entries(remaining.pop(rel)):
                target = _path(posixpath.join(posixpath.dirname(rel), value.replace("\\", "/")))
                if key == "include":
                    includes.add(target)
                elif key == "include_recursive":
                    recursive.add(target)
    return IniLoadRules(excludes, tuple(sorted(includes)), tuple(sorted(recursive)))
