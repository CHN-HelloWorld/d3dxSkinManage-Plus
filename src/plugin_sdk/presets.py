"""当前游戏的不可变预设快照与排队写入接口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from src.plugin_sdk.facades import ModFacade
from src.plugin_sdk.operations import OperationTask


@dataclass(frozen=True)
class PresetInfo:
    """插件可见的不可变预设快照，包含角色成员和每个成员的变体选择。"""

    preset_id: str
    game_id: str
    name: str
    description: str = ""
    is_default: bool = False
    enabled_mod_ids: tuple[str, ...] = ()
    node_mods: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    mod_variants: Mapping[str, str | None] = field(default_factory=lambda: MappingProxyType({}))
    unresolved_mod_ids: tuple[str, ...] = ()

    def __post_init__(self):
        """复制成员序列和映射并设为只读，隔离调用方传入的可变集合。"""
        object.__setattr__(self, "enabled_mod_ids", tuple(self.enabled_mod_ids))
        object.__setattr__(
            self, "node_mods", MappingProxyType({node: tuple(ids) for node, ids in self.node_mods.items()})
        )
        object.__setattr__(self, "mod_variants", MappingProxyType(dict(self.mod_variants)))
        object.__setattr__(self, "unresolved_mod_ids", tuple(self.unresolved_mod_ids))


def _snapshot(preset, unresolved_mod_ids: tuple[str, ...] = ()):
    """将宿主预设复制为插件公开的不可变数据结构。"""
    return PresetInfo(
        preset.id,
        preset.game_id,
        preset.name,
        preset.description,
        preset.is_default,
        tuple(preset.enabled_mod_ids),
        preset.character_mods,
        preset.mod_variants,
        unresolved_mod_ids,
    )


class PresetFacade:
    """通过宿主共享队列写入预设；禁止删除当前预设和默认预设。"""

    def __init__(self, manager, granted, *, operations=None, owner=""):
        """绑定预设查询服务，并复用带权限和插件归属的写操作入口。"""
        self._m = manager
        self._writes = ModFacade(manager, granted, operations=operations, owner=owner)

    def _close(self):
        """停用预设写操作入口，使后续提交返回失败任务。"""
        self._writes._close()

    def get_presets(self) -> list[PresetInfo]:
        """返回当前游戏全部预设的只读快照。"""
        self._writes._read_guard()
        manager = self._m.preset_manager
        return [
            _snapshot(preset, manager.get_unresolved_mod_ids(preset.id))
            for preset in manager.get_all_presets(self._m.current_game_id)
        ]

    def get_preset(self, preset_id: str) -> PresetInfo | None:
        """按预设 ID 查询快照，不存在时返回 None。"""
        self._writes._read_guard()
        manager = self._m.preset_manager
        preset = manager.get_preset(preset_id)
        return _snapshot(preset, manager.get_unresolved_mod_ids(preset_id)) if preset is not None else None

    def get_current(self) -> PresetInfo | None:
        """返回当前游戏所选预设；未选择游戏时返回 None。"""
        self._writes._read_guard()
        if not self._m.current_game_id:
            return None
        manager = self._m.preset_manager
        preset = manager.get_current(self._m.current_game_id)
        return _snapshot(preset, manager.get_unresolved_mod_ids(preset.id))

    def _submit(self, operation, *, permission="mods:write", **args):
        """检查操作所需权限后，将预设操作交给宿主异步队列。"""
        self._writes._require(permission)
        return self._writes._submit(operation, **args)

    def create(self, name: str, *, description: str = "") -> OperationTask:
        """请求创建命名预设，可同时保存说明，返回异步任务。"""
        return self._submit("preset_create", name=name, description=description)

    def duplicate(self, preset_id: str, *, name: str | None = None) -> OperationTask:
        """请求复制指定预设，可覆盖副本名称。"""
        return self._submit("preset_duplicate", preset_id=preset_id, name=name)

    def rename(self, preset_id: str, name: str) -> OperationTask:
        """请求更新预设名称，结果通过任务返回。"""
        return self._submit("preset_rename", preset_id=preset_id, name=name)

    def update_description(self, preset_id: str, description: str) -> OperationTask:
        """请求替换预设说明，结果通过任务返回。"""
        return self._submit("preset_update_description", preset_id=preset_id, description=description)

    def apply(self, preset_id: str) -> OperationTask:
        """请求切换到指定预设，由宿主协调部署及界面状态。"""
        return self._submit("preset_apply", preset_id=preset_id)

    def delete(self, preset_id: str) -> OperationTask:
        """检查 mods:manage 权限后请求删除预设。"""
        return self._submit("preset_delete", permission="mods:manage", preset_id=preset_id)
