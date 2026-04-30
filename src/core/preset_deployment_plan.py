"""不访问文件系统的预设部署规划。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from src.core.mod_identity import IdentityConflict, read_deployment_identity, validate_mod_id
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import Mod
from src.utils.constants import MIGOTO_DISABLED_PREFIX
from src.utils.paths import resolve_stored_path, to_stored_path

ActionKind = Literal["park", "restore", "materialize_active", "noop"]


@dataclass(frozen=True)
class DeploymentAction:
    """描述单个 Mod 在预设切换中的动作及源、活动、停放和临时路径。"""

    mod_id: str
    source_path: str
    active_path: str
    disabled_path: str
    partial_path: str
    action: ActionKind


@dataclass(frozen=True)
class PresetDeploymentPlan:
    """冻结预设切换的游戏、前后预设和部署动作，供执行及恢复使用。"""

    operation_id: str
    game_id: str
    from_preset_id: str
    to_preset_id: str
    actions: tuple[DeploymentAction, ...]

    @property
    def has_materialization(self) -> bool:
        """判断计划是否需要新建部署副本。"""
        return any(a.action == "materialize_active" for a in self.actions)

    def to_json(self) -> str:
        """将计划序列化为紧凑 JSON，并把源路径转换为可迁移存储形式。"""
        data = asdict(self)
        for action in data["actions"]:
            action["source_path"] = to_stored_path(action["source_path"])
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> PresetDeploymentPlan:
        """解析持久计划并恢复源路径和不可变动作对象。"""
        data = json.loads(raw)
        data["actions"] = tuple(
            DeploymentAction(**{**item, "source_path": resolve_stored_path(item["source_path"])})
            for item in data["actions"]
        )
        return cls(**data)


def _path_key(path: str | Path) -> str:
    """生成部署路径的绝对规范比较键，用于检测目标占用与路径一致性。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _safe_name(raw: str) -> str:
    """校验部署名称为非空单层名称，拒绝点号和路径分隔符。"""
    name = str(raw).strip()
    if not name or name in (".", "..") or Path(name).name != name:
        raise ValueError(f"invalid deploy name: {raw!r}")
    if any(sep in name for sep in ("/", "\\")):
        raise ValueError(f"invalid deploy name: {raw!r}")
    return name


def build_preset_deployment_plan(
    *,
    game_id: str,
    from_preset_id: str,
    to_preset_id: str,
    mods: list[Mod],
    target_mod_ids: set[str],
    deployments: list[DeploymentRecord],
    deploy_root: str | Path,
) -> PresetDeploymentPlan:
    """按独立身份规划，保留部署被外部改名后的实际路径。"""
    root = Path(deploy_root)
    if not str(deploy_root).strip():
        raise ValueError("deployment root is not configured")
    by_mod = {mod.mod_id: mod for mod in mods}
    missing = sorted(target_mod_ids - set(by_mod))
    if missing:
        raise ValueError(f"preset references missing mods: {', '.join(missing)}")
    records = {record.mod_id: record for record in deployments}
    identities = {}
    for mod in mods:
        validate_mod_id(mod.mod_id)
        if mod.mod_id in identities:
            raise IdentityConflict(f"duplicate mod_id: {mod.mod_id}")
        identities[mod.mod_id] = mod

    paths = {}
    relevant = {}
    for identity, mod in identities.items():
        record = records.get(identity)
        state = record.state if record else DeploymentState.ABSENT
        name = _safe_name(mod.folder_name or Path(mod.path).stem)
        actual = Path(record.actual_path) if record is not None and record.actual_path else None
        parked_name = None
        if actual is not None and state == DeploymentState.DISABLED:
            from src.core.mod_storage import ModStorage

            parked_name = _safe_name(ModStorage.active_name_from_parked(actual, identity))
        if mod.mod_id not in target_mod_ids and state != DeploymentState.ACTIVE:
            continue
        relevant[identity] = mod
        active = root / name
        disabled = root / f"{MIGOTO_DISABLED_PREFIX}_{identity[:12]}_{name}"
        if actual is not None:
            if state == DeploymentState.ACTIVE:
                active = actual
                disabled = actual.with_name(f"{MIGOTO_DISABLED_PREFIX}_{identity[:12]}_{actual.name}")
            elif state == DeploymentState.DISABLED:
                disabled = actual
                active = actual.parent / parked_name
        paths[identity] = [active, disabled, state]

    # 优先保留选中的活动路径，同名但身份不同的选择在写入前分配不同目标。
    occupied = {}
    selected = [mod for mod in mods if mod.mod_id in target_mod_ids]
    selected.sort(key=lambda mod: (paths[mod.mod_id][2] != DeploymentState.ACTIVE, mod.mod_id))
    for mod in selected:
        identity = mod.mod_id
        active, disabled, state = paths[identity]
        owner = occupied.get(_path_key(active))
        if owner is not None and owner != identity:
            if state == DeploymentState.ACTIVE:
                raise IdentityConflict(f"active paths share different identities: {active}")
            base = active.name[:180]
            for candidate in (f"{base}_{identity[:12]}", f"{base}_{identity}"):
                target = active.with_name(candidate)
                if _path_key(target) not in occupied:
                    active = target
                    break
            else:
                raise IdentityConflict(f"cannot reserve deployment path: {active}")
            if state == DeploymentState.ABSENT:
                disabled = active.with_name(f"{MIGOTO_DISABLED_PREFIX}_{identity[:12]}_{active.name}")
            paths[identity] = [active, disabled, state]
        occupied[_path_key(active)] = identity

    actions = []
    for identity, mod in relevant.items():
        active, disabled, state = paths[identity]
        wanted = mod.mod_id in target_mod_ids
        currently_active = state == DeploymentState.ACTIVE
        if not wanted and not currently_active:
            continue
        if currently_active:
            kind = "noop" if wanted else "park"
        elif wanted and state == DeploymentState.DISABLED:
            kind = "restore"
        elif wanted and state == DeploymentState.ABSENT:
            kind = "materialize_active"
        else:
            raise ValueError(f"deployment {identity} cannot transition from {state.value}")
        actions.append(
            DeploymentAction(
                mod_id=identity,
                source_path=mod.path,
                active_path=str(active),
                disabled_path=str(disabled),
                partial_path=str(disabled.with_name(disabled.name + ".partial")),
                action=kind,
            )
        )
    order = {"park": 0, "restore": 1, "materialize_active": 2, "noop": 3}
    actions.sort(key=lambda action: (order[action.action], action.mod_id))
    return PresetDeploymentPlan(uuid.uuid4().hex, game_id, from_preset_id, to_preset_id, tuple(actions))


