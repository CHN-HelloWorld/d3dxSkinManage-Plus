"""根据磁盘上的持久身份刷新可重建的部署索引。"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.mod_identity import validate_mod_id
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import Mod
from src.utils.logger import logger


@dataclass(frozen=True)
class DeploymentChanges:
    """汇总本次磁盘对账改变的 Mod 对象以及由启用转为禁用的成员编号。"""

    changed: tuple[Mod, ...] = ()
    disabled_ids: tuple[str, ...] = ()


def reconcile_deployment_records(
    index, storage, game_id: str, *, force=False, rows=None, locations=None, records=None
) -> DeploymentChanges:
    """按磁盘部署身份核对索引，事务写入状态变化并返回受影响的 Mod。

    可复用调用方采集的行、位置和部署记录；无部署根时直接返回，提交失败时保留异常并恢复索引游戏上下文。"""
    if storage.get_deploy_dir() is None:
        return DeploymentChanges()
    rows = index.get_library_rows(game_id) if rows is None else rows
    if locations is None:
        locations = storage.scan_deployments((row.deployed_path for row in rows if row.deployed_path), force=force)
    if records is None:
        records = {record.mod_id: record for record in index.get_deployments(game_id)}
    changed, disabled_ids, updates = [], [], []
    for row in rows:
        location = locations.get(validate_mod_id(row.mod_id))
        new_path = str(location.path) if location else ""
        enabled = bool(location and location.active)
        record = records.get(row.mod_id)
        state = DeploymentState.ACTIVE if enabled else DeploymentState.DISABLED if new_path else DeploymentState.ABSENT
        if (
            row.deployed_path == new_path
            and row.enabled == enabled
            and record is not None
            and record.actual_path == new_path
            and record.state == state
        ):
            continue
        mod = index.get_mod(row.mod_id)
        if mod is None or mod.mod_id != row.mod_id or mod.path != row.path:
            continue
        was_enabled = mod.is_enabled
        mod.deployed_path = new_path
        mod.enable() if enabled else mod.disable()
        updates.append(DeploymentRecord(mod_id=mod.mod_id, state=state, actual_path=new_path))
        changed.append(mod)
        if was_enabled and not enabled:
            disabled_ids.append(mod.mod_id)
    if changed:
        previous_game = index.game_id
        index.game_id = game_id
        try:
            with index.transaction():
                for record in updates:
                    index.upsert_deployment(record, commit=False)
        except Exception as error:
            logger.error(
                "部署状态核对提交失败 game=%s mods=%s paths=%s: %s",
                game_id,
                [record.mod_id for record in updates],
                [record.actual_path for record in updates],
                error,
            )
            raise
        finally:
            index.game_id = previous_game
        for record in updates:
            records[record.mod_id] = record
    return DeploymentChanges(tuple(changed), tuple(disabled_ids))
