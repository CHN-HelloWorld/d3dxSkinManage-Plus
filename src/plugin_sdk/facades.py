"""受控的宿主能力门面（公开 API）。

提供 Mod 查询、写入和高风险操作的受控入口，写操作须通过权限检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.errors import PermissionDenied
from src.plugin_sdk.operations import OperationResult, OperationTask
from src.utils.i18n import t as tr
from src.utils.logger import logger as _logger

if TYPE_CHECKING:
    from src.core.mod_manager import ModManager

# 公开快照隔离宿主内部模型，避免模型调整改变插件接口。


@dataclass(frozen=True)
class ModInfo:
    """向插件提供 Mod 的只读快照，隔离宿主内部可变对象。"""

    mod_id: str
    name: str
    author: str = ""
    enabled: bool = False
    node_id: str = ""
    game_id: str = ""
    rating: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CharacterInfo:
    """向插件提供分类或角色节点的只读身份与层级信息。"""

    id: str
    name: str
    name_en: str = ""
    parent_id: str = ""
    game_id: str = ""
    node_type: str = "category"


def _mod_to_info(m: Any, game_id: str = "") -> ModInfo:
    """复制 Mod 的公开字段并附上所属游戏 ID，标签转为不可变元组。"""
    return ModInfo(
        mod_id=m.mod_id,
        name=str(getattr(m, "display_name", None) or getattr(m, "name", "")),
        author=m.info.author,
        enabled=bool(getattr(m, "is_enabled", False)),
        node_id=str(getattr(m, "node_id", "") or ""),
        game_id=game_id,
        rating=m.info.age_rating.value,
        tags=tuple(m.info.tags),
    )


def _char_to_info(c: Any) -> CharacterInfo:
    """将宿主节点转换为插件只读快照，以空字符串表示根节点的父 ID。"""
    return CharacterInfo(
        id=str(getattr(c, "id", "")),
        name=str(getattr(c, "name", "") or ""),
        name_en=str(getattr(c, "name_en", "") or ""),
        parent_id=c.parent_id or "",
        game_id=c.game_id,
        node_type=c.node_type,
    )


class _PermMixin(ContextResource):
    """为业务门面提供统一的插件权限检查。"""

    _granted: frozenset[str]

    def _read_guard(self) -> None:
        """在任何宿主查询前验证权限、所属线程和上下文有效期。"""
        self._require_open()
        self._require("mods:read")

    def _require(self, perm: str) -> None:
        """检查指定权限，缺失时记录警告并抛出 PermissionDenied。"""
        if perm not in self._granted:
            _logger.warning("插件越权调用被拦截：缺少权限 %s", perm)
            raise PermissionDenied(perm)


class ModFacade(_PermMixin):
    """Mod 读写门面。写入操作须具备对应权限，并在主线程执行。"""

    def __init__(self, manager: ModManager, granted: frozenset[str], *, operations=None, owner: str = "") -> None:
        """绑定 Mod 管理器、已授予权限和带插件归属的异步操作队列。"""
        super().__init__()
        self._m = manager
        self._granted = granted
        self._operations = operations
        self._owner = owner
        self._closed = False

    def _close(self) -> None:
        """停用写操作入口，使之后的提交返回失败任务。"""
        self._closed_game_id = getattr(self._m, "current_game_id", "")
        self._closed = True

    # 查询返回独立快照，并要求读取权限。
    def current_game_id(self) -> str:
        """返回宿主当前选中的游戏 ID。"""
        self._read_guard()
        return self._m.current_game_id

    def get_mods(self, node_id: str | None = None) -> list[ModInfo]:
        """读取当前库中指定节点的 Mod 快照，node_id 为空时使用管理器默认范围。"""
        self._read_guard()
        return [self._info(m) for m in self._m.get_mods(node_id)]

    def get_mod(self, mod_id: str) -> ModInfo | None:
        """按稳定 ID 返回 Mod 快照，不存在时返回 None。"""
        self._read_guard()
        m = self._m.get_mod(mod_id)
        return self._info(m) if m else None

    def _info(self, mod: Any) -> ModInfo:
        """查询 Mod 实际所属游戏后构造公开快照，避免使用界面当前游戏替代归属。"""
        return _mod_to_info(mod, self._m._index.get_mod_game_id(mod.mod_id) or "")

    def search(self, query: str) -> list[ModInfo]:
        """使用宿主搜索规则查找 Mod，并返回隔离的公开快照。"""
        self._read_guard()
        return [self._info(m) for m in self._m.search_mods(query)]

    # 写（mods:write）
    def _submit(self, operation: str, **args) -> OperationTask:
        """将写操作提交给宿主队列；门面停用或服务缺失时返回已完成的失败任务。"""
        if self._closed or self._operations is None:
            task = OperationTask()
            ids = (args["mod_id"],) if args.get("mod_id") else ()
            task._finish(
                OperationResult(
                    self._closed_game_id if self._closed else getattr(self._m, "current_game_id", ""),
                    failed_ids=ids,
                    error=tr("插件已停用") if self._closed else tr("宿主未提供 Mod 操作服务"),
                )
            )
            return task
        self._require_open()
        return self._operations.submit(self._owner, operation, **args)

    def set_enabled(self, mod_id: str, enabled: bool) -> OperationTask:
        """检查 mods:write 权限后请求设置 Mod 启用状态，结果通过任务回调返回。"""
        self._require("mods:write")
        return self._submit("set_enabled", mod_id=mod_id, enabled=enabled)

    def toggle_mod(self, mod_id: str) -> OperationTask:
        """检查 mods:write 权限后请求切换 Mod 启用状态。"""
        self._require("mods:write")
        return self._submit("toggle_mod", mod_id=mod_id)

    def disable_all(self, node_id: str | None = None) -> OperationTask:
        """检查 mods:write 权限后请求禁用指定节点范围内的 Mod。"""
        self._require("mods:write")
        return self._submit("disable_all", node_id=node_id)

    # 高危（mods:manage）
    def delete_mod(self, mod_id: str) -> OperationTask:
        """检查 mods:manage 权限后请求宿主删除指定 Mod。"""
        self._require("mods:manage")
        return self._submit("delete_mod", mod_id=mod_id)


class GameFacade(_PermMixin):
    """游戏 / 角色树只读门面。"""

    def __init__(self, manager: ModManager, granted: frozenset[str]) -> None:
        """绑定游戏查询所需的管理器和插件权限集合。"""
        super().__init__()
        self._m = manager
        self._granted = granted

    def current_game_id(self) -> str:
        """返回宿主当前选中的游戏 ID。"""
        self._read_guard()
        return self._m.current_game_id

    def get_characters(self) -> list[CharacterInfo]:
        """返回当前游戏分类节点的只读快照列表。"""
        self._read_guard()
        return [_char_to_info(c) for c in self._m.get_characters()]

    def get_character(self, node_id: str) -> CharacterInfo | None:
        """按节点 ID 返回只读快照，不存在时返回 None。"""
        self._read_guard()
        c = self._m.get_character(node_id)
        return _char_to_info(c) if c else None
