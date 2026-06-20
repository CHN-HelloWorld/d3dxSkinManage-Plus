"""准备受管 Mod 的导入文件，并在主线程原子提交。"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.import_format import (
    DEGRADED_METADATA_NOTICE,
    PreparedImportSource,
    get_import_settings,
    install_staged_ini_overrides,
    materialize_import_body,
    materialize_staged_previews,
    normalize_import_format,
    prepare_import_source,
    stage_import_metadata,
    take_reject_reason,
)
from src.core.mod_identity import (
    IdentityConflict,
    new_mod_id,
    read_source_mod_id,
    validate_mod_id,
)
from src.core.mod_package import (
    InvalidManagedMetadataError,
    build_local_manifest,
    is_mod_container,
    local_meta_from_mod,
    read_container_marker,
    read_sidecar,
    restore_preview_references,
    valid_imported_at,
    write_container_marker,
    write_sidecar,
)
from src.core.mod_variant_package import saved_variants, stage_imported_variants, verify_variant_files
from src.core.preview_cache_index import capture_source_version
from src.models.mod import (
    AgeRating,
    Mod,
    ModStatus,
    validate_mod_record,
)
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import is_resident_archive
from src.utils.i18n import t as tr
from src.utils.paths import get_data_dir

if TYPE_CHECKING:
    from src.core.mod_manager import ModManager


ProgressCallback = Callable[[float | None, str], None]


@dataclass
class _IdentityReservations:
    """保存同一管理器中已索引与正在导入的身份集合，防止并发导入重复认领。"""

    indexed: set[str] = field(default_factory=set)
    inflight: set[str] = field(default_factory=set)


_identity_lock = threading.Lock()
_identity_reservations: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class IntakeError(RuntimeError):
    """无法安全准备或提交导入。"""


class DuplicateIntakeError(IntakeError):
    """该游戏已存在逻辑内容相同的 Mod。"""


class IntakeTargetMissingError(IntakeError):
    """导入期间目标目录或分类被移除。"""


def intake_identity(context: str, marker: dict | None = None, *, used=()) -> tuple[str, str]:
    """使用显式来源类型，不根据其他字段猜测来源。"""
    now = datetime.now().isoformat()
    if context == "external":
        return new_mod_id(used), now
    if context != "rediscovery":
        raise IntakeError(f"unknown intake context: {context}")
    marker = marker or {}
    try:
        identity = validate_mod_id(marker.get("mod_id"))
    except IdentityConflict:
        identity = new_mod_id(used)
    timestamp = marker.get("imported_at")
    return identity, timestamp if valid_imported_at(timestamp) else now


@dataclass(frozen=True)
class IntakePlanningSnapshot:
    """冻结导入规划使用的游戏、目标目录及现有身份和内容摘要信息。"""

    game_id: str
    index_stamp: tuple
    existing_hashes: frozenset[str]
    existing_mod_ids: frozenset[str]


@dataclass(frozen=True)
class IntakeRequest:
    """描述导入来源及可选原始文件名、目标和元数据，并携带来源版本和去重依据。"""

    source_path: Path
    game_id: str
    node_id: str
    display_name: str = ""
    move_source: bool = False
    destination_path: Path | None = None
    target_format: str = ""
    strict_format: bool | None = None
    classification: tuple[str, ...] = ()
    resource_dir: str = ""
    auto_preview: bool = True
    selected_preview_name: str = ""
    excluded_previews: tuple[str, ...] = ()
    age_rating: str = ""
    gamebanana_metadata: dict | None = None
    force_age_rating: str = ""
    existing_hashes: frozenset[str] = frozenset()
    existing_mod_ids: frozenset[str] = frozenset()
    context: str = "external"
    external_copy: bool = False
    source_version: tuple | None = None
    source_name: str = ""


@dataclass
class PreparedIntake:
    """保存已准备的 Mod、文件产物、原始快照和预留资源，供提交或回滚使用。"""

    original_path: Path
    body_path: Path
    container_path: Path
    content_hash: str
    share_manifest: dict | None
    preview_paths: list[Path]
    created_paths: list[Path]
    source_was_moved: bool
    request: IntakeRequest = field(repr=False)
    mod: Mod = field(repr=False)
    import_source: PreparedImportSource = field(repr=False)
    preview_cache_path: Path | None = field(default=None, repr=False)
    source_backup_path: Path | None = field(default=None, repr=False)
    source_backup_root: Path | None = field(default=None, repr=False)
    source_marker_snapshot: bytes | None = field(default=None, repr=False)
    source_sidecar_snapshot: tuple[bool, bytes] | None = field(default=None, repr=False)
    metadata_version: tuple[bytes | None, bytes | None] | None = field(default=None, repr=False)
    written_marker: bytes | None = field(default=None, repr=False)
    written_sidecar: bytes | None = field(default=None, repr=False)
    operation_temp: Path | None = field(default=None, repr=False)
    body_version: tuple | None = field(default=None, repr=False)
    variants: tuple[ModVariant, ...] = field(default=(), repr=False)
    #: 非空表示随包描述类元数据已整份丢弃（内容完好）；由任务中心明细行展示给用户。
    metadata_notice: str = ""
    committed: bool = field(default=False, init=False)


class ModIntakeService:
    """先准备文件，再提交一条数据库记录。"""

    def __init__(self, manager: ModManager) -> None:
        """绑定管理器并获取其共享身份预留表，以协调多个导入服务实例。"""
        self._manager = manager
        with _identity_lock:
            self._identities = _identity_reservations.setdefault(manager, _IdentityReservations())

    def _snapshot_mod_ids(self) -> frozenset[str]:
        """在主线程读取游戏配置，准备线程仅使用冻结快照。"""
        indexed = self._manager._index.get_mod_ids()
        with _identity_lock:
            self._identities.indexed = set(indexed)
        return frozenset(indexed)

    def planning_snapshot(self, game_id: str) -> IntakePlanningSnapshot:
        """采集批量查重信息，不构造完整 Mod 对象。"""
        index = self._manager._index
        if index.in_transaction:
            raise IntakeError("cannot capture intake planning snapshot in a transaction")
        stamp = index.change_stamp
        rows = (
            index._db()
            .execute("SELECT DISTINCT content_hash FROM mods WHERE game_id=? AND content_hash<>''", (game_id,))
            .fetchall()
        )
        hashes = frozenset(row[0] for row in rows)
        identities = self._snapshot_mod_ids()
        if stamp != index.change_stamp:
            raise IntakeError("index changed during planning snapshot capture")
        return IntakePlanningSnapshot(game_id, stamp, hashes, identities)

    def _reserve_mod_id(self, request: IntakeRequest, identity: str | None = None) -> str:
        """在锁内生成或认领未占用身份，重复身份抛出冲突异常。"""
        with _identity_lock:
            occupied = self._identities.indexed | self._identities.inflight | request.existing_mod_ids
            if identity is None:
                identity = new_mod_id(occupied)
            elif identity in occupied:
                raise IdentityConflict(tr("mod_id 身份已被其他 Mod 占用: {identity}", identity=identity))
            self._identities.inflight.add(identity)
            return identity

    def _release_mod_id(self, identity: str, *, committed: bool = False) -> None:
        """释放在途身份预留；已提交身份同时加入已索引集合。"""
        with _identity_lock:
            if committed:
                # 较早请求可能仍持有提交前的快照。
                self._identities.indexed.add(identity)
            self._identities.inflight.discard(identity)

    def plan_request(
        self,
        *,
        source_path: str | Path,
        game_id: str,
        node_id: str,
        display_name: str = "",
        source_name: str = "",
        move_source: bool = False,
        target_format: str | None = None,
        strict_format: bool | None = None,
        auto_preview: bool = True,
        selected_preview_name: str = "",
        excluded_previews: tuple[str, ...] = (),
        age_rating: str = "",
        gamebanana_metadata: dict | None = None,
        force_age_rating: str = "",
        context: str = "external",
        external_copy: bool = False,
        source_version: tuple | None = None,
        planning_snapshot: IntakePlanningSnapshot | None = None,
    ) -> IntakeRequest:
        """冻结配置和索引派生的导入输入，原始文件名仅用于新容器的默认命名。"""
        if game_id != self._manager.current_game_id:
            raise IntakeError("intake game no longer matches the active game")
        if self._manager._index.in_transaction:
            raise IntakeError("cannot plan intake in a transaction")
        if context not in {"external", "rediscovery"}:
            raise IntakeError(f"unknown intake context: {context}")
        if context == "rediscovery":
            try:
                identity = read_source_mod_id(source_path)
            except (IdentityConflict, InvalidManagedMetadataError):
                identity = None
            if identity and self._manager._index.get_mod(identity) is None:
                with _identity_lock:
                    self._identities.indexed.discard(identity)
        snapshot = planning_snapshot or IntakePlanningSnapshot(
            game_id, self._manager._index.change_stamp, frozenset(), frozenset()
        )
        if snapshot.game_id != game_id or snapshot.index_stamp != self._manager._index.change_stamp:
            raise IntakeError("intake planning snapshot is no longer current")
        configured_format, configured_strict = get_import_settings(
            self._manager._config,
        )
        planned_format = normalize_import_format(target_format or configured_format)
        planned_strict = bool(configured_strict) if strict_format is None else bool(strict_format)
        try:
            destination = self._manager._get_character_library(
                node_id,
                game_id,
                create=False,
            )
        except KeyError as exc:
            raise IntakeTargetMissingError(tr("导入目标目录已被删除")) from exc
        request = IntakeRequest(
            source_path=Path(fs.normal_path(source_path)),
            game_id=game_id,
            node_id=node_id,
            display_name=display_name,
            source_name=source_name,
            move_source=move_source,
            destination_path=destination,
            target_format=planned_format,
            strict_format=planned_strict,
            classification=tuple(
                self._manager._character_breadcrumb(node_id, game_id),
            ),
            resource_dir=self._manager._res_dir(game_id),
            auto_preview=auto_preview,
            selected_preview_name=selected_preview_name,
            excluded_previews=tuple(excluded_previews),
            age_rating=age_rating,
            gamebanana_metadata=(dict(gamebanana_metadata) if gamebanana_metadata is not None else None),
            force_age_rating=force_age_rating,
            existing_hashes=snapshot.existing_hashes,
            existing_mod_ids=snapshot.existing_mod_ids,
            context=context,
            external_copy=external_copy,
            source_version=source_version,
        )
        self._validate_target(request, check_character=True)
        return request

    def _validate_target(
        self,
        request: IntakeRequest,
        *,
        check_character: bool = False,
    ) -> Path:
        # 规划和提交在主线程检查分类；worker 只查事先冻结的物理路径，不读 SQLite。
        # 未规划的同步调用也必须检查分类，避免已删除的 ID 被路径解析回退到库根。
        """确认目标分类仍属于游戏且目标目录存在，返回有效目标目录。"""
        if request.node_id and (check_character or request.destination_path is None):
            character = (
                self._manager._index._db()
                .execute(
                    "SELECT 1 FROM nodes WHERE id = ? AND game_id = ?",
                    (request.node_id, request.game_id),
                )
                .fetchone()
            )
            if character is None:
                raise IntakeTargetMissingError(tr("导入目标目录已被删除"))
        destination = request.destination_path
        if destination is None:
            destination = self._manager._get_character_library(
                request.node_id,
                request.game_id,
                create=False,
            )
        if not fs.is_dir(destination):
            raise IntakeTargetMissingError(tr("导入目标目录已不存在: {destination}", destination=destination))
        return Path(destination)

    @staticmethod
    def _progress(callback: ProgressCallback | None, value: float | None, text: str) -> None:
        """在提供回调时传递导入进度值及阶段说明。"""
        if callback is not None:
            callback(value, text)

    def prepare(
        self,
        request: IntakeRequest,
        *,
        progress: ProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PreparedIntake:
        """检查来源与目标，预留身份后准备格式、元数据及受管容器，返回待提交结果。

        耗时阶段响应取消并复核来源版本；准备失败时清理产物、恢复移动来源并释放身份和路径保护。"""
        source = Path(fs.normal_path(request.source_path))
        if request.game_id != self._manager.current_game_id:
            raise IntakeError("intake game no longer matches the active game")
        destination = self._validate_target(request)
        if not fs.exists(source):
            raise IntakeError(f"source does not exist: {source}")

        def continuing() -> bool:
            """将取消回调转换为后续文件操作所需的继续执行布尔值。"""
            return not (should_cancel is not None and should_cancel())

        def report_copy(value: float) -> None:
            """把复制进度映射到总导入进度区间，通知后重新检查目标目录。"""
            self._progress(
                progress,
                0.77 + 0.13 * value,
                tr("复制到 Mod 库: {value}", value=request.display_name or source.name),
            )
            # 进度回调也可能暂停当前项；恢复后必须先复查，避免 copy_path 创建父目录。
            self._validate_target(request)

        if request.context not in {"external", "rediscovery"}:
            raise IntakeError(f"unknown intake context: {request.context}")
        if request.source_version is not None and capture_source_version(source) != request.source_version:
            raise IntakeError("source changed after discovery")
        if request.context == "rediscovery":
            # 接管已有容器时也需传递 continuing，保证库内重新发现操作响应取消。
            return self._prepare_existing(request, source, checkpoint=continuing)

        if request.target_format:
            target_format = normalize_import_format(request.target_format)
            use_strict = bool(request.strict_format)
        else:
            configured_format, configured_strict = get_import_settings(
                self._manager._config,
            )
            target_format = normalize_import_format(configured_format)
            use_strict = bool(configured_strict)
        prepared_source: PreparedImportSource | None = None
        container: Path | None = None
        preview_cache: Path | None = None
        source_backup: Path | None = None
        source_backup_root: Path | None = None
        mod_id: str | None = None
        operation_temp = None
        try:
            mod_id = self._reserve_mod_id(request)
            operation_temp = get_data_dir() / "temp" / f"intake_{mod_id}"
            protected_source = source.parent if fs.is_file(source.parent / ".d3dxmod") else source
            self._manager.protect_library_operation(
                "intake:" + mod_id, paths=(protected_source, operation_temp), mod_ids=(mod_id,)
            )
            self._progress(progress, 0.03, tr("分析格式: {value}", value=request.display_name or source.name))
            prepared_source = prepare_import_source(
                source,
                self._manager._archive,
                target_format,
                use_strict,
                operation_temp,
                excluded_previews=request.excluded_previews,
                extract_progress_cb=lambda value: self._progress(
                    progress,
                    0.05 + 0.25 * value,
                    tr("解压: {value}", value=request.display_name or source.name),
                ),
                checkpoint=continuing,
                should_cancel=(lambda: not continuing()),
            )
            if prepared_source is None or not continuing():
                # 元数据非法时带上具体原因（哪个文件坏了、怎么处置），不再只说通用失败。
                reason = take_reject_reason()
                raise IntakeError(reason or "failed to prepare import source")

            self._progress(progress, None, tr("校验内容: {value}", value=request.display_name or source.name))
            prepared_source.content_version = capture_source_version(prepared_source.clean_path)
            content_hash = Mod.compute_sha1(
                prepared_source.clean_path,
                checkpoint=continuing,
            )
            if not content_hash:
                raise IntakeError("content hash is empty")
            if capture_source_version(prepared_source.clean_path) != prepared_source.content_version:
                raise IntakeError("source changed while computing content hash")
            if content_hash in request.existing_hashes and not (
                request.context == "external" and request.external_copy
            ):
                raise DuplicateIntakeError(
                    f"content already exists: {request.display_name or source.name}",
                )

            if not stage_import_metadata(
                prepared_source,
                self._manager._archive,
                operation_temp,
                include_previews=request.auto_preview,
                checkpoint=continuing,
                should_cancel=(lambda: not continuing()),
            ):
                raise IntakeError("failed to stage previews or INI overrides")

            imported_at = datetime.now().isoformat()

            if request.move_source:
                source_backup_root = get_data_dir() / "temp" / f"intake_backup_{uuid.uuid4().hex}"
                fs.ensure_dir(source_backup_root, exist_ok=False)
                source_backup = source_backup_root / source.name
                self._manager.protect_library_operation("intake:" + mod_id, paths=(source_backup_root,))
                os.replace(fs.native_path(source), fs.native_path(source_backup))
                try:
                    clean_relative = Path(fs.normal_path(prepared_source.clean_path)).relative_to(source)
                except ValueError:
                    pass
                else:
                    prepared_source.clean_path = source_backup / clean_relative

            final_label = prepared_source.final_format.upper()
            materialized = materialize_import_body(
                prepared_source,
                self._manager._archive,
                progress_cb=lambda value: self._progress(
                    progress,
                    0.48 + 0.27 * value,
                    tr(
                        "转为 {final_label}: {value}",
                        final_label=final_label,
                        value=request.display_name or source.name,
                    ),
                ),
                should_cancel=(lambda: not continuing()),
                checkpoint=continuing,
            )
            if materialized is None or not continuing():
                raise IntakeError(f"failed to materialize {final_label}")

            # 解压/转格式可能耗时很久，写入磁盘前再查，不能让复制函数重建被删的目标目录。
            self._validate_target(request)
            if capture_source_version(prepared_source.clean_path) != prepared_source.content_version:
                raise IntakeError("source changed while preparing import")
            body = self._manager._storage.copy_mod_into_container(
                materialized,
                destination,
                content_hash=content_hash,
                source_name=request.source_name,
                mod_id=mod_id,
                imported_at=imported_at,
                checkpoint=continuing,
                should_cancel=(lambda: not continuing()),
                progress_cb=report_copy,
                before_create=lambda target: self._manager.protect_library_operation(
                    "intake:" + mod_id, paths=(target,)
                ),
            )
            if body is None:
                raise IntakeError("failed to create managed container")
            body = Path(body)
            container = body.parent
            if capture_source_version(prepared_source.clean_path) != prepared_source.content_version:
                raise IntakeError("source changed while copying import")

            # 登记「在途 mod_id」：此刻预览缓存已按最终 id 写入磁盘，但 commit 还没 add_mod。
            # 孤儿清理（_cleanup_orphaned_managed_artifacts）按「是否在索引中」判定，
            # 若它在这个窗口期跑起来就会把缓存当孤儿删掉。核心层登记比 UI 层加闸门更
            # 可靠——任何调用方触发的清理都能豁免。commit / rollback 负责注销。
            self._manager.register_inflight_mod_id(mod_id)
            if fs.is_file(body) and is_resident_archive(body):
                resource_dir = request.resource_dir or self._manager._res_dir(
                    request.game_id,
                )
                preview_cache = get_data_dir() / resource_dir / "preview_cache" / mod_id
                self._manager.protect_library_operation("intake:" + mod_id, paths=(preview_cache,))
            previews = materialize_staged_previews(
                prepared_source,
                body,
                cache_dir=preview_cache,
                content_hash=content_hash,
                checkpoint=continuing,
                should_cancel=(lambda: not continuing()),
            )
            if fs.is_file(body) and is_resident_archive(body):
                if not install_staged_ini_overrides(
                    prepared_source,
                    body,
                    checkpoint=continuing,
                    should_cancel=(lambda: not continuing()),
                ):
                    raise IntakeError("failed to install INI overrides")

            mod = Mod(
                mod_id=mod_id,
                game_id=request.game_id,
                name=container.name,
                folder_name=container.name,
                path=str(body),
                content_hash=content_hash,
                status=ModStatus.DISABLED,
                node_id=request.node_id,
                preview_images=[str(path) for path in previews],
                imported_at=imported_at,
            )
            if request.age_rating:
                try:
                    mod.info.age_rating = AgeRating(request.age_rating)
                except ValueError:
                    pass
            self._manager._apply_share_envelope(
                mod,
                prepared_source.share_manifest,
                prepared_source.share_previews,
            )
            if request.display_name:
                mod.name = request.display_name
            gb = request.gamebanana_metadata or {}
            if gb:
                mod.info.custom_data["gamebanana"] = dict(gb)
                if not mod.info.author and gb.get("submitter_name"):
                    mod.info.author = str(gb["submitter_name"])
                if not mod.info.url and gb.get("page_url"):
                    mod.info.url = str(gb["page_url"])
                if not mod.info.version and gb.get("version"):
                    mod.info.version = str(gb["version"])
            if request.force_age_rating and mod.info.age_rating == AgeRating.G:
                try:
                    mod.info.age_rating = AgeRating(request.force_age_rating)
                except ValueError:
                    pass
            if request.selected_preview_name and mod.preview_images:
                selected = (
                    request.selected_preview_name.replace(
                        "\\",
                        "/",
                    )
                    .strip("/")
                    .casefold()
                )
                for index, image_path in enumerate(mod.preview_images):
                    key = str(image_path).replace("\\", "/").casefold()
                    if key == selected or key.endswith("/" + selected):
                        if index:
                            mod.preview_images.insert(0, mod.preview_images.pop(index))
                        break
            local: dict = {}
            preview_roots = [("container", body.parent)]
            if preview_cache is not None:
                preview_roots.append(("preview_cache", preview_cache))
            if request.node_id:
                preview_roots.append(("node", body.parent.parent))
            references = []
            for path in mod.preview_images:
                for scope, root in preview_roots:
                    try:
                        relative = Path(path).relative_to(root).as_posix()
                    except ValueError:
                        continue
                    references.append({"scope": scope, "path": relative})
                    break
                else:
                    raise IntakeError(f"preview has no local logical location: {path}")
            local["preview_references"] = references
            sidecar = build_local_manifest(local_meta_from_mod(mod), local)
            if not write_sidecar(mod.path, sidecar):
                raise IntakeError("failed to write current sidecar")
            imported_variants = stage_imported_variants(body, prepared_source.share_variants)
            self._validate_container(container, body, content_hash, mod_id)

            return PreparedIntake(
                original_path=source,
                body_path=body,
                container_path=container,
                content_hash=content_hash,
                share_manifest=prepared_source.share_manifest,
                preview_paths=[Path(path) for path in mod.preview_images],
                created_paths=[container],
                source_was_moved=source_backup is not None,
                request=request,
                mod=mod,
                import_source=prepared_source,
                preview_cache_path=preview_cache,
                source_backup_path=source_backup,
                source_backup_root=source_backup_root,
                body_version=capture_source_version(body),
                metadata_notice=prepared_source.metadata_notice,
                operation_temp=operation_temp,
                variants=imported_variants,
            )
        except BaseException:
            try:
                if mod_id is not None:
                    self._release_mod_id(mod_id)
                if container is not None:
                    fs.rmtree(container, ignore_errors=True)
                if preview_cache is not None:
                    fs.rmtree(preview_cache, ignore_errors=True)
                # 产物回滚时同时撤销在途登记，使预览清理能够回收不再使用的缓存。
                # 准备早期失败时可能尚未生成 Mod 编号，按实际已创建状态处理。
                leaked_id = locals().get("mod_id")
                if leaked_id:
                    self._manager.unregister_inflight_mod_id(leaked_id)
                self._restore_source(source, source_backup, source_backup_root)
                if prepared_source is not None:
                    prepared_source.cleanup()
            finally:
                if operation_temp is not None:
                    fs.rmtree(operation_temp, ignore_errors=True)
                if mod_id is not None:
                    self._manager.release_library_operation("intake:" + mod_id)
            raise

    def _prepare_existing(
        self,
        request: IntakeRequest,
        body: Path,
        *,
        checkpoint: Callable[[], bool] | None = None,
    ) -> PreparedIntake:
        """校验重新发现的受管容器并恢复其身份、描述、预览及变体引用。

        仅在内容和元数据版本稳定时补齐标记；失败时按写入快照恢复原标记并释放预留。"""

        def _check() -> None:
            """在耗时校验和预览提取之间检查取消请求，以中断异常交由调用方回滚。"""
            if checkpoint is not None and not checkpoint():
                raise InterruptedError("intake cancelled")

        _check()
        container = body.parent
        metadata_version = self._metadata_version(container)
        marker = read_container_marker(container)
        if marker is None:
            raise IntakeError(f"invalid managed container: {container}")
        variants = saved_variants(body)
        if variants:
            try:
                validate_mod_id(marker.get("mod_id"))
            except IdentityConflict as error:
                raise IntakeError(tr("含已保存变体的 Mod 身份无法确认，原文件与变体 ID 已保留")) from error
        mod_id, imported_at = intake_identity("rediscovery", marker, used=request.existing_mod_ids)
        marker_snapshot = None
        written_marker = None
        try:
            sidecar = read_sidecar(body) or {}
        except InvalidManagedMetadataError:
            sidecar = {}
        source_version = capture_source_version(body)
        content_hash = Mod.compute_sha1(body, checkpoint=checkpoint)
        if not content_hash:
            raise IntakeError(f"container hash is empty: {container}")
        if capture_source_version(body) != source_version:
            raise IntakeError("source changed during rediscovery fingerprinting")
        mod_id = self._reserve_mod_id(request, mod_id)
        preview_cache = None
        try:
            self._manager.protect_library_operation("intake:" + mod_id, paths=(container,), mod_ids=(mod_id,))
            _check()
            resource_dir = request.resource_dir or self._manager._res_dir(request.game_id)
            if fs.is_file(body) and is_resident_archive(body):
                preview_cache = get_data_dir() / resource_dir / "preview_cache" / mod_id
                self._manager.protect_library_operation("intake:" + mod_id, paths=(preview_cache,))
            self._manager.register_inflight_mod_id(mod_id)
            previews = self._manager._find_previews(
                str(body),
                mod_id=mod_id,
                resource_dir=resource_dir,
                content_hash=content_hash,
            )
            _check()
            mod = Mod(
                mod_id=mod_id,
                game_id=request.game_id,
                name=sidecar.get("name") or request.display_name or container.name,
                folder_name=container.name,
                path=str(body),
                content_hash=content_hash,
                status=(
                    ModStatus.ENABLED
                    if self._manager._storage.is_deployed(
                        str(body),
                        deploy_name=container.name,
                        mod_id=mod_id,
                    )
                    else ModStatus.DISABLED
                ),
                node_id=request.node_id,
                preview_images=previews,
                imported_at=imported_at,
            )
            self._manager._apply_sidecar_info(mod, sidecar)
            mod.preview_images = restore_preview_references(
                sidecar,
                mod.preview_images,
                container=container,
                preview_cache=get_data_dir() / resource_dir / "preview_cache" / mod_id,
                node=(request.destination_path or container.parent) if request.node_id else None,
            )
            source_format_name = body.suffix.lower().lstrip(".") if fs.is_file(body) else "folder"
            _check()
            if capture_source_version(body) != source_version:
                raise IntakeError("source changed during rediscovery")
            if self._metadata_version(container) != metadata_version:
                raise IntakeError("source metadata changed during rediscovery")
            # 校验前补齐缺失的本地字段；原子标记写入器禁止替换已有有效身份。
            if (
                marker.get("mod_id") != mod_id
                or marker.get("imported_at") != imported_at
                or marker.get("content_hash") != content_hash
            ):
                marker_snapshot = (container / ".d3dxmod").read_bytes()
                write_container_marker(container, body.name, content_hash, mod_id=mod_id, imported_at=imported_at)
                written_marker = self._read_metadata_bytes(container / ".d3dxmod")
                metadata_version = (written_marker, metadata_version[1])
            self._validate_container(container, body, content_hash, mod_id)
            return PreparedIntake(
                original_path=body,
                body_path=body,
                container_path=container,
                content_hash=content_hash,
                share_manifest=None,
                preview_paths=[Path(path) for path in mod.preview_images],
                created_paths=[],
                source_was_moved=False,
                request=request,
                mod=mod,
                import_source=PreparedImportSource(
                    clean_path=body,
                    source_format=source_format_name,
                    final_format=source_format_name,
                    passthrough_archive=fs.is_file(body),
                ),
                preview_cache_path=preview_cache,
                source_marker_snapshot=marker_snapshot,
                metadata_version=metadata_version,
                written_marker=written_marker,
                body_version=source_version,
                metadata_notice=tr(DEGRADED_METADATA_NOTICE) if not sidecar else "",
                variants=variants,
            )
        except BaseException:
            try:
                self._release_mod_id(mod_id)
                if (
                    marker_snapshot is not None
                    and written_marker is not None
                    and self._read_metadata_bytes(container / ".d3dxmod") == written_marker
                ):
                    self._restore_marker(container, marker_snapshot)
                if mod_id:
                    self._manager.unregister_inflight_mod_id(mod_id)
            finally:
                self._manager.release_library_operation("intake:" + mod_id)
            raise

    def commit(
        self,
        prepared: PreparedIntake,
        *,
        rollback_on_error: bool = True,
    ) -> Mod:
        """重新验证目标、正文和变体文件，在同一索引事务中检查身份与内容冲突并写入 Mod。

        提交前失败按参数回滚准备结果；成功后标记已提交、释放在途登记并清理临时文件及通知导入事件。"""
        try:
            if (
                prepared.request.context == "rediscovery"
                and self._metadata_version(prepared.container_path) != prepared.metadata_version
            ):
                raise IntakeError("source metadata changed before intake commit")
            # 软件内删除可能因文件占用留下物理目录，分类记录也必须仍然存在。
            self._validate_target(prepared.request, check_character=True)
            if (
                prepared.body_version is not None
                and capture_source_version(prepared.body_path) != prepared.body_version
            ):
                raise IntakeError("managed body changed before intake commit")
            self._validate_container(
                prepared.container_path,
                prepared.body_path,
                prepared.content_hash,
                prepared.mod.mod_id,
            )
            verify_variant_files(prepared.body_path, prepared.variants)
            # 与 ModIndex 共用必填字段规则；校验失败进入导入回滚并向进度面板报告。
            reason = validate_mod_record(prepared.mod, prepared.request.game_id)
            if reason:
                raise IntakeError(f"invalid managed record: {reason}")
            if prepared.request.context == "rediscovery" and prepared.metadata_notice:
                sidecar_path = prepared.container_path / ".d3dxmeta" / "info.json"
                try:
                    prepared.source_sidecar_snapshot = (True, sidecar_path.read_bytes())
                except FileNotFoundError:
                    prepared.source_sidecar_snapshot = (False, b"")
                if not self._manager._write_mod_sidecar(prepared.mod, repair_invalid=True):
                    raise IntakeError("failed to restore source descriptions")
                prepared.written_sidecar = self._read_metadata_bytes(sidecar_path)
            old_game = self._manager._index.game_id
            self._manager._index.game_id = prepared.request.game_id
            try:
                with self._manager._index.transaction():
                    # 在插入记录的同一事务中重新核对全部索引约束，中间不执行事件回调或文件写入。
                    owner = self._manager._index.get_mod(prepared.mod.mod_id)
                    if owner is not None:
                        raise IdentityConflict(
                            tr("mod_id 身份已被其他 Mod 占用: {display_name}", display_name=owner.display_name),
                        )
                    existing = self._manager._index.find_by_content_hash(
                        prepared.content_hash,
                        prepared.request.game_id,
                    )
                    if (
                        existing is not None
                        and prepared.request.context == "external"
                        and not prepared.request.external_copy
                    ):
                        raise DuplicateIntakeError(
                            f"content already exists: {existing.display_name}",
                        )
                    occupied = self._manager._index.find_mod_id_by_path(
                        prepared.mod.path,
                        prepared.request.game_id,
                    )
                    if occupied is not None:
                        raise DuplicateIntakeError(f"path already managed: {prepared.mod.display_name}")
                    added = self._manager._index.add_mod(
                        prepared.mod,
                        commit=False,
                    )
                    if not added:
                        raise IntakeError("failed to add Mod to the index")
            finally:
                self._manager._index.game_id = old_game
        except Exception:
            if rollback_on_error:
                self.rollback(prepared)
            raise

        prepared.committed = True
        try:
            self._release_mod_id(prepared.mod.mod_id, committed=True)
            # 已入库 → 索引本身就能保护它，注销在途登记。
            self._manager.unregister_inflight_mod_id(prepared.mod.mod_id)
            prepared.import_source.cleanup()
            if prepared.source_backup_root is not None:
                fs.rmtree(prepared.source_backup_root, ignore_errors=True)
            self._manager._emit(
                "mod_imported",
                mod_id=prepared.mod.mod_id,
                node_id=prepared.mod.node_id,
                game_id=prepared.request.game_id,
            )
        finally:
            if prepared.operation_temp is not None:
                fs.rmtree(prepared.operation_temp, ignore_errors=True)
            self._manager.release_library_operation("intake:" + prepared.mod.mod_id)
        return prepared.mod

    def rollback(self, prepared: PreparedIntake) -> None:
        """撤销尚未提交的导入准备，并确保清理操作临时目录及释放路径保护。"""
        try:
            self._rollback(prepared)
        finally:
            if prepared.operation_temp is not None:
                fs.rmtree(prepared.operation_temp, ignore_errors=True)
            self._manager.release_library_operation("intake:" + prepared.mod.mod_id)

    def _rollback(self, prepared: PreparedIntake) -> None:
        """移除本次产物和在途登记，按写入快照恢复原始元数据与移动来源；已提交结果不撤销。"""
        if prepared.committed:
            return
        self._release_mod_id(prepared.mod.mod_id)
        # 本轮产物即将全部删除，在途登记随之注销（放在最前，异常也不残留登记）。
        try:
            self._manager.unregister_inflight_mod_id(prepared.mod.mod_id)
        except Exception:
            pass
        for path in reversed(prepared.created_paths):
            if fs.is_dir(path):
                fs.rmtree(path, ignore_errors=True)
            elif fs.exists(path):
                fs.unlink(path, missing_ok=True)
        if prepared.preview_cache_path is not None and prepared.request.context == "external":
            fs.rmtree(prepared.preview_cache_path, ignore_errors=True)
        if (
            prepared.source_marker_snapshot is not None
            and prepared.written_marker is not None
            and self._read_metadata_bytes(prepared.container_path / ".d3dxmod") == prepared.written_marker
        ):
            self._restore_marker(prepared.container_path, prepared.source_marker_snapshot)
        if (
            prepared.source_sidecar_snapshot is not None
            and prepared.written_sidecar is not None
            and self._read_metadata_bytes(prepared.container_path / ".d3dxmeta" / "info.json")
            == prepared.written_sidecar
        ):
            existed, snapshot = prepared.source_sidecar_snapshot
            path = prepared.container_path / ".d3dxmeta" / "info.json"
            if existed:
                self._restore_metadata(path, snapshot)
            else:
                fs.unlink(path, missing_ok=True)
        self._restore_source(
            prepared.original_path,
            prepared.source_backup_path,
            prepared.source_backup_root,
        )
        prepared.import_source.cleanup()

    @staticmethod
    def _read_metadata_bytes(path: Path) -> bytes | None:
        """读取元数据原始字节，文件不存在时返回 None。"""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _metadata_version(container: Path) -> tuple[bytes | None, bytes | None]:
        """以容器标记和旁置描述的完整字节组成元数据版本。"""
        return (
            ModIntakeService._read_metadata_bytes(container / ".d3dxmod"),
            ModIntakeService._read_metadata_bytes(container / ".d3dxmeta" / "info.json"),
        )

    @staticmethod
    def _restore_marker(container: Path, snapshot: bytes) -> None:
        """恢复采集的原始内容，包括原本不完整的标记。"""
        ModIntakeService._restore_metadata(container / ".d3dxmod", snapshot)

    @staticmethod
    def _restore_metadata(path: Path, snapshot: bytes) -> None:
        """通过同目录临时文件和原子替换恢复元数据快照，写入后刷新磁盘。"""
        descriptor, temporary = tempfile.mkstemp(prefix=".intake-metadata-", dir=fs.native_path(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(snapshot)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, fs.native_path(path))
        finally:
            fs.unlink(temporary, missing_ok=True)

    @staticmethod
    def _restore_source(
        original: Path,
        backup: Path | None,
        backup_root: Path | None,
    ) -> None:
        """将已移动的备份恢复到原位置，并清理本次来源备份目录。"""
        if backup is not None and fs.exists(backup):
            fs.ensure_dir(original.parent)
            os.replace(fs.native_path(backup), fs.native_path(original))
        if backup_root is not None:
            fs.rmtree(backup_root, ignore_errors=True)

    @staticmethod
    def _validate_container(
        container: Path,
        body: Path,
        content_hash: str,
        mod_id: str,
    ) -> None:
        """核对受管容器格式、正文名称、摘要及持久身份，任一不符时拒绝提交。"""
        if not is_mod_container(container):
            raise IntakeError(f"invalid managed container: {container}")
        marker = read_container_marker(container)
        if marker is None or marker.get("body") != body.name:
            raise IntakeError(f"container body mismatch: {container}")
        if marker.get("content_hash") != content_hash:
            raise IntakeError(f"container hash mismatch: {container}")
        if read_source_mod_id(body) != mod_id:
            raise IdentityConflict(tr("源 Mod 的 mod_id 身份发生变化: {container}", container=container))
