"""执行插件分类创建及导入，文件准备与索引提交遵守宿主线程边界。"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path

from PIL import Image

from src.core.import_cleanup import ImportCleanupPending
from src.core.mod_intake import DuplicateIntakeError, ModIntakeService
from src.core.mod_package import build_local_manifest, local_meta_from_mod, read_sidecar, write_sidecar
from src.core.preview_cache_index import capture_source_version
from src.models.mod import AgeRating
from src.plugin_sdk.imports import ModImportRequest, directory_fingerprint
from src.utils import fs
from src.utils.constants import ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS
from src.utils.i18n import t as tr
from src.utils.paths import get_data_dir


def _check_cancel(worker) -> None:
    """在可恢复文件阶段响应宿主取消请求。"""
    if worker.isInterruptionRequested():
        raise InterruptedError(tr("操作已取消"))


def _check_source_digest(source: Path, expected: str, worker) -> None:
    """核对可选的原文件字节摘要，避免将旧索引身份误用为 Plus 内容指纹。"""
    if not expected:
        return
    digest = hashlib.sha1()
    with open(fs.native_path(source), "rb") as stream:
        while True:
            _check_cancel(worker)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(tr("原文件校验失败，请重新扫描：{name}", name=source.name))


def _archive_suffix(source: Path) -> str:
    """从文件头识别无扩展名归档，只接受实际支持的三类容器。"""
    with open(fs.native_path(source), "rb") as stream:
        header = stream.read(8)
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ".zip"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return ".rar"
    raise ValueError(tr("无法识别 Mod 归档格式：{name}", name=source.name))


def _apply_import_metadata(prepared, spec: ModImportRequest, worker) -> None:
    """在新容器提交前保存字段和独立预览，保留随包的其余元数据。"""
    mod = prepared.mod
    for source_name, target_name in (("author", "author"), ("description", "description"), ("source_url", "url")):
        value = getattr(spec, source_name)
        if value is not None:
            setattr(mod.info, target_name, value)
    if spec.rating is not None:
        mod.info.age_rating = AgeRating(spec.rating)
    if spec.tags is not None:
        mod.info.tags = list(spec.tags)
    manifest = read_sidecar(mod.path) or {}
    local = dict(manifest.get("local") or {})
    references, previews = [], []

    def preview_digest(path):
        """以流式字节摘要排除重复预览，避免一次读取整个图片文件。"""
        digest = hashlib.sha256()
        with open(fs.native_path(path), "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                _check_cancel(worker)
                digest.update(chunk)
        return digest.digest()

    existing_previews, existing_references = [], []
    seen = set()
    if spec.expected_directory_hash:
        local["preview_selection_locked"] = True
    else:
        for path, reference in zip(mod.preview_images, local.get("preview_references") or []):
            _check_cancel(worker)
            digest = preview_digest(path)
            if digest not in seen:
                seen.add(digest)
                existing_previews.append(path)
                existing_references.append(reference)
    for path in spec.preview_paths:
        _check_cancel(worker)
        source = Path(path)
        if source.suffix.lower() not in IMAGE_EXTENSIONS or not fs.is_file(source):
            raise ValueError(tr("预览图不存在或格式不受支持：{path}", path=path))
        version = capture_source_version(source)
        with Image.open(fs.native_path(source)) as image:
            image.verify()
        digest = preview_digest(source)
        if capture_source_version(source) != version:
            raise ValueError(tr("预览图在导入期间发生变化：{path}", path=path))
        if digest in seen:
            continue
        target = prepared.container_path / ".d3dxmeta" / "previews" / (uuid.uuid4().hex + source.suffix.lower())
        fs.copy_path(source, target, should_cancel=worker.isInterruptionRequested)
        if capture_source_version(source) != version:
            raise ValueError(tr("预览图在导入期间发生变化：{path}", path=path))
        references.append({"scope": "container", "path": target.relative_to(prepared.container_path).as_posix()})
        previews.append(str(target))
        seen.add(digest)
    local["preview_references"] = references + existing_references
    mod.preview_images = previews + existing_previews
    prepared.preview_paths = [Path(path) for path in mod.preview_images]
    _check_cancel(worker)
    if not write_sidecar(mod.path, build_local_manifest(local_meta_from_mod(mod), local, extra=manifest)):
        raise OSError(tr("无法保存导入的 Mod 信息"))


class PluginLibraryOperations:
    """复用宿主导入事务，为插件执行分类和资源库写操作。"""

    def __init__(self, service, manager) -> None:
        """保存串行调度器和库管理器，不创建额外线程或数据库连接。"""
        self._service = service
        self._manager = manager

    def start(self, request) -> None:
        """在宿主线程校验分类和请求，并启动可取消的文件准备。"""
        if request.operation == "create_category":
            self._create_category(request)
            return
        spec = request.args["request"]
        if not isinstance(spec, ModImportRequest):
            raise TypeError("request must be a ModImportRequest")
        intake = ModIntakeService(self._manager)
        plan = intake.plan_request(
            source_path=spec.source_path,
            game_id=request.game_id,
            node_id=spec.node_id,
            display_name=spec.name,
            move_source=False,
            planning_snapshot=intake.planning_snapshot(request.game_id),
            recoverable_cleanup=True,
        )
        request.state["intake"] = intake
        request.kind = "import_mod"
        self._service.started.emit(request.operation, ())

        def prepare(worker):
            """只在工作线程读取源文件、复制归档和生成待提交容器。"""
            worker.input_stage = None
            worker.input_cleanup_token = ""
            source = plan.source_path
            prepared = None
            recovery = self._manager._import_cleanup_recovery
            try:
                _check_cancel(worker)
                recovery.retry_pending(request.game_id, plan.existing_mod_ids)
                _check_cancel(worker)
                version = capture_source_version(source)
                if (
                    spec.expected_directory_hash
                    and directory_fingerprint(str(source), worker.isInterruptionRequested)
                    != spec.expected_directory_hash
                ):
                    raise ValueError(tr("Mod 来源已变化，请重新扫描"))
                _check_source_digest(source, spec.expected_sha1, worker)
                actual_plan = replace(plan, source_version=version)
                if fs.is_file(source) and source.suffix.lower() not in ARCHIVE_EXTENSIONS:
                    suffix = _archive_suffix(source)
                    # 旧库以无扩展名 SHA 存包。复制到独立暂存文件后复用归档服务，源路径不改写。
                    stage = get_data_dir() / "temp" / ("plugin-import-" + uuid.uuid4().hex)
                    fs.ensure_dir(stage, exist_ok=False)
                    worker.input_stage = stage
                    staged_source = stage / ("source" + suffix)
                    worker.input_cleanup_token = recovery.begin(
                        game_id=request.game_id,
                        root=stage.parent,
                        container=stage,
                        body_name=staged_source.name,
                        mod_id=uuid.uuid4().hex,
                        content_hash="",
                        location_kind="staging",
                    )
                    fs.copy_path(source, staged_source, should_cancel=worker.isInterruptionRequested)
                    recovery.prepared(worker.input_cleanup_token)
                    actual_plan = replace(plan, source_path=staged_source, source_version=None)
                if capture_source_version(source) != version:
                    raise ValueError(tr("Mod 来源已变化，请重新扫描"))
                prepared = intake.prepare(
                    actual_plan,
                    should_cancel=worker.isInterruptionRequested,
                    progress=lambda fraction, _text: worker.progress.emit("", float(fraction or 0)),
                )
                _apply_import_metadata(prepared, spec, worker)
                if (
                    spec.expected_directory_hash
                    and directory_fingerprint(str(source), worker.isInterruptionRequested)
                    != spec.expected_directory_hash
                ):
                    raise ValueError(tr("Mod 来源已变化，请重新扫描"))
                if capture_source_version(source) != version:
                    raise ValueError(tr("Mod 来源已变化，请重新扫描"))
                return prepared
            except BaseException:
                self._cleanup_files(worker, intake, prepared)
                raise

        self._service._start_worker(request, prepare)

    def _create_category(self, request) -> None:
        """创建合法节点或显式复用同名节点，不覆盖已有分类属性。"""
        args = request.args
        parent_id = args["parent_id"]
        if parent_id:
            parent = self._manager.get_character(parent_id)
            if parent is None or parent.game_id != request.game_id:
                raise ValueError(tr("目标分类不存在或不属于当前游戏"))
        if args["node_type"] not in {"category", "character"}:
            raise ValueError(tr("分类类型无效"))
        for node in self._manager.get_characters():
            if (node.parent_id or "") == parent_id and node.name.casefold() == args["name"].casefold():
                if args["exist_ok"] and node.node_type == args["node_type"]:
                    self._service._complete(request, node_id=node.id)
                    return
                raise ValueError(tr("目标位置已有同名分类：{name}", name=args["name"]))
        self._service.started.emit(request.operation, ())
        if self._service._cancel_before_mutation(request):
            return
        node = self._manager.add_character(args["name"], args["name_en"], request.game_id, parent_id, args["node_type"])
        if node is None:
            raise ValueError(tr("无法创建分类，请检查名称和目标目录"))
        warnings = ()
        if args["thumbnail_path"] and not self._manager.update_character_thumbnail(node.id, args["thumbnail_path"]):
            warnings = (tr("分类已创建，但缩略图未能保存：{name}", name=node.name),)
        self._service._complete(request, node_id=node.id, warnings=warnings)

    def settle(self, request, worker) -> None:
        """在主线程提交准备结果，取消和提交失败时将清理交回后台。"""
        if request.kind == "import_cleanup":
            outcome = request.state.pop("cleanup_outcome")
            if worker.error:
                outcome["error"] = (outcome.get("error", "") + "\n" + worker.error).strip()
            self._service._complete(request, **outcome)
            return
        if worker.error:
            duplicate = self._duplicate(request, worker.exception)
            if duplicate is not None:
                self._service._complete(request, skipped=(duplicate.mod_id,))
            else:
                error = (
                    tr("操作已取消")
                    if request.task._cancel_requested and not isinstance(worker.exception, ImportCleanupPending)
                    else worker.error
                )
                self._service._complete(request, error=error)
            return
        prepared = worker.result
        intake = request.state["intake"]
        if request.task._cancel_requested or request.game_id != self._manager.current_game_id:
            error = tr("操作已取消") if request.task._cancel_requested else tr("游戏已切换，请重新发起操作")
            self._cleanup(request, worker, prepared, {"error": error})
            return
        try:
            imported = intake.commit(prepared, rollback_on_error=False, cleanup_on_success=False)
        except Exception as error:
            if prepared.committed:
                self._cleanup(
                    request, worker, prepared, {"succeeded": (prepared.mod.mod_id,), "warnings": (str(error),)}
                )
                return
            duplicate = self._duplicate(request, error)
            outcome = {"skipped": (duplicate.mod_id,)} if duplicate else {"error": str(error)}
            self._cleanup(request, worker, prepared, outcome)
            return
        notices = (prepared.metadata_notice,) if prepared.metadata_notice else ()
        self._cleanup(request, worker, prepared, {"succeeded": (imported.mod_id,), "warnings": notices})

    def _duplicate(self, request, error):
        """只按已验证的内容指纹查找当前请求游戏中的重复记录。"""
        if isinstance(error, DuplicateIntakeError) and error.content_hash:
            return self._manager._index.find_by_content_hash(error.content_hash, request.game_id)
        return None

    def _cleanup(self, request, source_worker, prepared, outcome: dict) -> None:
        """后台完成提交后的清理或未提交产物回滚，结束前持续保持任务占用。"""
        request.kind = "import_cleanup"
        request.state["cleanup_outcome"] = outcome
        intake = request.state["intake"]

        def cleanup(worker):
            """区分已提交资源与半成品，仅删除本次拥有的临时产物。"""
            self._cleanup_files(source_worker, intake, prepared)

        self._service._start_worker(request, cleanup, must_finish=True, check_cancel=False)

    def _cleanup_files(self, worker, intake, prepared) -> None:
        """分别回收库内产物和暂存副本，同时保留两个范围的清理失败诊断。"""
        errors = []
        try:
            if prepared is not None:
                if prepared.committed:
                    intake.cleanup_committed(prepared)
                else:
                    intake.rollback(prepared)
        except Exception as error:
            errors.append(error)
        try:
            self._clean_input_stage(worker)
        except Exception as error:
            errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            paths = tuple(path for error in errors if isinstance(error, ImportCleanupPending) for path in error.paths)
            detail = "\n".join(str(error) for error in errors)
            if paths:
                raise ImportCleanupPending(paths, detail)
            raise OSError(detail)

    def _clean_input_stage(self, worker) -> None:
        """按耐久凭据回收本次暂存副本，失败时保留令牌供后续导入恢复。"""
        stage = getattr(worker, "input_stage", None)
        if stage is not None:
            token = getattr(worker, "input_cleanup_token", "")
            if token:
                self._manager._import_cleanup_recovery.cancel(token)
            elif fs.exists(stage):
                # 登记前失败时没有开始复制，只允许移除仍为空的目录。
                fs.rmdir(stage)
            worker.input_stage = None
            worker.input_cleanup_token = ""
