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


class PersistNamespaceUnavailable(ValueError):
    """持久变量作用域无法定位，但不代表 Mod 文件损坏或变量归属冲突。"""


@dataclass(frozen=True)
class ModIniSource:
    """提供单个 Mod 的变量归属输入。

    inis 使用有效 INI 的相对路径与文本。implicit_prefix 是部署目录相对于
    3DMigoto 根目录的规范路径；无法推导时使用空字符串，仅匹配显式命名空间。
    """

    mod_id: str
    inis: tuple[tuple[str, str], ...]
    implicit_prefix: str = ""
    excluded_inis: tuple[str, ...] = ()


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
    excluded_inis: dict[str, tuple[str, ...]] = field(default_factory=dict)

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


def full_key(namespace: str, var: str) -> str:
    """拼出上游 ``get_namespaced_var_name_lower`` 的等价键：``$\\<ns>\\<var>``（全小写）。"""
    return normalize_var_key("$\\" + namespace.strip("\\") + "\\" + var)


def build_attribution_index(sources: Iterable[ModIniSource]) -> AttributionIndex:
    """建唯一归属索引。冲突键从 ``owner`` 中剔除并登记到 ``conflicts``。"""
    declarations: list[PersistDeclaration] = []
    diagnostics: list[str] = []
    claims: dict[str, list[str]] = {}
    unresolved: set[str] = set()
    excluded_inis: dict[str, tuple[str, ...]] = {}

    for source in sources:
        dep_id = (source.mod_id or "").strip()
        if not dep_id:
            diagnostics.append(tr("跳过：mod_id 为空（不得并入同一个空 id 桶）"))
            continue
        excluded = {rel.replace("\\", "/").casefold() for rel in source.excluded_inis}
        if excluded:
            excluded_inis[dep_id] = tuple(sorted(excluded))
        for rel, text in source.inis:
            if rel.replace("\\", "/").casefold() in excluded:
                continue
            if not rel.lower().endswith(".ini"):
                continue
            text = decode_passthrough_text(text)
            variables = [var for var, _initial in _persist_declarations(text)]
            if not variables:
                continue
            try:
                explicit = parse_namespace(text)
            except ValueError as error:
                unresolved.add(dep_id)
                diagnostics.append(tr("跳过 {dep_id}/{rel}：{error}", dep_id=dep_id, rel=rel, error=error))
                continue
            if explicit:
                namespace, is_explicit = explicit.casefold(), True
            elif source.implicit_prefix:
                namespace, is_explicit = implicit_namespace(source.implicit_prefix, rel), False
            else:
                unresolved.add(dep_id)
                diagnostics.append(
                    tr(
                        (
                            "跳过 {dep_id}/{rel}：ini 未声明 namespace "
                            "且隐式前缀不可推导（当前部署环境无法定位 / 非激活部署）"
                        ),
                        dep_id=dep_id,
                        rel=rel,
                    )
                )
                continue
            for var in variables:
                key = full_key(namespace, var)
                declarations.append(
                    PersistDeclaration(
                        mod_id=dep_id,
                        rel=rel,
                        namespace=namespace,
                        var=var,
                        full_key=key,
                        explicit_namespace=is_explicit,
                    )
                )
                owners = claims.setdefault(key, [])
                owners.append(dep_id)

    owner: dict[str, str] = {}
    conflicts: dict[str, tuple[str, ...]] = {}
    aliases = {key: key_variants(key) for key in claims}
    alias_claims: dict[str, set[str]] = {}
    for key, variants in aliases.items():
        for variant in variants:
            alias_claims.setdefault(variant, set()).add(key)
    claim_order = {key: position for position, key in enumerate(claims)}
    for key, variants in aliases.items():
        collisions = {candidate for variant in variants for candidate in alias_claims[variant]}
        owners = [
            mod_id for candidate in sorted(collisions, key=claim_order.__getitem__) for mod_id in claims[candidate]
        ]
        if len(owners) == 1:
            owner[key] = owners[0]
            continue
        conflicts[key] = tuple(dict.fromkeys(owners))
        diagnostics.append(
            tr(
                "冲突：{key} 被重复声明 {count} 次（{value}），该键不写入任何 Mod 偏好",
                key=key,
                count=len(owners),
                value=", ".join(dict.fromkeys(owners)),
            )
        )

    keys_by_deployment: dict[str, list[str]] = {}
    for decl in declarations:
        bucket = keys_by_deployment.setdefault(decl.mod_id, [])
        if decl.full_key not in bucket:
            bucket.append(decl.full_key)

    return AttributionIndex(
        owner=owner,
        conflicts=conflicts,
        keys_by_deployment={k: tuple(v) for k, v in keys_by_deployment.items()},
        declarations=tuple(declarations),
        diagnostics=tuple(diagnostics),
        unresolved_mods=tuple(sorted(unresolved)),
        excluded_inis=excluded_inis,
    )


def _portable_variable(decl: PersistDeclaration, value: str) -> VariantVariable:
    """将声明和值转换为可随 Mod 移动的变体变量，保留显式命名空间信息。"""
    return VariantVariable(
        ini_path=decl.rel,
        variable=decl.var,
        namespace_mode="explicit" if decl.explicit_namespace else "implicit",
        namespace=normalize_var_key(decl.namespace) if decl.explicit_namespace else "",
        value=value,
    )


def _variable_identity(variable: VariantVariable) -> tuple[str, str, str, str]:
    """组合 INI 路径、变量名和命名空间属性，生成与当前值无关的变量定位键。"""
    return (variable.ini_path.casefold(), variable.variable, variable.namespace_mode, variable.namespace)


