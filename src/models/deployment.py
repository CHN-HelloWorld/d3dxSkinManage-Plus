"""部署状态和预设应用事务的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DeploymentState(Enum):
    """部署副本从生成到启停、移除或失败的可观测状态。"""

    ABSENT = "absent"
    MATERIALIZING = "materializing"
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass(frozen=True)
class DeploymentRecord:
    """记录单个 Mod 的部署状态及实际副本路径，不包含源文件位置。"""

    mod_id: str
    state: DeploymentState = DeploymentState.ABSENT
    actual_path: str = ""


@dataclass(frozen=True)
class PresetApplyJournal:
    """保存预设切换计划、已完成步骤和运行时补丁，供中断恢复使用。"""

    operation_id: str
    game_id: str
    from_preset_id: str
    to_preset_id: str
    phase: str
    plan_json: str
    completed_steps_json: str = "[]"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    #: 单 Mod 变体的运行时补丁账本（JSON）。整份 ini 原文快照 + d3dx_user.ini 写前/写后
    #: 哈希，供切换中断后反向回滚。结构见 :class:`~src.core.variant_journal.RuntimePatch`。
    #: 取粗颗粒度（整份快照而非逐键旧值）是刻意的：回滚路径最容易写错，用"覆盖写"换确定性。
    runtime_patch_json: str = "{}"
