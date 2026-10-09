"""插件语义版本及依赖区间，构建标识不参与版本优先级。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_NUMBER = r"(?:0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"^({_NUMBER})\.({_NUMBER})\.({_NUMBER})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    """保留版本主次号及预发布标识，按语义版本规则比较。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Version:
        """解析完整 MAJOR.MINOR.PATCH 版本，拒绝前导零和不完整标识。"""
        match = _VERSION.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError(f"非法语义版本: {value!r}")
        prerelease = tuple(match[4].split(".")) if match[4] else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
            raise ValueError(f"预发布版本数字不能包含前导零: {value!r}")
        return cls(int(match[1]), int(match[2]), int(match[3]), prerelease)

    @property
    def core(self) -> tuple[int, int, int]:
        """返回不含预发布标识的三段版本号。"""
        return self.major, self.minor, self.patch

    def __lt__(self, other: Version) -> bool:
        """正式版本高于同号预发布，预发布中的数字标识按数值排序。"""
        if not isinstance(other, Version):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        left = tuple((0, int(part)) if part.isdigit() else (1, part) for part in self.prerelease)
        right = tuple((0, int(part)) if part.isdigit() else (1, part) for part in other.prerelease)
        return left < right


def constraint_bounds(constraint: str) -> tuple[tuple[str, Version], ...]:
    """将精确版本、通配符、兼容区间或比较符交集解析为边界。"""
    if not isinstance(constraint, str) or not constraint.strip():
        raise ValueError("依赖版本约束不能为空")
    if constraint == "*":
        return ()
    bounds = []
    for token in constraint.split():
        match = re.fullmatch(r"(\^|~|>=|<=|>|<|=)?(.+)", token)
        if match is None:
            raise ValueError(f"非法版本约束: {constraint!r}")
        operator, value = match.groups()
        version = Version.parse(value)
        if operator in {"^", "~"}:
            if operator == "~":
                upper = Version(version.major, version.minor + 1, 0)
            elif version.major:
                upper = Version(version.major + 1, 0, 0)
            elif version.minor:
                upper = Version(0, version.minor + 1, 0)
            else:
                upper = Version(0, 0, version.patch + 1)
            bounds.extend(((">=", version), ("<", upper)))
        else:
            bounds.append((operator or "=", version))
    return tuple(bounds)


def satisfies(value: str, constraint: str) -> bool:
    """判断版本是否满足全部边界；预发布必须由同号预发布边界显式允许。"""
    version = Version.parse(value)
    bounds = constraint_bounds(constraint)
    if version.prerelease and not any(bound.prerelease and bound.core == version.core for _, bound in bounds):
        return False
    for operator, bound in bounds:
        if not {
            "=": version == bound,
            ">": version > bound,
            ">=": version >= bound,
            "<": version < bound,
            "<=": version <= bound,
        }[operator]:
            return False
    return True
