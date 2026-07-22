from __future__ import annotations

import pytest

from src.core.library_snapshot import library_mutation
from src.core.mod_manager import ModManager
from src.utils.logger import logger


@pytest.fixture(autouse=True)
def registered_games(temp_data_dir):
    """为资源库测试显式注册游戏及其根目录。"""
    register_games("game_a", "game_b", "g", "first", "second", "other", "Game")
    yield
    reset_game_service()


def make_mod(index, name="Skin", *, mod_id=None, node_id="", body_name="body.zip", **fields):
    """在索引配置的位置创建符合当前字段约束的记录。"""
    from src.core.mod_identity import new_mod_id
    from src.models.mod import Mod

    game_id = fields.pop("game_id", index.game_id)
    parent = index.paths.node_path(node_id) if node_id else index.paths.roots[game_id]
    values = dict(
        game_id=game_id,
        mod_id=mod_id or new_mod_id(),
        node_id=node_id,
        name=name,
        folder_name=name,
        path=str(parent / name / body_name),
        content_hash="a" * 40,
        imported_at="2026-10-05T10:00:00",
    )
    values.update(fields)
    return Mod(**values)


def reset_game_service() -> None:
    """重置 GameService 和 GameConfigStore 单例（`temp_data_dir` 只清后者）。"""
    from src.core import game_config_store as game_store_mod
    from src.core import game_service as game_service_mod

    game_service_mod.GameService._instance = None
    game_store_mod.GameConfigStore._instance = None


def register_games(*game_ids: str) -> None:
    """通过完整游戏配置注册测试游戏，资源目录必须非空。

    调用方清理时须再次重置游戏服务。
    """
    from src.core.game_config_store import GameConfigStore

    reset_game_service()
    GameConfigStore().set_games(
        [{"id": gid, "name": gid, "name_en": gid, "mods_path": "", "resource_dir": gid} for gid in game_ids]
    )


def scan_and_sync(manager: ModManager) -> int:
    """扫描当前游戏 library、逐项纳管新增成员，返回成功登记数。"""
    from src.core.mod_intake import (
        DuplicateIntakeError,
        IntakeError,
        ModIntakeService,
    )

    service = ModIntakeService(manager)
    count = 0
    for request in manager.plan_scan_intakes():
        try:
            service.commit(service.prepare(request))
            count += 1
        except DuplicateIntakeError:
            continue
        except (IntakeError, InterruptedError):
            continue
    return count


@library_mutation
def apply_preset(manager, preset_id: str) -> list[str]:
    """同步调用预设事务原语，供测试构建和验证操作状态。"""
    try:
        plan = manager.prepare_preset_apply(preset_id)
        completed = execute_preset_plan(manager, plan)
        return manager.commit_preset_apply(plan, completed)
    except Exception as exc:
        if "plan" in locals():
            if "completed" in locals():
                from src.core.preset_apply_service import PresetApplyError, PresetPlanExecutor

                errors = manager._reverse_runtime_patch(plan)
                if not errors:
                    errors = PresetPlanExecutor(manager._get_storage()).rollback(completed)
                exc = PresetApplyError(str(exc), completed=completed, rollback_errors=errors)
            manager.fail_preset_apply(plan, exc)
        logger.error("预设应用失败: %s", exc)
        return []


def execute_preset_plan(manager, plan, *, should_cancel=None, progress=None):
    """同步执行文件步骤，并使用现行执行器记录测试事务进度。"""
    from src.core.preset_apply_service import PresetPlanExecutor

    manager.mark_preset_apply_executing(plan.operation_id)
    return PresetPlanExecutor(manager._get_storage()).execute(
        plan,
        should_cancel=should_cancel,
        progress=progress,
        step_completed=lambda steps: manager.record_preset_apply_steps(plan.operation_id, steps),
    )
