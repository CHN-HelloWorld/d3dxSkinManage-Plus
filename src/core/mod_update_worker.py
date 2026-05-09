"""可暂停的独立文件准备流程，不接收界面或数据库对象。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from src.utils.i18n import t as tr


def _atomic_json(path: Path, value: dict) -> None:
    """先写临时 JSON 再原子替换目标，短暂权限冲突在限定时间内重试。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # Windows 上父进程短暂读取可能阻止替换，仅在可控工作进程中重试。
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _capture_deployed_overrides(old: Path, staged: Path, deploy: Path) -> None:
    """将游戏新写入的配置保留到暂存目录，不修改活动文件。"""
    from src.core.mod_package import iter_effective_inis, override_path_for, write_override
    from src.utils import fs

    if not fs.is_dir(deploy):
        return
    effective = {
        rel.replace("\\", "/").casefold(): text.encode("utf-8", errors="surrogateescape")
        for rel, text in iter_effective_inis(str(old))
    }
    for file in fs.iter_files(deploy):
        file = Path(fs.normal_path(file))
        if file.suffix.lower() != ".ini" or ".d3dxmeta" in file.relative_to(deploy).parts:
            continue
        rel = file.relative_to(deploy).as_posix()
        content = fs.read_bytes(file)
        if effective.get(rel.casefold()) == content:
            continue
        reference = override_path_for(old, rel)
        reference = reference if reference is not None and fs.exists(reference) else old
        if os.stat(fs.native_path(reference)).st_mtime_ns > os.stat(fs.native_path(file)).st_mtime_ns:
            continue
        if not write_override(staged, rel, content.decode("utf-8", errors="surrogateescape")):
            raise OSError(tr("无法保留部署中的配置：{rel}", rel=rel))


def _link_staged_overrides(body: Path, deployment: Path, future_body: Path) -> None:
    """硬链接随文件改名保留，符号链接指向最终活动位置。"""
    from src.core.mod_package import list_overrides, override_path_for
    from src.utils import fs

    for rel in list_overrides(body):
        source = override_path_for(body, rel)
        # 提交前标记仍记录旧扩展名，目标应由受管容器确定。
        future = future_body.parent / ".d3dxmeta" / "ini_overrides" / rel
        target = deployment / rel
        fs.ensure_dir(target.parent)
        if os.path.lexists(fs.native_path(target)):
            fs.unlink(target)
        try:
            os.link(fs.native_path(source), fs.native_path(target))
        except OSError:
            try:
                os.symlink(fs.native_path(future), fs.native_path(target))
            except OSError:
                fs.copy_path(source, target)


def _prepare_ini_baseline(body: Path) -> list[dict]:
    """为更新后的覆盖 INI 采集摘要和按键标记，任一文件不可读时中止。"""
    from src.core.ini_sync import _has_key_bindings, _hash_file
    from src.core.mod_package import list_overrides, override_path_for

    pairs = []
    for relative in list_overrides(body):
        source = override_path_for(body, relative)
        checksum = _hash_file(source)
        if checksum is None:
            raise OSError(tr("无法读取更新配置：{relative}", relative=relative))
        pairs.append({"rel": relative, "last_hash": checksum, "has_key_bindings": _has_key_bindings(source)})
    return pairs


