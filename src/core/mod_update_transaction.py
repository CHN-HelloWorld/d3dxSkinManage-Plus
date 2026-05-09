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


def _make_update_plan(manager, mid: str, archive, metadata) -> dict:
    """采集不可变文件输入，不向子进程传递数据库对象。"""
    mod = manager.get_mod(mid)
    if mod is None or not manager.current_game_id:
        raise UpdateContextChanged(tr("找不到待更新的 Mod"))
    _check_ownership(manager, mid, manager.current_game_id)
    old = Path(mod.path).absolute()
    if not fs.is_file(old) or not is_resident_archive(old):
        raise ValueError(tr("Mod 本体不存在或不是支持的归档"))
    token = uuid.uuid4().hex
    container = manager._index.paths.container_path(mid)
    root = container / ".d3dxmeta" / "update_work" / token
    target_format, strict = get_import_settings(manager._config)
    deploy = None
    if mod.is_enabled and mod.deployed_path:
        deploy = Path(mod.deployed_path)
    elif manager._is_parked_deployment_available(mod):
        deploy = Path(mod.deployed_path)
    plan = {
        "job_id": token,
        "game_id": manager.current_game_id,
        "mod_id": mid,
        "old_path": str(old),
        "old_signature": file_signature(old),
        "old_hash": mod.content_hash,
        "old_enabled": mod.is_enabled,
        "imported_at": mod.imported_at,
        "old_deployed_path": mod.deployed_path,
        "container": str(container),
        "folder_name": mod.folder_name,
        "archive": str(Path(archive).absolute()),
        "metadata": metadata.to_dict() if hasattr(metadata, "to_dict") else dict(metadata),
        "target_format": target_format,
        "strict_format": strict,
        "work_root": str(root),
        "deploy_target": str(deploy.absolute()) if deploy is not None else "",
        "deploy_work": str(deploy.parent.absolute() / f"DISABLED_.update_{token}.partial") if deploy else "",
    }
    owned_update_paths(plan)
    fs.ensure_dir(root, exist_ok=False)
    return plan


