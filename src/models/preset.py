"""预设数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.utils.i18n import t as tr


@dataclass
class Preset:
    """记录游戏中各类别的 Mod 启用状态。

    未出现在 character_mods 中的节点没有启用的 Mod。角色节点最多启用一个 Mod，
    类别节点可以启用多个。

    属性：
        id: 唯一标识，默认预设为 default_<game_id>。
        name: 显示名称。
        description: 预设描述。
        game_id: 所属游戏标识。
        is_default: 是否为不可删除的默认预设。
        character_mods: 节点标识到 Mod 标识列表的映射。
        created_at: ISO 格式的创建时间。
        updated_at: ISO 格式的最后修改时间。
    """

    id: str = ""
    name: str = ""
    description: str = ""
    game_id: str = ""
    is_default: bool = False
    character_mods: dict[str, list[str]] = field(default_factory=dict)
    mod_variants: dict[str, str | None] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def display_name(self) -> str:
        """翻译内置显示名，不改变存储名称和用户命名。"""
        return tr("默认") if self.is_default and self.name == "默认" else self.name

    @property
    def enabled_mod_ids(self) -> list[str]:
        """扁平化所有启用 mod_id"""
        out: list[str] = []
        for ids in self.character_mods.values():
            out.extend(ids)
        return out

    def add_mod_for_character(self, node_id: str, mod_id: str) -> None:
        """将 Mod 加入指定角色，去重并初始化其变体引用。"""
        bucket = self.character_mods.setdefault(node_id, [])
        if mod_id not in bucket:
            bucket.append(mod_id)
        self.mod_variants.setdefault(mod_id, None)
        self.updated_at = datetime.now().isoformat()

    def remove_mod(self, mod_id: str) -> None:
        """移除 Mod 的全部角色引用和变体选择，同时删除空角色分组。"""
        empty_keys: list[str] = []
        for cid, ids in self.character_mods.items():
            if mod_id in ids:
                ids.remove(mod_id)
            if not ids:
                empty_keys.append(cid)
        for k in empty_keys:
            del self.character_mods[k]
        self.mod_variants.pop(mod_id, None)
        self.updated_at = datetime.now().isoformat()

    def remove_character(self, node_id: str) -> None:
        """删除某角色的整条记录"""
        if node_id in self.character_mods:
            removed = self.character_mods.pop(node_id)
            remaining = set(self.enabled_mod_ids)
            for mod_id in removed:
                if mod_id not in remaining:
                    self.mod_variants.pop(mod_id, None)
            self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        """导出预设元数据和成员副本，仅保留已启用成员的变体引用。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "game_id": self.game_id,
            "is_default": self.is_default,
            "character_mods": {k: list(v) for k, v in self.character_mods.items()},
            "mod_variants": {mid: self.mod_variants.get(mid) for mid in self.enabled_mod_ids},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Preset:
        """严格反序列化，非法值抛出 ValueError。

        id、name、game_id、character_mods 必须存在且有效；
        is_default、时间戳和 description 为可选元数据，提供时校验类型。
        """
        if not isinstance(data, dict):
            raise ValueError(tr("预设记录必须是对象，实际是 {name__}", name__=type(data).__name__))

        ident: dict[str, str] = {}
        for key in ("id", "name", "game_id"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(tr("预设字段 {key} 必须是非空字符串，实际为 {value!r}", key=key, value=value))
            ident[key] = value

        if "character_mods" not in data:
            raise ValueError(tr("预设 {value} 缺少 character_mods", value=ident["id"]))
        raw_cm = data["character_mods"]
        if not isinstance(raw_cm, dict):
            raise ValueError(
                tr(
                    "预设 {value} 的 character_mods 必须是映射，实际是 {name__}",
                    value=ident["id"],
                    name__=type(raw_cm).__name__,
                )
            )
        character_mods: dict[str, list[str]] = {}
        for cid, mod_ids in raw_cm.items():
            if not isinstance(cid, str) or not cid:
                raise ValueError(tr("预设 {value} 的角色键不合法: {cid!r}", value=ident["id"], cid=cid))
            if not isinstance(mod_ids, list):
                raise ValueError(
                    tr(
                        "预设 {value} 角色 {cid} 的取值必须是列表，实际是 {name__}",
                        value=ident["id"],
                        cid=cid,
                        name__=type(mod_ids).__name__,
                    )
                )
            for mod_id in mod_ids:
                if not isinstance(mod_id, str) or not mod_id:
                    raise ValueError(
                        tr(
                            "预设 {value} 角色 {cid} 含不合法 mod_id: {mod_id!r}",
                            value=ident["id"],
                            cid=cid,
                            mod_id=mod_id,
                        )
                    )
            character_mods[cid] = list(mod_ids)

        raw_variants = data.get("mod_variants", {})
        if not isinstance(raw_variants, dict):
            raise ValueError(tr("mod_variants 必须是 Mod ID 到变体 ID 的映射"))
        enabled = {mid for mids in character_mods.values() for mid in mids}
        mod_variants = {}
        for mid, variant_id in raw_variants.items():
            if mid not in enabled:
                raise ValueError(tr("mod_variants 只能引用组合中的启用成员"))
            if variant_id is not None and (not isinstance(variant_id, str) or not variant_id.strip()):
                raise ValueError(tr("变体引用必须是非空 ID 或 null"))
            mod_variants[mid] = variant_id

        description = data.get("description", "")
        if not isinstance(description, str):
            raise ValueError(tr("预设 {value} 的 description 必须是字符串", value=ident["id"]))
        is_default = data.get("is_default", False)
        if not isinstance(is_default, bool):
            raise ValueError(
                tr(
                    "预设 {value} 的 is_default 必须是布尔值，实际为 {is_default!r}",
                    value=ident["id"],
                    is_default=is_default,
                )
            )
        now = datetime.now().isoformat()
        stamps: dict[str, str] = {}
        for key in ("created_at", "updated_at"):
            value = data.get(key, now)
            if not isinstance(value, str):
                raise ValueError(
                    tr(
                        "预设 {value} 的 {key} 必须是字符串，实际为 {value_2!r}",
                        value=ident["id"],
                        key=key,
                        value_2=value,
                    )
                )
            stamps[key] = value or now

        return cls(
            id=ident["id"],
            name=ident["name"],
            description=description,
            game_id=ident["game_id"],
            is_default=is_default,
            character_mods=character_mods,
            mod_variants=mod_variants,
            created_at=stamps["created_at"],
            updated_at=stamps["updated_at"],
        )
