"""标签名称、配色和独立编辑草稿，不依赖数据库或 Qt。"""

from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from src.models.mod import Mod
from src.utils.i18n import N_
from src.utils.i18n import t as tr

MAX_TAGS = 20
MAX_TAG_LENGTH = 24
TAG_PRESETS = {"blue": "#479EE0", "mint": "#58B98A", "purple": "#AD8AD7", "amber": "#D6AD58"}
TAG_PRESET_NAMES = {"blue": N_("蓝色"), "mint": N_("青色"), "purple": N_("紫色"), "amber": N_("琥珀")}
_PRESET_COLORS = {
    True: {
        "blue": ("#293C4A", "#B0D8F6"),
        "mint": ("#293D34", "#B3DFBE"),
        "purple": ("#393143", "#D8C2EE"),
        "amber": ("#403A2C", "#E9D39F"),
    },
    False: {
        "blue": ("#E7F1FA", "#255C86"),
        "mint": ("#E8F2EA", "#315F3C"),
        "purple": ("#F0EAFA", "#685085"),
        "amber": ("#F7EFD9", "#785B20"),
    },
}


def normalize_tag_name(value: str) -> str:
    """统一 Unicode 等价写法并折叠连续空白，返回去除首尾空白的名称。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def tag_key(value: str) -> str:
    """生成忽略大小写和空白差异的标签查找键。"""
    return normalize_tag_name(value).lower()


def validate_tag_name(value: str) -> str:
    """规范化标签名称，拒绝空值、超长文本、分隔符及控制字符。"""
    name = normalize_tag_name(value)
    if not name:
        raise ValueError(tr("请输入标签名称。"))
    if len(name) > MAX_TAG_LENGTH:
        raise ValueError(tr("单个新标签最多 24 个字，请缩短名称。"))
    if any(ch in value or ch in name for ch in (",", "，", "\r", "\n")):
        raise ValueError(tr("请一次输入一个标签；多个标签可在标签输入框中粘贴。"))
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise ValueError(tr("标签中不能包含控制字符。"))
    return name


def default_tag_color(name: str) -> str:
    """依据规范化名称计算稳定的预设色，使相同标签跨进程保持同色。"""
    value = 2166136261
    for character in tag_key(name):
        value = ((value ^ ord(character)) * 16777619) & 0xFFFFFFFF
    return tuple(TAG_PRESETS)[value % len(TAG_PRESETS)]


def normalize_tag_color(color: str) -> str:
    """接受预设色名或六位十六进制颜色，将后者统一为大写；非法值抛出 ValueError。"""
    if color in TAG_PRESETS:
        return color
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color.upper()
    raise ValueError(tr("颜色应为预设色或 #RRGGBB。"))


def _blend(a: str, b: str, amount: float) -> str:
    # 颜色舍入使用半值向上规则，不使用 Python 的偶数舍入。
    """按给定比例混合两个六位 RGB 颜色，以半值向上规则取整。"""
    return "#" + "".join(
        f"{int(int(a[i : i + 2], 16) * (1 - amount) + int(b[i : i + 2], 16) * amount + 0.5):02X}" for i in (1, 3, 5)
    )


def _luminance(color: str) -> float:
    """将 sRGB 分量线性化后计算相对亮度，供文字对比度计算使用。"""
    rgb = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(values, (0.2126, 0.7152, 0.0722)))


def tag_colors(color: str, dark: bool = True) -> tuple[str, str]:
    """计算标签配色，自定义文字满足 4.5:1 对比度要求。"""
    if color in TAG_PRESETS:
        return _PRESET_COLORS[bool(dark)][color]
    try:
        color = normalize_tag_color(color)
    except ValueError:
        return _PRESET_COLORS[bool(dark)]["blue"]
    background = _blend("#202020" if dark else "#FFFFFF", color, 0.20 if dark else 0.12)
    anchor = "#FFFFFF" if dark else "#111111"
    bg_luminance = _luminance(background)
    for step in range(41):
        foreground = _blend(color, anchor, step / 40)
        low, high = sorted((bg_luminance, _luminance(foreground)))
        if (high + 0.05) / (low + 0.05) >= 4.5:
            return background, foreground
    return background, anchor


@dataclass
class TagDefinition:
    """游戏内共享标签定义，包含规范化名称、配色及使用统计。"""

    id: str
    game_id: str
    name: str
    normalized_name: str
    color: str
    last_used_at: str = ""
    usage_count: int = 0


@dataclass
class TagCommitResult:
    # 返回元数据变化及当前 Mod；仅目录定义变化时，另行提供完整引用集合用于插件事件。
    """汇总标签提交影响的 Mod、删除项与元数据文件写入失败项。"""

    mods: list[Mod] = field(default_factory=list)
    deleted_tag_ids: set[str] = field(default_factory=set)
    sidecar_failed_ids: list[str] = field(default_factory=list)
    sidecar_mod_ids: set[str] = field(default_factory=set)
    affected_mod_ids: set[str] = field(default_factory=set)


class TagDraft:
    """独立的标签目录和有序选择，编辑过程不执行文件读写。"""

    def __init__(
        self,
        mod: Mod,
        game_id: str = "",
        definitions: Iterable[TagDefinition] = (),
        selected_ids: list[str] | None = None,
    ) -> None:
        """复制 Mod 和标签定义建立独立草稿；未传选择列表时按现有标签名称解析引用。"""
        self.original_mod = copy.deepcopy(mod)
        self.game_id = game_id
        self.mod_id = mod.mod_id
        self.definitions = {tag.id: copy.deepcopy(tag) for tag in definitions}
        if selected_ids is None:
            selected_ids = []
            by_name = {tag.normalized_name: tag.id for tag in self.definitions.values()}
            for name in mod.info.tags or []:
                if not isinstance(name, str) or not tag_key(name):
                    continue
                key = tag_key(name)
                tag_id = by_name.get(key)
                if tag_id is None:
                    tag_id = uuid.uuid4().hex
                    by_name[key] = tag_id
                    self.definitions[tag_id] = TagDefinition(
                        tag_id, game_id, name, key, default_tag_color(name), usage_count=1
                    )
                if tag_id not in selected_ids:
                    selected_ids.append(tag_id)
        self.selected_ids = list(selected_ids)
        self.original_selected_ids = list(selected_ids)
        self.original_definitions = copy.deepcopy(self.definitions)
        self.deleted_ids: set[str] = set()

    @classmethod
    def from_mod(
        cls,
        mod: Mod,
        game_id: str = "",
        definitions: Iterable[TagDefinition] = (),
        selected_ids: list[str] | None = None,
    ) -> TagDraft:
        """为指定 Mod 构造不影响原始对象的标签编辑草稿。"""
        return cls(mod, game_id, definitions, selected_ids)

    @property
    def names(self) -> list[str]:
        """按选择顺序返回尚未删除的标签名称。"""
        return [self.definitions[tag_id].name for tag_id in self.selected_ids if tag_id not in self.deleted_ids]

    @property
    def is_dirty(self) -> bool:
        """判断选择顺序、标签定义或删除记录是否偏离初始快照。"""
        return bool(
            self.deleted_ids
            or self.selected_ids != self.original_selected_ids
            or self.definitions != self.original_definitions
        )

    def _tag(self, tag_id: str) -> TagDefinition:
        """取得仍有效的标签定义，未知或已删除的 ID 抛出 ValueError。"""
        if tag_id in self.deleted_ids or tag_id not in self.definitions:
            raise ValueError(tr("这个标签已被删除，请重新选择。"))
        return self.definitions[tag_id]

    def _check_count(self, ids: list[str]) -> None:
        """限制新增选择的数量，同时允许保留或减少原有的超限标签集合。"""
        if len(ids) > MAX_TAGS and not set(ids).issubset(self.original_selected_ids):
            raise ValueError(tr("每个 Mod 最多添加 20 个标签；已有标签会完整保留。"))

    def add_names(self, names: list[str], color: str | None = None) -> list[str]:
        """先校验整批标签，再统一应用，不部分接受粘贴内容。"""
        if color is not None:
            color = normalize_tag_color(color)
        by_name = {tag.normalized_name: tag for tag in self.definitions.values() if tag.id not in self.deleted_ids}
        additions: list[TagDefinition] = []
        seen: set[str] = set()
        for raw in names:
            key = tag_key(raw)
            if key in seen:
                continue
            seen.add(key)
            existing = by_name.get(key)
            if existing is not None:
                additions.append(existing)
            else:
                name = validate_tag_name(raw)
                additions.append(
                    TagDefinition(uuid.uuid4().hex, self.game_id, name, key, color or default_tag_color(name))
                )
        ids = list(dict.fromkeys(self.selected_ids + [tag.id for tag in additions]))
        self._check_count(ids)
        for tag in additions:
            self.definitions[tag.id] = tag
        self.selected_ids = ids
        return [tag.id for tag in additions]

    def attach(self, tag_id: str) -> None:
        """将已有标签加入选择，去重并在修改前检查数量限制。"""
        self._tag(tag_id)
        ids = list(dict.fromkeys(self.selected_ids + [tag_id]))
        self._check_count(ids)
        self.selected_ids = ids

    def detach(self, tag_id: str) -> None:
        """解除当前 Mod 的标签选择，保留共享标签定义。"""
        self.selected_ids = [value for value in self.selected_ids if value != tag_id]

    def rename(self, tag_id: str, name: str) -> None:
        """校验并暂存标签新名称，拒绝与其他有效标签的规范化名称重复。"""
        tag = self._tag(tag_id)
        if name == tag.name:
            return
        name = validate_tag_name(name)
        key = tag_key(name)
        if any(
            other.id != tag_id and other.id not in self.deleted_ids and other.normalized_name == key
            for other in self.definitions.values()
        ):
            raise ValueError(tr("已有同名标签，请使用其他名称。"))
        tag.name, tag.normalized_name = name, key

    def set_color(self, tag_id: str, color: str) -> None:
        """校验并暂存指定标签的配色。"""
        self._tag(tag_id).color = normalize_tag_color(color)

    def remove_candidate(self, tag_id: str) -> None:
        """暂存共享标签定义及其全部 Mod 引用的删除。"""
        self._tag(tag_id)
        self.deleted_ids.add(tag_id)
        self.detach(tag_id)

    def usage(self, tag_id: str) -> int:
        """在持久化引用数上叠加当前草稿的选择变化，返回非负使用数。"""
        if tag_id in self.deleted_ids:
            return 0
        tag = self.definitions[tag_id]
        return max(0, tag.usage_count - int(tag_id in self.original_selected_ids) + int(tag_id in self.selected_ids))

    def candidates(self, query: str = "") -> list[TagDefinition]:
        """筛选匹配名称的有效标签，按匹配程度、选择状态、最近使用时间和使用量排序。"""
        key = tag_key(query)
        tags = [
            tag
            for tag in self.definitions.values()
            if tag.id not in self.deleted_ids and (not key or key in tag.normalized_name)
        ]
        tags.sort(key=lambda tag: (tag.normalized_name, tag.name))
        tags.sort(key=lambda tag: self.usage(tag.id), reverse=True)
        tags.sort(key=lambda tag: tag.last_used_at, reverse=True)
        tags.sort(
            key=lambda tag: (
                (
                    0 if key == tag.normalized_name else 1 if tag.normalized_name.startswith(key) else 2,
                    tag.id in self.selected_ids,
                )
                if key
                else (tag.id in self.selected_ids,)
            )
        )
        return tags

    def validate(self) -> None:
        """检查草稿中的新增与修改字段、名称唯一性及全部选择引用，非法状态抛出 ValueError。"""
        self._check_count(self.selected_ids)
        keys: set[str] = set()
        for tag_id, tag in self.definitions.items():
            if tag_id in self.deleted_ids:
                continue
            original = self.original_definitions.get(tag_id)
            if original is None or tag.name != original.name:
                validate_tag_name(tag.name)
            if original is None or tag.color != original.color:
                normalize_tag_color(tag.color)
            if tag.normalized_name != tag_key(tag.name) or tag.normalized_name in keys:
                raise ValueError(tr("已有同名标签，请使用其他名称。"))
            keys.add(tag.normalized_name)
        for tag_id in self.selected_ids:
            self._tag(tag_id)