def owned_update_paths(plan: dict) -> list[Path]:
    """对照固定父目录和名称校验每个可递归删除的路径。"""
    token = str(plan.get("job_id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError(tr("无效的更新任务标识"))
    old = Path(plan["old_path"]).absolute()
    container = Path(plan["container"]).absolute()
    if not old.is_relative_to(container):
        raise ValueError(tr("更新本体不属于容器"))
    expected = {
        "work_root": container / ".d3dxmeta" / "update_work" / token,
    }
    if plan.get("deploy_target"):
        expected["deploy_work"] = Path(plan["deploy_target"]).absolute().parent / f"DISABLED_.update_{token}.partial"
    result = []
    for key, target in expected.items():
        candidate = Path(plan[key]).absolute()
        # 路径解析期间父目录出现时可能保留 Windows 长路径前缀；
        # 仅规范化该前缀，仍拒绝工作目录被链接重定向。
        if (
            candidate != target
            or Path(fs.normal_path(candidate.resolve())) != Path(fs.normal_path(target.parent.resolve())) / target.name
        ):
            raise ValueError(tr("更新暂存路径越界：{key}", key=key))
        result.append(candidate)
    return result


def cleanup_update(plan: dict) -> None:
    """仅在子进程退出且提交或回滚完成后调用。"""
    paths = owned_update_paths(plan)
    # 回滚失败不能作为删除保留原件的依据。
    if fs.exists(Path(plan["work_root"]) / "recovery_required"):
        logger.error("更新需要恢复，保留暂存及旧内容：%s", plan["work_root"])
        return
    for path in reversed(paths):
        if fs.exists(path):
            if path.is_symlink():
                fs.unlink(path)
            else:
                fs.rmtree(path, ignore_errors=False)


def _inside(path: str | Path, root: Path) -> Path:
    """确认路径解析后仍位于任务暂存根内，越界时抛出 ValueError。"""
    path = Path(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(tr("更新结果不在本任务暂存目录中"))
    return path


def _variant_append_moves(current, staged: Path, payloads) -> list[tuple[Path, Path]]:
    """验证待追加变体文件及本地身份、名称和目标占用，返回无冲突的文件移动对。"""
    if not isinstance(payloads, list):
        raise ValueError(tr("更新变体清单格式无效"))
    variants = tuple(ModVariant.from_dict(payload) for payload in payloads)
    verify_variant_files(staged, variants)
    if not variants:
        return []
    live_store = ModVariantStore(current.path)
    existing = live_store.list()
    ids = {variant.variant_id.casefold() for variant in existing}
    names = {variant.name.casefold() for variant in existing}
    staged_store = ModVariantStore(staged)
    moves = []
    for variant in variants:
        if variant.variant_id.casefold() in ids or variant.name.casefold() in names:
            raise UpdateContextChanged(tr("本地变体在更新准备后出现身份或名称冲突，请重试更新"))
        target = live_store.path_for(variant.variant_id)
        if os.path.lexists(fs.native_path(target)):
            raise UpdateContextChanged(tr("导入变体的目标文件已被占用，请重试更新"))
        moves.append((staged_store.path_for(variant.variant_id), target))
        ids.add(variant.variant_id.casefold())
        names.add(variant.name.casefold())
    return moves


def commit_update(manager, plan: dict, prepared: dict):
    """持有目标操作保护和变体存储锁，串行提交已准备的更新结果。"""
    with _update_scope(manager, plan["mod_id"]), VARIANT_STORE_LOCK:
        return _commit_update(manager, plan, prepared)


def _commit_update(manager, plan: dict, prepared: dict):
    """仅执行原子改名和元数据写入，大量文件读写已由子进程完成。"""
    owned_update_paths(plan)
    if manager.current_game_id != plan["game_id"]:
        raise UpdateContextChanged(tr("请切回原游戏后继续更新，下载文件已保留"))
    _check_ownership(manager, plan["mod_id"], plan["game_id"])
    current = manager.get_mod(plan["mod_id"])
    if (
        current is None
        or Path(current.path).absolute() != Path(plan["old_path"])
        or current.content_hash != plan["old_hash"]
        or current.is_enabled != plan["old_enabled"]
        or current.mod_id != plan["mod_id"]
        or current.imported_at != plan["imported_at"]
        or current.deployed_path != plan["old_deployed_path"]
        or file_signature(current.path) != plan["old_signature"]
    ):
        raise UpdateContextChanged(tr("Mod 在更新期间发生变化，请重试"))
    if override_signature(current.path) != prepared["source_overrides"]:
        raise UpdateContextChanged(tr("Mod 配置在更新期间发生变化，请重试"))
    if deployment_ini_signature(plan["deploy_target"]) != prepared["source_deployment"]:
        raise UpdateContextChanged(tr("部署中的 Mod 配置在更新期间发生变化，请重试"))
    root = Path(plan["work_root"])
    staged = _inside(prepared["body"], root)
    if not fs.is_file(staged) or not is_resident_archive(staged):
        raise OSError(tr("更新文件尚未准备完整"))
    variant_moves = _variant_append_moves(current, staged, prepared.get("variants", []))
    staged_deploy = None
    try:
        if read_source_mod_id(current.path) != current.mod_id:
            raise IdentityConflict(tr("源 Mod 身份发生变化"))
        if read_source_mod_id(staged) != current.mod_id:
            raise IdentityConflict(tr("暂存源 Mod 身份发生变化"))
        if plan["deploy_target"]:
            staged_deploy = _inside(prepared["deployment"], Path(plan["deploy_work"]))
            for directory in (Path(plan["deploy_target"]), staged_deploy):
                identity = read_deployment_identity(directory)
                if identity is None or identity["mod_id"] != current.mod_id:
                    raise IdentityConflict(tr("部署身份发生变化：{directory}", directory=directory))
    except (IdentityConflict, OSError) as error:
        raise UpdateContextChanged(tr("更新身份校验失败，旧文件已保留：{error}", error=error)) from error
    old = Path(current.path)
    container = manager._index.paths.container_path(current.mod_id)
    destination = container / f"{current.folder_name or old.stem}{staged.suffix.lower()}"
    if destination != old and os.path.lexists(fs.native_path(destination)):
        raise FileExistsError(tr("更新目标已存在：{name}", name=destination.name))
    marker = container / MOD_CONTAINER_MARKER
    rollback = root / "rollback"
    fs.ensure_dir(rollback)
    moves: list[tuple[Path, Path]] = []

    def move(source: Path, target: Path) -> None:
        """确保目标父目录存在后移动文件，并登记逆向恢复所需的路径对。"""
        fs.ensure_dir(target.parent)
        os.replace(fs.native_path(source), fs.native_path(target))
        moves.append((source, target))

    sync = getattr(manager, "_ini_sync", None)
    previous_pairs = sync.snapshot_mod(current.mod_id) if sync is not None else []
    if sync is not None:
        sync.unregister_mod(current.mod_id)
    updated = deepcopy(current)
    try:
        move(old, rollback / old.name)
        move(staged, destination)
        if fs.exists(marker):
            move(marker, rollback / MOD_CONTAINER_MARKER)
            move(staged.parent / MOD_CONTAINER_MARKER, marker)
        overrides = resolve_ini_overrides_dir(destination)
        staged_overrides = staged.parent / ".d3dxmeta" / "ini_overrides"
        if overrides is not None and fs.exists(staged_overrides):
            if fs.exists(overrides):
                move(overrides, rollback / "ini_overrides")
            move(staged_overrides, overrides)
        if plan["deploy_target"]:
            target = Path(plan["deploy_target"])
            work = Path(plan["deploy_work"])
            if not fs.is_dir(staged_deploy):
                raise OSError(tr("更新部署尚未准备完整"))
            if os.path.lexists(fs.native_path(target)):
                move(target, work / "old_deployment")
            move(staged_deploy, target)
            updated.remember_deployment(target)
        for source, target in variant_moves:
            if os.path.lexists(fs.native_path(target)):
                raise UpdateContextChanged(tr("导入变体的目标文件已被占用，请重试更新"))
            move(source, target)
        updated.path = str(destination)
        updated.content_hash = prepared["content_hash"]
        updated.info.custom_data.setdefault("gamebanana", {}).update(plan["metadata"])
        if plan["metadata"].get("version"):
            updated.info.version = plan["metadata"]["version"]
        if not manager._commit_mod_metadata(updated):
            raise OSError(tr("保存更新索引失败"))
    except Exception:
        errors = []
        for source, target in reversed(moves):
            try:
                os.replace(fs.native_path(target), fs.native_path(source))
            except OSError as error:
                errors.append(str(error))
        if errors:
            fs.write_text(root / "recovery_required", "\n".join(errors), encoding="utf-8")
            logger.error("更新回退遇到错误，旧文件保留在 %s：%s", root, errors)
        if current.is_enabled and current.deployed_path:
            manager._register_ini_sync(
                current.mod_id, current.path, current.deployed_path, (current.mod_id,), prepared_pairs=previous_pairs
            )
        raise
    if updated.is_enabled and updated.deployed_path:
        manager._register_ini_sync(
            updated.mod_id, updated.path, updated.deployed_path, (updated.mod_id,), prepared_pairs=prepared["ini_pairs"]
        )
    return updated
