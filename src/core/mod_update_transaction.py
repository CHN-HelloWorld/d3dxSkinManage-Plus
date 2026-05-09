"""在主线程规划更新，并通过短事务提交文件与数据库。"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from src.core.import_format import get_import_settings
from src.core.mod_identity import IdentityConflict, read_deployment_identity, read_source_mod_id
from src.core.mod_package import resolve_ini_overrides_dir
from src.core.mod_variant_package import verify_variant_files
from src.core.mod_variant_store import VARIANT_STORE_LOCK, ModVariantStore
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import MOD_CONTAINER_MARKER, is_resident_archive
from src.utils.i18n import t as tr
from src.utils.logger import logger


class UpdateContextChanged(RuntimeError):
    """准备结果已不属于当前活动 Mod 或游戏。"""


def file_signature(path: str | Path) -> list[int]:
    """读取文件大小、纳秒修改时间及文件编号，生成更新一致性签名。"""
    stat = os.stat(fs.native_path(path))
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def override_signature(body: str | Path) -> list[list]:
    """按相对路径收集全部覆盖文件的签名，覆盖目录缺失时返回空列表。"""
    root = resolve_ini_overrides_dir(body)
    if root is None or not fs.exists(root):
        return []
    return sorted(
        [
            [str(Path(fs.normal_path(path)).relative_to(root)), *file_signature(path)]
            for path in fs.iter_files(root)
            if fs.is_file(path)
        ]
    )


def deployment_ini_signature(deploy: str | Path) -> list[list]:
    """识别尚未同步到源覆盖文件的编辑结果。"""
    if not deploy or not fs.is_dir(deploy):
        return []
    root = Path(deploy)
    entries = [[".", *file_signature(root)]]
    for name in fs.iter_files(root):
        file = Path(fs.normal_path(name))
        relative = file.relative_to(root)
        if file.suffix.lower() == ".ini" and ".d3dxmeta" not in relative.parts:
            entries.append([relative.as_posix(), *file_signature(file)])
    return sorted(entries)


def _check_ownership(manager, mid: str, game_id: str):
    """核对游戏归属、预设事务及源部署身份，发现上下文变化时拒绝更新。"""
    index = manager._index
    if index.get_mod_game_id(mid) != game_id:
        raise UpdateContextChanged(tr("Mod 已不属于当前游戏，请切回原游戏后重试"))
    if manager.preset_manager.has_pending_apply(game_id):
        raise UpdateContextChanged(tr("预设正在切换，请完成后重试更新"))
    from src.core.operation_preflight import OperationPreflightError

    try:
        manager.require_mod_operation(mid, metadata=True, deployment=True)
    except OperationPreflightError as error:
        manager.report_operation_failure(error)
        raise UpdateContextChanged(str(error)) from error
    # 唯一索引防止重复身份和位置，只验证当前容器与标记，不读取无关记录。
    source = index.paths.body_path(mid)
    if index.paths.locate(str(source)) != ("mod", mid):
        raise UpdateContextChanged(tr("Mod 位置归属已变化，旧版已保留"))
    if read_source_mod_id(source) != mid:
        raise UpdateContextChanged(tr("Mod 源身份已变化，旧版已保留"))


def _update_scope(manager, mid: str):
    """为目标 Mod 的源与部署创建操作保护上下文，无保护注册表时返回空上下文。"""
    registry = getattr(manager, "_library_protection", None)
    if registry is None:
        return nullcontext()
    mod = manager.get_mod(mid)
    paths = (Path(mod.path).parent, mod.deployed_path) if mod is not None else ()
    # 短期令牌独立释放，不影响工作线程的异步所有权令牌。
    return registry.operation("update-command:" + uuid.uuid4().hex, paths=paths, mod_ids=(mid,))


def make_update_plan(manager, mid: str, archive, metadata) -> dict:
    """在目标 Mod 操作保护范围内采集并生成更新计划。"""
    with _update_scope(manager, mid):
        return _make_update_plan(manager, mid, archive, metadata)
