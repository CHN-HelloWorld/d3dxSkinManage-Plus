"""解析有效 INI 中的 global persist 声明，建立变量到 Mod 的唯一归属索引。

模块不依赖 Qt，也不读取磁盘；调用方传入相对路径与文本，归档使用已准备的覆盖文件。
空 mod_id、无法确定的命名空间、重复声明和未声明变量均不参与值写回，并产生诊断。
不得通过显示名称、目录名称片段或导入顺序推断归属。

显式 namespace 位于首个段落之前，与部署路径无关。隐式命名空间是 INI 相对
3DMigoto 根目录的路径，包含文件名；移动、改名或停放部署都会改变它。
因此调用方仅为处于 ACTIVE 状态的部署提供隐式前缀。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.core.migoto_user_ini import (
    CONSTANTS_SECTION,
    decode_passthrough_text,
    key_variants,
    normalize_var_key,
)
from src.models.mod_variant import ModVariant, VariantVariable, validate_numeric_value
from src.utils.i18n import t as tr

# 节名使用第一个右方括号作为边界，缺失时取行尾；声明发现与初始值读取使用同一规则。
_SECTION_RE = re.compile(r"^\ufeff?\s*\[(?P<name>[^\]]*)(?:\]|$)")
_COMMENT_RE = re.compile(r"^\s*(?:;|//)")
_NAMESPACE_RE = re.compile(r"(?i)^\ufeff?\s*namespace\s*=\s*(?P<ns>[^;]*?)\s*(?:;.*)?$")
# 分组保留声明中的缩进、修饰符和行内注释。
_GLOBAL_DECL_WRITE_RE = re.compile(
    r"(?i)^(?P<indent>\s*)(?P<mods>(?:(?:global|persist|local)\s+)+)"
    r"\$(?P<var>\w+)\s*(?:=\s*(?P<init>[^;]*?))?(?P<trailer>\s*(?:;.*)?)$"
)


@dataclass(frozen=True)
class ModIniSource:
    """提供单个 Mod 的变量归属输入。

    inis 使用有效 INI 的相对路径与文本。implicit_prefix 是部署目录相对于
    3DMigoto 根目录的规范路径；无法推导时使用空字符串，仅匹配显式命名空间。
    """

    mod_id: str
    inis: tuple[tuple[str, str], ...]
    implicit_prefix: str = ""


@dataclass(frozen=True)
class PersistDeclaration:
    """记录持久变量的 Mod 归属、INI 相对路径、命名空间及完整运行键。"""

    mod_id: str
    rel: str
    namespace: str
    var: str
    full_key: str
    explicit_namespace: bool


@dataclass
class AttributionIndex:
    """``full_key -> mod_id`` 唯一归属表。"""

    owner: dict[str, str] = field(default_factory=dict)
    # 记录存在多个候选 Mod 的冲突键，按顺序去重且不分配所有者。
    conflicts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 该 Mod 声明的全部 full_key（含冲突键），供「删 d3dx_user 键」定位
    keys_by_deployment: dict[str, tuple[str, ...]] = field(default_factory=dict)
    declarations: tuple[PersistDeclaration, ...] = ()
    diagnostics: tuple[str, ...] = ()
    # 不完整索引不能作为完整快照保存。
    unresolved_mods: tuple[str, ...] = ()

    def resolve(self, key: str) -> str | None:
        """返回键唯一归属的 Mod 编号，无声明或存在冲突时返回 None。

        比较键在不同编码下的等价解释，避免同一字节序列的不同解码结果导致归属缺失。
        """
        variants = key_variants(key)
        if any(variant in self.conflicts for variant in variants):
            return None
        owners = {self.owner[variant] for variant in variants if variant in self.owner}
        return next(iter(owners)) if len(owners) == 1 else None

    def canonical_key(self, key: str) -> str:
        """将外部键转换为归属索引使用的规范形式。

        无法确定归属时返回规范化后的原键，供调用方报告诊断。
        """
        for variant in key_variants(key):
            if variant in self.owner or variant in self.conflicts:
                return variant
        return normalize_var_key(key)

    def keys_of(self, mod_id: str) -> tuple[str, ...]:
        """返回指定 Mod 已登记的运行变量键，无登记时返回空元组。"""
        return self.keys_by_deployment.get(mod_id, ())


def parse_namespace(text: str) -> str:
    """取 ini 的显式 namespace；没有则返回空串。

    按加载器 ParseIniPreamble 的规则使用第一个 section 之前的最后一次赋值。
    显式清空 namespace 与未声明不同；当前可携带定位不支持前者，必须拒绝。
    """
    namespace: str | None = None
    for line in text.splitlines():
        if not line.strip() or _COMMENT_RE.match(line):
            continue
        if _SECTION_RE.match(line):
            break
        m = _NAMESPACE_RE.match(line)
        if m:
            namespace = m.group("ns").strip().strip('"')
    if namespace is None:
        return ""
    if not namespace:
        raise ValueError(tr("显式 namespace 最终为空，无法可靠定位持久变量"))
    return namespace


def parse_persist_vars(text: str) -> list[str]:
    """取 ``[Constants]`` 段内所有 ``global persist $var`` 的变量名（不含 ``$``，小写）。"""
    return list(dict.fromkeys(var for var, _initial in _persist_declarations(text)))


def _persist_declarations(text: str) -> Iterable[tuple[str, str | None]]:
    """保留重复声明，并区分初始化值缺失和空值。"""
    section = ""
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group("name").strip().casefold()
            continue
        if section != CONSTANTS_SECTION.casefold():
            continue
        m = _GLOBAL_DECL_WRITE_RE.match(line)
        if not m:
            continue
        mods = {w.casefold() for w in m.group("mods").split()}
        if "global" not in mods or "persist" not in mods:
            continue
        yield m.group("var").casefold(), m.group("init")


def implicit_namespace(prefix: str, rel: str) -> str:
    """按加载器规则拼接隐式命名空间：部署相对路径与包含文件名的 INI 相对路径。"""
    tail = rel.replace("/", "\\").strip("\\")
    head = prefix.strip("\\")
    body = (head + "\\" + tail) if head else tail
    return body.casefold()