def _validated_declarations(index: AttributionIndex, mod_id: str) -> tuple[PersistDeclaration, ...]:
    """核对 Mod 声明唯一性、命名空间和归属索引一致性，拒绝无法确定归属的变量。"""
    if not isinstance(mod_id, str) or not mod_id.strip():
        raise ValueError(tr("mod_id 不能为空"))
    declarations = tuple(decl for decl in index.declarations if decl.mod_id == mod_id)
    locations: set[tuple[str, str]] = set()
    keys: set[str] = set()
    for decl in declarations:
        variable = _portable_variable(decl, "0")
        location = (variable.ini_path.casefold(), variable.variable)
        if location in locations or decl.full_key in keys:
            raise ValueError(tr("Mod 包含重复变量定位或重复声明"))
        if (
            not decl.namespace
            or decl.full_key != full_key(decl.namespace, decl.var)
            or index.resolve(decl.full_key) != mod_id
            or index.owner.get(decl.full_key) != mod_id
            or decl.full_key in index.conflicts
        ):
            raise ValueError(tr("持久变量无法唯一归属：{full_key}", full_key=decl.full_key))
        locations.add(location)
        keys.add(decl.full_key)
    if keys != set(index.keys_of(mod_id)):
        raise ValueError(tr("持久变量归属索引与声明不一致"))
    if mod_id in index.unresolved_mods:
        raise PersistNamespaceUnavailable(tr("Mod 包含无法确定命名空间的持久变量"))
    return declarations


def _fingerprint(declarations: Iterable[PersistDeclaration]) -> str:
    """排序可移植变量身份并计算 SHA-256，生成声明结构指纹。"""
    identities = sorted(_variable_identity(_portable_variable(decl, "0")) for decl in declarations)
    encoded = json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def declaration_fingerprint(index: AttributionIndex, mod_id: str) -> str:
    """计算完整声明结构的摘要，不包含变量值和部署状态。"""
    return _fingerprint(_validated_declarations(index, mod_id))


def snapshot_variables(index: AttributionIndex, mod_id: str, values: dict[str, str]) -> tuple[VariantVariable, ...]:
    """校验完整快照，并将运行时键转换为可移植定位信息。"""
    declarations = _validated_declarations(index, mod_id)
    if not isinstance(values, dict):
        raise ValueError(tr("变量快照必须是字典"))
    canonical: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or index.resolve(key) != mod_id:
            raise ValueError(tr("快照包含未知或归属不确定的变量：{key!r}", key=key))
        normalized = index.canonical_key(key)
        if normalized in canonical:
            raise ValueError(tr("快照包含重复变量：{key}", key=key))
        canonical[normalized] = validate_numeric_value(value)
    if set(canonical) != {decl.full_key for decl in declarations}:
        raise ValueError(tr("变量快照不完整或包含未知键"))
    return tuple(
        sorted(
            (_portable_variable(decl, canonical[decl.full_key]) for decl in declarations),
            key=_variable_identity,
        )
    )


def resolve_variant_values(index: AttributionIndex, mod_id: str, variant: ModVariant) -> dict[str, str]:
    """将每个可移植定位信息解析为当前部署的完整变量键。"""
    if not isinstance(variant, ModVariant):
        raise ValueError(tr("目标必须是 ModVariant"))
    declarations = _validated_declarations(index, mod_id)
    if variant.declaration_fingerprint and variant.declaration_fingerprint != _fingerprint(declarations):
        raise ValueError(tr("变体与当前 Mod 的变量声明版本不兼容"))
    stored = {_variable_identity(item): item.value for item in variant.variables}
    expected = {_variable_identity(_portable_variable(decl, "0")): decl.full_key for decl in declarations}
    if len(stored) != len(variant.variables) or set(stored) != set(expected):
        raise ValueError(tr("变体变量定位与当前完整声明不一致"))
    return {key: validate_numeric_value(stored[identity]) for identity, key in expected.items()}


def read_default_values(index: AttributionIndex, mod_id: str, body_inis: Iterable[tuple[str, str]]) -> dict[str, str]:
    """仅读取原始资源 INI 的字面默认值，不读取当前生效值。"""
    declarations = _validated_declarations(index, mod_id)
    expected = {_variable_identity(_portable_variable(decl, "0")): decl.full_key for decl in declarations}
    found: dict[tuple[str, str, str, str], str] = {}
    seen_paths: set[str] = set()
    excluded = set(index.excluded_inis.get(mod_id, ()))
    for rel, text in body_inis:
        if rel.replace("\\", "/").casefold() in excluded:
            continue
        text = decode_passthrough_text(text)
        namespace = parse_namespace(text)
        variables = tuple(_persist_declarations(text))
        path_key = rel.replace("\\", "/").casefold()
        if path_key in seen_paths:
            raise ValueError(tr("资源本体包含重复 INI 路径"))
        seen_paths.add(path_key)
        for var, initial in variables:
            variable = VariantVariable(
                ini_path=rel,
                variable=var,
                namespace_mode="explicit" if namespace else "implicit",
                namespace=normalize_var_key(namespace) if namespace else "",
                value="0" if initial is None else initial,
            )
            identity = _variable_identity(variable)
            if identity in found:
                raise ValueError(tr("资源本体包含重复变量声明"))
            found[identity] = variable.value
    if set(found) != set(expected):
        raise ValueError(tr("资源本体与当前完整变量声明不一致，无法恢复默认"))
    return {key: found[identity] for identity, key in expected.items()}