def validate_plan_filesystem(plan: PresetDeploymentPlan) -> None:
    """创建日志或执行修改前检查全部路径。"""
    if not plan.actions:
        return
    root = Path(plan.actions[0].active_path).parent
    if not root.is_dir():
        raise OSError(f"deployment root is offline: {root}")
    # 停放动作先释放活动路径，后续恢复和部署可使用同名位置；
    # 该计划内交接不属于外部目录冲突。
    paths_vacated_by_plan = {_path_key(action.active_path) for action in plan.actions if action.action == "park"}
    for action in plan.actions:
        from src.core.operation_preflight import confirm_source

        confirm_source(action.source_path, action.mod_id, full=False)
        active = Path(action.active_path)
        disabled = Path(action.disabled_path)
        partial = Path(action.partial_path)
        active_key = _path_key(active)
        if partial.exists() or os.path.lexists(str(partial)):
            raise FileExistsError(f"partial deployment exists: {partial}")
        active_exists = os.path.lexists(str(active))
        disabled_exists = os.path.lexists(str(disabled))
        owned = active if action.action in ("park", "noop") else disabled if action.action == "restore" else None
        if owned is not None and owned.exists():
            marker = read_deployment_identity(owned)
            if marker is None or marker["mod_id"] != action.mod_id:
                raise IdentityConflict(f"deployment identity mismatch: {owned}")
        if (
            active_exists
            and disabled_exists
            and not (action.action == "restore" and active_key in paths_vacated_by_plan)
        ):
            raise FileExistsError(f"active and disabled deployments both exist: {active}; {disabled}")
        if action.action == "park" and (not active_exists or disabled_exists):
            raise FileNotFoundError(f"invalid park layout: {active} -> {disabled}")
        if action.action == "restore" and (
            (active_exists and active_key not in paths_vacated_by_plan) or not disabled_exists
        ):
            raise FileNotFoundError(f"invalid restore layout: {disabled} -> {active}")
        if action.action == "materialize_active":
            if (active_exists and active_key not in paths_vacated_by_plan) or disabled_exists:
                raise FileExistsError(f"deployment target already exists: {active}")
            if not Path(action.source_path).exists():
                raise FileNotFoundError(action.source_path)
        if action.action == "noop" and not active_exists:
            raise FileNotFoundError(f"active deployment is missing: {active}")
