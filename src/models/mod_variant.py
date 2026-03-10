"""可移植且不可变的命名 Mod 配置，不包含部署或预设状态。"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from typing import ClassVar

from src.utils.i18n import t as tr

_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_VARIABLE_RE = re.compile(r"\w+\Z")
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_DEVICE_RE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.IGNORECASE)


def _text(value: object, field: str) -> str:
    """校验字段为不含控制字符或代理码点的 Unicode 字符串，否则抛出 ValueError。"""
    if not isinstance(value, str) or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError(tr("{field} 必须是不含控制字符的有效 Unicode 字符串", field=field))
    return value


def validate_variant_id(value: str) -> str:
    """校验变体 ID 可安全用作文件标识，并拒绝 Windows 保留设备名。"""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or _DEVICE_RE.fullmatch(value):
        raise ValueError(tr("variant_id 必须是安全的非空文件标识"))
    return value


def normalize_variant_name(value: str) -> str:
    """去除名称首尾空白，拒绝空名称和非法 Unicode 文本。"""
    if not isinstance(value, str):
        raise ValueError(tr("变体名称必须是字符串"))
    name = _text(value.strip(), tr("变体名称"))
    if not name:
        raise ValueError(tr("变体名称不能为空"))
    return name


def normalize_ini_path(value: str) -> str:
    """拒绝越界路径和 Windows 名称别名，不依赖校验所在平台。"""
    path = _text(value, tr("INI 相对路径")).replace("\\", "/")
    components = path.split("/")
    if (
        not path.casefold().endswith(".ini")
        or any(char in path for char in '<>:"|?*')
        or any(
            part in ("", ".", "..") or part != part.strip() or part.endswith(".") or _DEVICE_RE.fullmatch(part)
            for part in components
        )
    ):
        raise ValueError(tr("变体 INI 必须使用不越界的相对 .ini 路径"))
    return path


def validate_numeric_value(value: str) -> str:
    """保留加载器可表达的有限十进制字面写法。"""
    if not isinstance(value, str):
        raise ValueError(tr("变体数值必须是数字字面量字符串"))
    literal = value.strip()
    if not _NUMBER_RE.fullmatch(literal):
        raise ValueError(tr("变体数值不是数字字面量：{value!r}", value=value))
    number = float(literal)
    try:
        loader_number = struct.unpack("f", struct.pack("f", number))[0]
    except OverflowError as error:
        raise ValueError(tr("变体数值超出加载器范围：{value!r}", value=value)) from error
    if not math.isfinite(number) or not math.isfinite(loader_number):
        raise ValueError(tr("变体数值必须有限：{value!r}", value=value))
    return literal


def _payload(data: object, required: set[str], optional: set[str]) -> dict:
    """校验 JSON 对象的必填和可选字段集合，拒绝缺失项及未知字段。"""
    if not isinstance(data, dict):
        raise ValueError(tr("变体数据必须是 JSON 对象"))
    if not required.issubset(data) or set(data) - required - optional:
        raise ValueError(tr("变体数据包含缺失或未知字段"))
    return data


@dataclass(frozen=True)
class VariantVariable:
    """不可变的变体变量，保存 INI 内定位、命名空间模式及数值。"""

    ini_path: str
    variable: str
    namespace_mode: str
    value: str
    namespace: str = ""

    def __post_init__(self) -> None:
        """规范化路径、变量名和数值，并拒绝不符合命名空间模式的字段组合。"""
        object.__setattr__(self, "ini_path", normalize_ini_path(self.ini_path))
        variable = _text(self.variable, tr("变量名"))
        if not _VARIABLE_RE.fullmatch(variable):
            raise ValueError(tr("变量名必须不含 $ 或命名空间"))
        object.__setattr__(self, "variable", variable.casefold())
        if self.namespace_mode not in ("explicit", "implicit"):
            raise ValueError(tr("命名空间模式必须为 explicit 或 implicit"))
        namespace = _text(self.namespace, tr("命名空间")).strip().replace("/", "\\").strip("\\").casefold()
        if self.namespace_mode == "implicit":
            if self.namespace:
                raise ValueError(tr("隐式命名空间不得保存部署路径"))
        elif not namespace or any(char in namespace for char in ":;$="):
            raise ValueError(tr("显式命名空间必须有效且非空"))
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "value", validate_numeric_value(self.value))

    def to_dict(self) -> dict:
        """导出变量记录；仅显式命名空间保存 namespace 字段。"""
        data = {
            "ini_path": self.ini_path,
            "variable": self.variable,
            "namespace_mode": self.namespace_mode,
            "value": self.value,
        }
        if self.namespace_mode == "explicit":
            data["namespace"] = self.namespace
        return data

    @classmethod
    def from_dict(cls, data: dict) -> VariantVariable:
        """从字段集合有效的对象构造变量，同时执行路径、命名空间和数值校验。"""
        data = _payload(data, {"ini_path", "variable", "namespace_mode", "value"}, {"namespace"})
        return cls(
            ini_path=data["ini_path"],
            variable=data["variable"],
            namespace_mode=data["namespace_mode"],
            value=data["value"],
            namespace=data.get("namespace", ""),
        )


@dataclass(frozen=True)
class ModVariant:
    """可分享的命名变体及其变量快照，以内容和声明指纹标记来源。"""

    variant_id: str
    name: str
    variables: tuple[VariantVariable, ...]
    created_at: str = ""
    updated_at: str = ""
    source_content_hash: str = ""
    declaration_fingerprint: str = ""

    kind: ClassVar[str] = "d3dx-mod-variant"

    def __post_init__(self) -> None:
        """校验变体身份和元数据，并拒绝同一 INI 中重复的变量定位。"""
        validate_variant_id(self.variant_id)
        object.__setattr__(self, "name", normalize_variant_name(self.name))
        if not isinstance(self.variables, tuple) or any(
            not isinstance(item, VariantVariable) for item in self.variables
        ):
            raise ValueError(tr("variables 必须是 VariantVariable 元组"))
        locations = [(item.ini_path.casefold(), item.variable) for item in self.variables]
        if len(locations) != len(set(locations)):
            raise ValueError(tr("变体包含重复变量定位"))
        for field in ("created_at", "updated_at", "source_content_hash", "declaration_fingerprint"):
            _text(getattr(self, field), field)

    def to_dict(self) -> dict:
        """导出包含格式标识、来源指纹和变量列表的变体记录。"""
        return {
            "kind": self.kind,
            "variant_id": self.variant_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_content_hash": self.source_content_hash,
            "declaration_fingerprint": self.declaration_fingerprint,
            "variables": [item.to_dict() for item in self.variables],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ModVariant:
        """读取当前变体格式并逐项校验变量；格式或字段无效时抛出 ValueError。"""
        metadata = {"created_at", "updated_at", "source_content_hash", "declaration_fingerprint"}
        data = _payload(data, {"kind", "variant_id", "name", "variables"}, metadata)
        if data["kind"] != cls.kind or not isinstance(data["variables"], list):
            raise ValueError(tr("变体 kind 或 variables 格式无效"))
        return cls(
            variant_id=data["variant_id"],
            name=data["name"],
            variables=tuple(VariantVariable.from_dict(item) for item in data["variables"]),
            **{field: data.get(field, "") for field in metadata},
        )
