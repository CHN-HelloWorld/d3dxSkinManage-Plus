"""返回独立的变体快照，所有写入由宿主异步任务执行。"""

from __future__ import annotations

from dataclasses import dataclass

from src.plugin_sdk.facades import ModFacade
from src.plugin_sdk.operations import OperationTask


@dataclass(frozen=True)
class VariantVariableInfo:
    """插件可见的变体变量快照，包含 INI 定位、命名空间模式和保存值。"""

    ini_path: str
    variable: str
    namespace_mode: str
    value: str
    namespace: str = ""


@dataclass(frozen=True)
class VariantInfo:
    """指定 Mod 的命名变体快照，包含变量集合及来源内容指纹。"""

    variant_id: str
    mod_id: str
    game_id: str
    name: str
    variables: tuple[VariantVariableInfo, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    source_content_hash: str = ""
    declaration_fingerprint: str = ""


@dataclass(frozen=True)
class VariantStateInfo:
    """变体工作状态快照，描述当前选择、捕获可信度和可保存条件。"""

    mod_id: str
    game_id: str
    enabled: bool = False
    selected_variant_id: str | None = None
    status: str = "unconfirmed"
    can_save: bool = False
    reason: str = ""
    captured_at: str = ""
    variable_count: int = 0
    confirmed_count: int = 0
    source_changed: bool = False


class VariantFacade:
    """提供变体只读查询及经权限检查的宿主异步操作接口。"""

    def __init__(self, manager, granted, *, operations=None, owner=""):
        """绑定变体管理器，并复用带权限及插件归属的写操作入口。"""
        self._m = manager
        self._writes = ModFacade(manager, granted, operations=operations, owner=owner)

    def _close(self):
        """停用变体写操作入口，使后续提交返回失败任务。"""
        self._writes._close()

    def get_variants(self, mod_id: str) -> list[VariantInfo]:
        """复制指定 Mod 的全部命名变体及变量，并填入其实际游戏归属。"""
        self._writes._read_guard()
        game_id = self._m._index.get_mod_game_id(mod_id) or ""
        return [
            VariantInfo(
                v.variant_id,
                mod_id,
                game_id,
                v.name,
                tuple(
                    VariantVariableInfo(x.ini_path, x.variable, x.namespace_mode, x.value, x.namespace)
                    for x in v.variables
                ),
                v.created_at,
                v.updated_at,
                v.source_content_hash,
                v.declaration_fingerprint,
            )
            for v in self._m.variants.list_variants(mod_id)
        ]

    def get_state(self, mod_id: str) -> VariantStateInfo:
        """返回变体工作状态及 Mod 实际所属游戏的快照。"""
        self._writes._read_guard()
        state = self._m.variants.get_state(mod_id)
        return VariantStateInfo(game_id=self._m._index.get_mod_game_id(mod_id) or "", **state)

    def _submit(self, operation, *, permission="mods:write", **args):
        """检查操作权限后，将变体请求交给宿主异步队列。"""
        self._writes._require(permission)
        return self._writes._submit(operation, **args)

    def save(self, mod_id: str, name: str, *, use_defaults: bool = False) -> OperationTask:
        """请求将当前状态保存为命名变体；use_defaults 指定使用声明默认值。"""
        return self._submit("variant_save", mod_id=mod_id, name=name, use_defaults=use_defaults)

    def update(self, mod_id: str, variant_id: str, *, use_defaults: bool = False) -> OperationTask:
        """请求以当前状态或声明默认值更新已有变体。"""
        return self._submit("variant_update", mod_id=mod_id, variant_id=variant_id, use_defaults=use_defaults)

    def apply(self, mod_id: str, variant_id: str | None = None) -> OperationTask:
        """请求应用命名变体；variant_id 为 None 时恢复默认配置。"""
        return self._submit("variant_apply", mod_id=mod_id, variant_id=variant_id)

    def rename(self, mod_id: str, variant_id: str, name: str) -> OperationTask:
        """请求重命名指定 Mod 的变体。"""
        return self._submit("variant_rename", mod_id=mod_id, variant_id=variant_id, name=name)

    def duplicate(self, mod_id: str, variant_id: str, *, name: str | None = None) -> OperationTask:
        """请求复制变体，可指定副本名称。"""
        return self._submit("variant_duplicate", mod_id=mod_id, variant_id=variant_id, name=name)

    def delete(self, mod_id: str, variant_id: str) -> OperationTask:
        """检查 mods:manage 权限后请求删除变体。"""
        return self._submit("variant_delete", permission="mods:manage", mod_id=mod_id, variant_id=variant_id)