def prepare_update(plan: dict, report) -> dict:
    """验证更新归档并准备新正文、变体、覆盖配置和可选部署目录。

    保留旧配置及部署中的有效修改，返回提交需要的文件结果与签名；旧正文或配置在准备期间变化时拒绝结果。"""
    from src.core.import_format import materialize_import_body, prepare_import_source, take_reject_reason
    from src.core.mod_package import resolve_ini_overrides_dir, seed_ini_overrides, write_container_marker
    from src.core.mod_storage import ModStorage
    from src.core.mod_update_transaction import (
        deployment_ini_signature,
        file_signature,
        override_signature,
        owned_update_paths,
    )
    from src.core.mod_update_validation import validate_update_archive
    from src.core.mod_variant_package import saved_variants, stage_imported_variants
    from src.models.mod import Mod
    from src.services.archive import ArchiveService
    from src.utils import fs

    owned_update_paths(plan)
    root, old, archive = (Path(plan[key]) for key in ("work_root", "old_path", "archive"))
    if file_signature(old) != plan["old_signature"]:
        raise OSError(tr("原 Mod 文件在更新准备前发生变化"))
    original_overrides = override_signature(old)
    original_deployment = deployment_ini_signature(plan["deploy_target"])
    fs.ensure_dir(root)
    service = ArchiveService()

    def progress(phase: str, fraction=None, detail: str = "") -> None:
        """将阶段、比例、详情及归档名称组合为更新进度快照并报告。"""
        report({"phase": phase, "stage_progress": fraction, "detail": detail, "file_name": archive.name})

    progress("verifying", None, tr("检查更新文件"))
    validate_update_archive(archive, service)
    source = prepare_import_source(
        archive,
        service,
        plan["target_format"],
        plan["strict_format"],
        root,
        extract_progress_cb=lambda value: progress("extracting", value, tr("解压更新文件")),
    )
    if source is None:
        raise OSError(take_reject_reason() or tr("更新归档无法读取或解压"))
    progress("verifying", None, tr("校验 Mod 内容"))
    content_hash = Mod.compute_sha1(str(source.clean_path))
    if not content_hash:
        raise OSError(tr("无法校验更新文件"))
    progress("installing", None, tr("准备库内文件"))
    materialized = materialize_import_body(
        source,
        service,
        progress_cb=lambda value: progress("installing", value, tr("转换存储格式")),
    )
    if materialized is None:
        raise OSError(tr("无法生成更新文件"))
    container = root / "container"
    fs.ensure_dir(container)
    body = container / f"{plan['folder_name'] or old.stem}{Path(materialized).suffix.lower()}"
    fs.copy_path(
        materialized,
        body,
        buffer_size=512 * 1024,
        progress_cb=lambda value: progress("installing", value, tr("写入更新文件")),
    )
    if not write_container_marker(
        container, body.name, content_hash, mod_id=plan["mod_id"], imported_at=plan["imported_at"]
    ):
        raise OSError(tr("无法写入更新容器信息"))
    imported_variants = stage_imported_variants(
        body,
        source.share_variants,
        occupied=saved_variants(old) if source.share_variants else (),
    )
    progress("installing", None, tr("保留自定义配置"))
    existing = resolve_ini_overrides_dir(old)
    if existing is not None and fs.exists(existing):
        fs.copy_path(existing, container / ".d3dxmeta" / "ini_overrides")
    if plan["deploy_target"]:
        _capture_deployed_overrides(old, body, Path(plan["deploy_target"]))
    if not seed_ini_overrides(body):
        raise OSError(tr("无法准备 Mod 配置"))
    deployment = ""
    ini_pairs = []
    if plan["deploy_target"]:
        deploy_work = Path(plan["deploy_work"])
        fs.ensure_dir(deploy_work, exist_ok=False)
        progress("extracting", 0.0, tr("解压新版本部署文件"))
        unpacked = service.extract(
            body,
            deploy_work / "unpacked",
            progress_cb=lambda value: progress("extracting", value, tr("解压新版本部署文件")),
        )
        if unpacked is None:
            raise OSError(tr("新版本部署解压失败"))
        unpacked = Path(unpacked)
        ModStorage()._strip_deployed_meta(unpacked)
        from src.core.mod_identity import write_deployment_identity

        write_deployment_identity(unpacked, plan["mod_id"], content_hash)
        progress("installing", None, tr("连接 Mod 配置"))
        future_body = old.parent / body.name
        _link_staged_overrides(body, unpacked, future_body)
        ini_pairs = _prepare_ini_baseline(body)
        deployment = str(unpacked)
    if file_signature(old) != plan["old_signature"] or override_signature(old) != original_overrides:
        raise OSError(tr("Mod 或配置在更新准备期间发生变化，请重试"))
    if deployment_ini_signature(plan["deploy_target"]) != original_deployment:
        raise OSError(tr("部署中的 Mod 配置在更新准备期间发生变化，请重试"))
    progress("installing", 1.0, tr("文件已准备，正在提交更新"))
    return {
        "body": str(body),
        "content_hash": content_hash,
        "source_overrides": original_overrides,
        "source_deployment": original_deployment,
        "deployment": deployment,
        "ini_pairs": ini_pairs,
        "variants": [variant.to_dict() for variant in imported_variants],
    }


def main(plan_path: str) -> int:
    """等待父进程启动标记，读取计划并执行更新准备，以结果 JSON 和退出码报告成败。"""
    from src.core.mod_update_paths import load_update_plan, store_update_result

    path = Path(plan_path)
    root = path.parent
    # 等待父进程先将整个进程树纳入操作系统作业或进程组。
    while not (root / "start").exists():
        time.sleep(0.01)
    last_at, last_phase = 0.0, ""

    def report(snapshot: dict) -> None:
        """按阶段与时间节流持久化进度，保留阶段边界并附加单调序号。"""
        nonlocal last_at, last_phase
        now = time.monotonic()
        if (
            snapshot["phase"] == last_phase
            and snapshot.get("stage_progress") not in (None, 0.0, 1.0)
            and now - last_at < 0.05
        ):
            return
        last_at, last_phase = now, snapshot["phase"]
        _atomic_json(root / "progress.json", {**snapshot, "sequence": time.monotonic_ns()})

    try:
        plan = load_update_plan(json.loads(path.read_text(encoding="utf-8")), root)
        from src.utils.i18n import set_language

        set_language(plan.get("language", "zh-CN"))
        prepared = prepare_update(plan, report)
        _atomic_json(root / "result.json", {"ok": True, "result": store_update_result(prepared, root)})
        return 0
    except Exception as error:
        _atomic_json(root / "result.json", {"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
