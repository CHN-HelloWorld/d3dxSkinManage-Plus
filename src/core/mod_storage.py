"""管理 Mod 源资源及其部署副本。

资源库保存源内容，启用时将其链接或复制到部署目录，禁用时移除部署。
源资源与部署路径分别管理，避免禁用操作影响资源库。
"""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PureWindowsPath

from src.core.library_snapshot import (
    DeploymentInventory,
    library_mutation,
    scan_deployment_inventory,
)
from src.core.mod_identity import (
    DEPLOYMENT_IDENTITY_FILE,
    IdentityConflict,
    read_deployment_identity,
    read_source_mod_id,
    validate_mod_id,
    write_deployment_identity,
)
from src.utils import fs
from src.utils.constants import (
    MIGOTO_DISABLED_PREFIX,
    MOD_CONTAINER_MARKER,
    RESIDENT_ARCHIVE_EXTENSIONS,
    is_resident_archive,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger


def write_ini_preserve_link(target: Path, text: str) -> bool:
    """就地写入 INI 文本并保留链接关系，失败时返回 False。

    符号链接写入其目标，硬链接保留文件身份，使覆盖文件和部署文件继续共享内容。
    不能使用原子替换，否则会将链接替换为独立文件。普通文件也按就地写入处理。
    """
    target = Path(target)
    try:
        fs.ensure_dir(target.parent)
        with open(fs.native_path(target), "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(text)
        return True
    except OSError as e:
        logger.warning("保链接写 ini 失败 (%s): %s", target, e)
        return False


# 进程级锁覆盖目标名称检查与文件复制或移动，避免并发操作覆盖同名内容。
_FS_OP_LOCK = threading.Lock()
_DEPLOYMENT_PUBLISH_LOCK = threading.Lock()


def _is_name_taken(dst: Path) -> bool:
    """检查目标名称是否被文件夹或同名 ZIP、7z、RAR 占用。

    文件夹与归档使用等价名称判定，避免导入和压缩覆盖已有同名资源。
    """
    if fs.exists(dst):
        return True
    # 文件夹场景：检查任一同名常驻压缩档是否存在
    if dst.suffix == "":
        for ext in RESIDENT_ARCHIVE_EXTENSIONS:
            if fs.exists(dst.with_suffix(ext)):
                return True
    # 压缩档场景：检查同名（无扩展名）文件夹是否存在
    elif is_resident_archive(dst):
        if fs.exists(dst.with_suffix("")):
            return True
    return False


class ModStorage:
    """分别管理资源库本体和已启用的部署副本。

    启用时创建链接或副本，禁用时移除部署，删除时同时清理源资源与对应部署。
    """

    def __init__(self) -> None:
        """初始化资料库修改计数、部署根配置及持久身份扫描缓存。"""
        self._library_epoch_lock = threading.RLock()
        self._library_epoch = 0
        self._library_mutations = 0
        self._identity_cache_epoch = -1
        self._migoto_mods_dir: Path | None = None  # 3DMigoto/Mods 目录
        self._identity_cache_signature = None
        self._identity_cache = {}
        self._identity_cache_time = 0.0

    # 目录设置

    @property
    def migoto_mods_dir(self) -> Path | None:
        """3DMigoto Mods 部署目录"""
        return self._migoto_mods_dir

    @library_mutation
    def set_migoto_mods_dir(self, path: str | Path) -> None:
        """设置 3DMigoto Mods 部署目录"""
        p = Path(path)
        if p and str(p) not in ("", "."):
            fs.ensure_dir(p)
            self._migoto_mods_dir = p
            logger.info("3DMigoto Mods 目录设置为: %s", p)
        else:
            self._migoto_mods_dir = None

    def get_deploy_dir(self) -> Path | None:
        """当前部署目录（3DMigoto Mods 目录）；未设置返回 None。

        override→部署副本同步、双向监测都需据此定位部署副本 ini。
        """
        return self._migoto_mods_dir

    @staticmethod
    def _resolve_deploy_name(
        source_path: str | Path,
        deploy_name: str = "",
        *,
        source_is_archive: bool | None = None,
    ) -> str:
        """返回稳定且不含路径穿越的部署目录名。

        ``deploy_name`` 由索引中的 ``Mod.folder_name`` 提供，是新建部署时的首选名称；
        未提供时按源路径取名。名称不参与身份判断。回退判定只看后缀而不看
        ``is_file``，避免压缩档源容器被外部删除后把 ``Foo.zip`` 错当文件夹名。
        """
        raw = str(deploy_name or "").strip()
        if raw:
            # 从名称字段提取单一文件名，避免外部数据使操作路径超出部署根目录。
            safe = Path(raw).name
            if safe not in ("", ".", ".."):
                return safe

        src = Path(source_path)
        is_archive = is_resident_archive(src) if source_is_archive is None else source_is_archive
        return src.stem if is_archive else src.name

    @classmethod
    def _deploy_path_exists(cls, path: Path) -> bool:
        """包含断链 symlink / junction 的部署路径存在性判断。"""
        return fs.lexists(path) or fs.is_symlink(path) or cls._is_junction(path)

    @staticmethod
    def _path_root_available(path: Path) -> bool:
        """检查部署路径所在卷或 UNC 根的可达性，避免将离线状态误判为删除。"""
        anchor = path.anchor
        if not anchor:
            return True
        try:
            return Path(anchor).exists()
        except OSError:
            return False

    @library_mutation
    def remove_deploy_path(self, deploy_path: str | Path) -> bool:
        """按已持久化的完整路径移除部署，不依赖当前游戏配置或源文件。"""
        target = Path(deploy_path)
        if not self._path_root_available(target):
            logger.warning("部署所在磁盘/共享当前不可用，保留清理凭据: %s", target)
            return False
        return self._remove_deploy(target)

    def deployment_path_state(self, deploy_path: str | Path) -> bool | None:
        """返回完整部署路径状态；卷离线时返回 None，避免误判为已被外删。"""
        target = Path(deploy_path)
        if not self._path_root_available(target):
            return None
        return self._deploy_path_exists(target)

    @staticmethod
    def safe_deploy_name(deploy_name: str) -> str:
        """确保部署名对应 Mod 根目录中的一个直接子项。"""
        raw = str(deploy_name).strip()
        if not raw or raw in (".", "..") or Path(raw).name != raw:
            raise ValueError(f"invalid deploy name: {deploy_name!r}")
        if any(sep in raw for sep in ("/", "\\")):
            raise ValueError(f"invalid deploy name: {deploy_name!r}")
        return raw

    def deployment_paths(self, mod_id: str, deploy_name: str) -> tuple[Path, Path, Path]:
        """生成同一 Mod 的活动、停放及未完成部署路径，要求已配置部署根且身份有效。"""
        root = self._migoto_mods_dir
        if root is None:
            raise RuntimeError("3DMigoto Mods directory is not configured")
        validate_mod_id(mod_id)
        name = self.active_deployment_path(mod_id, deploy_name).name
        token = mod_id[:12]
        disabled = root / f"{MIGOTO_DISABLED_PREFIX}_{token}_{name}"
        return root / name, disabled, disabled.with_name(disabled.name + ".partial")

    def active_deployment_path(self, mod_id: str, deploy_name: str) -> Path:
        """保留可读名称，通过持久身份区分已占用名称。"""
        validate_mod_id(mod_id)
        root = self._migoto_mods_dir
        if root is None:
            raise RuntimeError("3DMigoto Mods directory is not configured")
        name = self.safe_deploy_name(deploy_name)
        for candidate in (name, f"{name[:180]}_{mod_id[:12]}", f"{name[:180]}_{mod_id}"):
            target = root / candidate
            if not fs.lexists(target):
                return target
            identity = read_deployment_identity(target) if fs.is_dir(target) else None
            if identity is not None and identity["mod_id"] == mod_id:
                return target
        raise IdentityConflict(tr("部署目录被其他内容占用：{name}", name=name))

    def scan_deployments(self, remembered_paths=(), *, force: bool = False):
        """共用带轮询缓存的部署目录清单。

        改名立即改变根时间戳；周期扫描补查标记变化，修改和重建操作使用新清单。
        """
        roots = []
        if self._migoto_mods_dir is not None:
            roots.append(self._migoto_mods_dir)
        roots.extend(Path(path).parent for path in remembered_paths if path)
        roots = list({self._path_key(root): Path(root) for root in roots}.values())
        state = scan_deployment_inventory(roots, self.deployment_inventory_snapshot(), force=force)
        self.adopt_deployment_inventory(state)
        return {location.mod_id: location for location in state.locations}

    def deployment_inventory_snapshot(self) -> DeploymentInventory:
        """返回与当前修改代次匹配的部署清单快照，缓存失效时返回空清单。"""
        if self._identity_cache_epoch != self._library_epoch:
            return DeploymentInventory()
        return DeploymentInventory(
            self._identity_cache_signature or (), self._identity_cache_time, tuple(self._identity_cache.values())
        )

    def adopt_deployment_inventory(self, state: DeploymentInventory) -> None:
        """仅供所有者线程调用，周期任务须先验证令牌。"""
        self._identity_cache_signature = state.signature
        self._identity_cache_time = state.scanned_at
        self._identity_cache = {location.mod_id: location for location in state.locations}
        self._identity_cache_epoch = self._library_epoch

    @staticmethod
    def active_name_from_parked(path: str | Path, mod_id: str) -> str:
        """剥离包含身份或普通形式的禁用前缀，恢复活动部署名称。"""
        name = Path(path).name
        prefix = f"{MIGOTO_DISABLED_PREFIX}_{mod_id[:12]}_"
        if name.casefold().startswith(prefix.casefold()):
            return name[len(prefix) :]
        prefix = f"{MIGOTO_DISABLED_PREFIX}_"
        return name[len(prefix) :] if name.casefold().startswith(prefix.casefold()) else name

    @staticmethod
    def _parked_deploy_name(mod_id: str, deploy_name: str) -> str:
        """由有效身份片段和安全部署名生成停放目录名，空身份会被拒绝。"""
        token = "".join(ch for ch in str(mod_id) if ch.isalnum())[:12]
        if not token:
            raise ValueError("mod_id is empty")
        return f"{MIGOTO_DISABLED_PREFIX}_{token}_{ModStorage.safe_deploy_name(deploy_name)}"

    @staticmethod
    def _check_deployment_identity(deploy_path: str | Path, mod_id: str) -> dict:
        """同步或移除已定位路径前重新读取其归属。"""
        validate_mod_id(mod_id)
        identity = read_deployment_identity(deploy_path)
        if identity is None or identity["mod_id"] != mod_id:
            raise IdentityConflict(tr("部署目录身份不符，保留原目录：{deploy_path}", deploy_path=deploy_path))
        return identity

    @library_mutation
    def park_deployment(self, deploy_path: str | Path, mod_id: str) -> Path | None:
        """原子停放活动部署，不覆盖任一目标。"""
        source = Path(deploy_path)
        if self._migoto_mods_dir is None or not self._path_root_available(source):
            return None
        try:
            self._check_deployment_identity(source, mod_id)
            parked = source.with_name(self._parked_deploy_name(mod_id, source.name))
        except (OSError, ValueError):
            return None
        try:
            if self._deploy_path_exists(parked):
                logger.error("停放目标已存在，拒绝覆盖: %s", parked)
                return None
            if not self._deploy_path_exists(source):
                return None
            self._rename_staged_deployment(source, parked)
            if self._deploy_path_exists(source) or not self._deploy_path_exists(parked):
                raise OSError("park invariant verification failed")
            logger.info("部署已停放: %s -> %s", source, parked)
            return parked
        except OSError as e:
            logger.warning("停放部署失败 (%s): %s", source, e)
            return None

    @library_mutation
    def restore_parked_deployment(
        self,
        parked_path: str | Path,
        deploy_name: str,
    ) -> Path | None:
        """把预设缓存目录恢复成启用名；目标被占用时拒绝覆盖。"""
        parked = Path(parked_path)
        root = self._migoto_mods_dir
        if root is None or not self._path_root_available(parked):
            return None
        target = parked.parent / self.safe_deploy_name(deploy_name)
        try:
            if self._deploy_path_exists(target):
                logger.warning("恢复停放部署时目标已存在，拒绝覆盖: %s", target)
                return None
            if not self._deploy_path_exists(parked):
                return None
            self._rename_staged_deployment(parked, target)
            if self._deploy_path_exists(parked) or not self._deploy_path_exists(target):
                raise OSError("restore invariant verification failed")
            logger.info("部署已恢复: %s -> %s", parked, target)
            return target
        except OSError as e:
            logger.warning("恢复停放部署失败 (%s): %s", parked, e)
            return None

    @staticmethod
    def _path_key(path: str | Path) -> str:
        """生成部署路径的绝对且按平台规范大小写的比较键。"""
        return os.path.normcase(os.path.abspath(str(path)))

    @staticmethod
    def _rename_staged_deployment(source: Path, target: Path, should_cancel=None) -> None:
        """等待 Windows 的短暂目录占用解除后再判定失败。"""
        deadline = time.monotonic() + 3.0
        while True:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("deployment materialization cancelled")
            if os.path.lexists(fs.native_path(target)):
                raise FileExistsError(f"deployment target is occupied: {target}")
            try:
                os.rename(fs.native_path(source), fs.native_path(target))
                return
            except OSError as error:
                # 子目录共享句柄也可能暂时阻止父目录移动；
                # 不重新解压或改用复制，仅在原子改名成功后发布。
                remaining = deadline - time.monotonic()
                if getattr(error, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                    raise
                time.sleep(min(0.05, remaining))

    @classmethod
    def _flatten_staged_deployment(cls, extracted: Path, partial: Path, should_cancel=None) -> None:
        """展开解压根目录；内外层同名时先给外层分配不与成员冲突的短暂存名。"""
        children = fs.list_children(extracted)
        names = {os.path.normcase(child.name) for child in children}
        if os.path.normcase(extracted.name) in names:
            temporary = partial / ".unpack"
            counter = 0
            while os.path.normcase(temporary.name) in names or cls._deploy_path_exists(temporary):
                counter += 1
                temporary = partial / f".unpack_{counter}"
            cls._rename_staged_deployment(extracted, temporary, should_cancel)
            extracted = temporary
        for child in children:
            cls._rename_staged_deployment(extracted / child.name, partial / child.name, should_cancel)
        fs.rmdir(extracted)

    @library_mutation
    def materialize_deployment(
        self,
        source_path: str | Path,
        mod_id: str,
        deploy_name: str,
        *,
        active: bool = True,
        progress_cb=None,
        should_cancel=None,
    ) -> Path:
        """通过临时禁用目录和原子改名创建物理部署。"""
        source = Path(source_path)
        validate_mod_id(mod_id)
        if read_source_mod_id(source) != mod_id:
            raise IdentityConflict(tr("源 Mod 与部署的 mod_id 不一致：{source}", source=source))
        active_path, disabled_path, partial_path = self.deployment_paths(mod_id, deploy_name)
        if not self._path_root_available(active_path):
            raise OSError(f"deployment root is offline: {active_path.anchor}")
        for path in (active_path, disabled_path, partial_path):
            if self._deploy_path_exists(path):
                raise FileExistsError(f"deployment invariant conflict: {path}")
        if not fs.exists(source):
            raise FileNotFoundError(str(source))

        def cancelled() -> bool:
            """读取可选取消回调并转换为布尔值，供部署复制过程检查。"""
            return bool(should_cancel is not None and should_cancel())

        # 资源库中的本体使用受支持的归档格式。
        # 不支持的源格式直接拒绝，不转换为目录复制。
        try:
            if fs.is_file(source) and is_resident_archive(source):
                from src.services.archive import ArchiveService

                extracted = ArchiveService().extract(
                    source,
                    partial_path,
                    progress_cb=progress_cb,
                    should_cancel=should_cancel,
                )
                if cancelled():
                    raise InterruptedError("deployment materialization cancelled")
                if extracted is None:
                    raise OSError(tr("解压失败，无法部署 Mod：{name}", name=source.name))
                extracted_path = Path(extracted)
                if self._path_key(extracted_path) != self._path_key(partial_path):
                    if extracted_path.parent != partial_path:
                        raise OSError(f"archive escaped partial path: {extracted_path}")
                    self._flatten_staged_deployment(extracted_path, partial_path, should_cancel)
            else:
                raise ValueError(f"unsupported deployment source: {source}")
            if cancelled():
                raise InterruptedError("deployment materialization cancelled")
            from src.core.mod_package import list_overrides

            self._strip_deployed_meta(partial_path)
            self._apply_overrides_to_deploy(source, partial_path, list_overrides(str(source)))
            from src.models.mod import Mod

            write_deployment_identity(partial_path, mod_id, Mod.compute_sha1(source))

            if not fs.is_dir(partial_path):
                raise OSError(f"partial deployment is not a directory: {partial_path}")
            with _DEPLOYMENT_PUBLISH_LOCK:
                # 解压期间同名位置可能被其他 Mod 占用，发布时重新选择名称而不覆盖。
                active_path = self.active_deployment_path(mod_id, deploy_name)
                disabled_path = active_path.with_name(self._parked_deploy_name(mod_id, active_path.name))
                target = active_path if active else disabled_path
                if fs.lexists(target):
                    raise FileExistsError(f"deployment target is occupied: {target}")
                self._rename_staged_deployment(partial_path, target, should_cancel)
            if self._deploy_path_exists(partial_path) or not self._deploy_path_exists(target):
                raise OSError("materialize invariant verification failed")
            return target
        except Exception:
            if self._deploy_path_exists(partial_path):
                self._remove_deploy(partial_path)
            raise

    # 部署 / 取消部署

    @library_mutation
    def deploy_mod(self, source_path: str | Path, progress_cb=None, deploy_name: str = "") -> Path | None:
        """通过暂存部署流程更新受管源自身的副本。"""
        source = Path(source_path)
        if self._migoto_mods_dir is None:
            logger.warning("未配置部署目录，无法部署 Mod")
            return None
        try:
            identity = read_source_mod_id(source)
            name = self._resolve_deploy_name(source, deploy_name)
            existing = self.scan_deployments(force=True).get(identity)
            if existing is not None:
                if existing.active:
                    name = existing.path.name
                if not self.undeploy_mod(source, deploy_path=existing.path, mod_id=identity):
                    raise OSError(tr("旧部署无法移除：{path}", path=existing.path))
            return self.materialize_deployment(source, identity, name, progress_cb=progress_cb)
        except Exception as error:
            logger.error("Mod 部署失败：%s", error)
            return None

    def _apply_overrides_to_deploy(
        self,
        src_body: Path,
        deploy_target: Path,
        overrides: list[str],
    ) -> None:
        """将容器 INI 覆盖文件关联到部署目录中的对应相对路径。

        依次尝试符号链接、硬链接和复制。链接成功后两侧共享修改，
        复制方式由 ini_sync 执行双向同步。
        """
        if not overrides:
            return
        from src.core.mod_package import override_path_for, read_override

        for rel in overrides:
            try:
                op = override_path_for(str(src_body), rel)
                target = Path(deploy_target) / rel
                fs.ensure_dir(target.parent)
                if op is not None and fs.is_file(op):
                    # 覆盖文件存在时建立链接，使部署与源覆盖文件共享内容。
                    self.link_ini(Path(op), target)
                    continue
                # 覆盖文件缺失时写入已读取的有效内容。
                text = read_override(str(src_body), rel)
                if text is None:
                    continue
                write_ini_preserve_link(target, text)
            except OSError as e:
                logger.warning("关联 override 到部署产物失败 (%s): %s", rel, e)

    def _capture_newer_deployed_inis(
        self,
        src_body: Path,
        deploy_target: Path,
    ) -> bool:
        """移除或替换部署前，将独立副本中的最新 INI 保存到覆盖目录。

        应用关闭期间没有同步基线，因此根据磁盘内容和修改时间判断来源。
        共享链接或内容相同时无需复制；内容不同时保留较新的版本，时间相同时采用部署内容。
        返回 False 表示必要写入失败，调用方须保留部署。
        """
        src_body = Path(fs.normal_path(src_body))
        deploy_target = Path(fs.normal_path(deploy_target))

        # junction/symlink 整目录部署：用户改部署就是改源本体，不存在独立副本丢失问题。
        if not fs.is_dir(deploy_target) or fs.is_symlink(deploy_target) or self._is_junction(deploy_target):
            return True

        from src.core.mod_package import (
            META_DIR,
            iter_effective_inis,
            override_path_for,
            resolve_ini_overrides_dir,
            write_override,
        )

        # 非容器路径没有覆盖目录，不修改源本体。
        if resolve_ini_overrides_dir(str(src_body)) is None:
            return True

        try:
            effective = {
                rel.replace("\\", "/").casefold(): text.encode("utf-8", errors="surrogateescape")
                for rel, text in iter_effective_inis(str(src_body))
            }
        except Exception as e:
            # 即使来源读取失败也继续：部署副本是即将被删除的最后一份数据，后面会将其
            # 写进 override。这里只记录诊断，不把“读不到来源”误当成“没有修改”。
            logger.warning("读取有效 ini 失败，将优先保全旧部署副本: %s", e)
            effective = {}

        failures = 0
        captured = 0
        try:
            deployed_inis = sorted(
                p for p in fs.iter_files(deploy_target) if p.suffix.lower() == ".ini" and fs.is_file(p)
            )
        except OSError as e:
            logger.warning("扫描旧部署 ini 失败 (%s): %s", deploy_target, e)
            return False

        for deployed in deployed_inis:
            try:
                rel_path = deployed.relative_to(deploy_target)
            except ValueError:
                continue
            if META_DIR in rel_path.parts:
                continue
            rel = rel_path.as_posix()

            try:
                deployed_data = fs.read_bytes(deployed)
            except OSError as e:
                logger.warning("读取旧部署 ini 失败 (%s): %s", deployed, e)
                failures += 1
                continue

            if effective.get(rel.casefold()) == deployed_data:
                continue

            # 找到当前来源的磁盘时间。已有 override 最精确；文件夹本体可定位到单文件；
            # 归档本体则以归档 mtime 作为整体版本时间。
            reference: Path | None = None
            op = override_path_for(str(src_body), rel)
            if op is not None and fs.is_file(op):
                reference = Path(op)
            elif fs.is_dir(src_body) and fs.is_file(src_body / rel_path):
                reference = src_body / rel_path
            elif fs.is_file(src_body):
                reference = src_body

            try:
                deployed_mtime = os.stat(fs.native_path(deployed)).st_mtime_ns
            except OSError:
                deployed_mtime = 0
            try:
                reference_mtime = os.stat(fs.native_path(reference)).st_mtime_ns if reference else -1
            except OSError:
                reference_mtime = -1

            if reference_mtime > deployed_mtime:
                logger.info("旧部署 ini 与来源不同，但来源更新，保留来源: %s", rel)
                continue

            text = deployed_data.decode("utf-8", errors="surrogateescape")
            if write_override(str(src_body), rel, text):
                effective[rel.casefold()] = deployed_data
                captured += 1
                logger.info("已回收旧部署 ini 修改: %s", rel)
            else:
                failures += 1

        if captured:
            logger.info("旧部署 ini 回收完成: %s（%d 个）", deploy_target, captured)
        return failures == 0

    def link_ini(self, override_path: Path, deploy_path: Path) -> str:
        """将部署 INI 关联到覆盖文件，并返回实际采用的方式。

        优先创建符号链接，其次尝试同卷硬链接，均不可用时复制内容并由同步器维持一致。
        创建前移除现有目标以保持幂等。链接建立后须就地写入，避免原子替换破坏链接关系。
        """
        override_path = Path(override_path)
        deploy_path = Path(deploy_path)
        # 创建链接前移除现有目标。
        try:
            if fs.lexists(deploy_path):
                fs.unlink(deploy_path)
        except OSError:
            try:
                if fs.exists(deploy_path):
                    fs.unlink(deploy_path)
            except OSError:
                pass
        # ① symlink 优先
        try:
            os.symlink(fs.native_path(override_path), fs.native_path(deploy_path))
            logger.debug("ini 已软链接: %s -> %s", deploy_path, override_path)
            return "symlink"
        except (OSError, NotImplementedError, AttributeError):
            pass
        # ② hardlink 回退（同卷）
        try:
            os.link(fs.native_path(override_path), fs.native_path(deploy_path))
            logger.debug("ini 已硬链接: %s == %s", deploy_path, override_path)
            return "hardlink"
        except OSError:
            pass
        # 链接不可用时复制内容；使用就地写入以保留可能存在的链接关系。
        try:
            data = fs.read_bytes(override_path)
            write_ini_preserve_link(deploy_path, data.decode("utf-8", errors="surrogateescape"))
            logger.debug("ini 链接不可用，回退复制: %s", deploy_path)
        except OSError as e:
            logger.warning("INI 复制回退失败 (%s): %s", deploy_path, e)
        return "copy"

    def _strip_deployed_meta(self, target: Path) -> None:
        """移除真实部署目录中的元数据。

        归档整体解压后需单独清理元数据成员；目录链接与源共享内容，不能在此删除其元数据。
        """
        try:
            from src.core.mod_package import META_DIR

            meta = Path(target) / META_DIR
            if fs.is_dir(meta) and not fs.is_symlink(meta):
                fs.rmtree(meta, ignore_errors=True)
        except Exception as e:
            logger.warning("清理部署产物 sidecar 失败: %s", e)

    @library_mutation
    def undeploy_mod(
        self,
        source_path: str | Path,
        deploy_name: str = "",
        *,
        capture_changes: bool = True,
        deploy_path: str | Path | None = None,
        mod_id: str | None = None,
    ) -> bool:
        """从 3DMigoto Mods 目录移除 Mod 部署 (禁用)

        源 Mod 文件本身不受影响。

        参数：
            source_path: 源 Mod 路径 (用于确定在 3DMigoto 中的部署名称)
            mod_id: 调用方保存的 UUID；省略时从源 .d3dxmod 读取，绝不按名称认领。
                源已缺失的清理必须传入已保存的 UUID。

        返回：
            成功返回 True
        """
        src = Path(source_path)
        if deploy_path is not None:
            deploy_target = Path(deploy_path)
            if not self._path_root_available(deploy_target):
                logger.warning("部署所在磁盘/共享当前不可用，暂缓移除: %s", deploy_target)
                return False
        elif not self._migoto_mods_dir:
            logger.info("未设置 3DMigoto 目录，无需取消部署")
            return True
        else:
            # 必须优先使用稳定 deploy_name；源已被外删时不能再靠 is_file 推断类型。
            stable_deploy_name = self._resolve_deploy_name(src, deploy_name)
            deploy_target = self._migoto_mods_dir / stable_deploy_name

        if not self._deploy_path_exists(deploy_target):
            logger.info("3DMigoto 中不存在该 Mod 部署，无需移除: %s", deploy_target)
            return True

        try:
            identity = read_source_mod_id(src) if mod_id is None else validate_mod_id(mod_id)
            self._check_deployment_identity(deploy_target, identity)
        except (OSError, ValueError) as error:
            logger.error("部署身份无法确认，已取消移除 (%s): %s", deploy_target, error)
            return False

        if capture_changes and not self._capture_newer_deployed_inis(src, deploy_target):
            logger.error("部署 ini 回收失败，为防止数据丢失已取消移除: %s", deploy_target)
            return False
        return self._remove_deploy(deploy_target, mod_id=identity)

    # 源目录操作

    @library_mutation
    def copy_mod(
        self,
        src: str | Path,
        dst_parent: str | Path,
        *,
        checkpoint=None,
        should_cancel=None,
        progress_cb=None,
    ) -> Path | None:
        """在锁内为目标分配唯一名称并复制 Mod。

        重名时追加序号，保留原始扩展名，避免并发名称检查与复制之间发生覆盖。
        """
        src = Path(src)
        dst_parent = Path(dst_parent)

        if not fs.exists(src):
            logger.error("源 Mod 不存在: %s", src)
            return None

        fs.ensure_dir(dst_parent)

        # 全程锁内：分配唯一目标 + 实际复制
        with _FS_OP_LOCK:
            dst = dst_parent / src.name

            if _is_name_taken(dst):
                # 保留原始扩展名（仅文件有 suffix）
                suffix = src.suffix if fs.is_file(src) else ""
                base = src.stem if fs.is_file(src) else src.name
                counter = 1
                while _is_name_taken(dst):
                    dst = dst_parent / f"{base}_{counter}{suffix}"
                    counter += 1

            try:
                fs.copy_path(
                    src,
                    dst,
                    checkpoint=checkpoint,
                    should_cancel=should_cancel,
                    progress_cb=progress_cb,
                )
                logger.info("Mod 已复制到 library: %s -> %s", src, dst)
                return dst
            except OSError as e:
                logger.error("复制 Mod 失败: %s", e)
                # 清理可能的半成品
                try:
                    if fs.is_dir(dst):
                        fs.rmtree(dst, ignore_errors=True)
                    elif fs.is_file(dst):
                        fs.unlink(dst, missing_ok=True)
                except OSError:
                    pass
                return None

    @library_mutation
    def copy_mod_into_container(
        self,
        src: str | Path,
        dst_parent: str | Path,
        *,
        content_hash: str,
        source_name: str = "",
        mod_id: str | None = None,
        imported_at: str | None = None,
        checkpoint=None,
        should_cancel=None,
        progress_cb=None,
        before_create=None,
        on_created=None,
        on_failed_cleanup=None,
    ) -> Path | None:
        """将 Mod 本体复制到新容器并返回本体路径。

        容器包含身份标记及同基名的文件夹或归档；重名时同时调整容器和本体名称。
        目标名称预留和创建在锁内完成，避免并发覆盖。
        source_name 可提供原始文件名；仅使用其安全基名，源路径和归档后缀保持不变。
        可选创建与失败清理回调由插件导入提供，用于为新容器保存可恢复的清理凭据。
        """
        src = Path(src)
        dst_parent = Path(dst_parent)
        if not fs.exists(src):
            logger.error("源 Mod 不存在: %s", src)
            return None

        fs.ensure_dir(dst_parent)
        is_file = fs.is_file(src)
        base = src.stem if is_file else src.name
        suffix = src.suffix if is_file else ""
        if source_name:
            from src.core.mod_manager import _sanitize_folder_name

            original = PureWindowsPath(source_name.strip())
            candidate = _sanitize_folder_name(original.stem, fallback="")
            is_reserved = getattr(ntpath, "isreserved", None)
            reserved = is_reserved(candidate) if is_reserved is not None else PureWindowsPath(candidate).is_reserved()
            if candidate and not reserved:
                base = candidate

        with _FS_OP_LOCK:
            # 分配唯一容器名称，同时检查同名目录和归档。
            container = dst_parent / base
            if _is_name_taken(container):
                counter = 1
                while _is_name_taken(dst_parent / f"{base}_{counter}"):
                    counter += 1
                container = dst_parent / f"{base}_{counter}"
                base = container.name

            body = container / f"{base}{suffix}"
            if before_create is not None:
                before_create(container)
            created = False
            try:
                fs.ensure_dir(container, exist_ok=False)
                created = True
                if on_created is not None:
                    on_created(container, body.name)
                fs.copy_path(
                    src,
                    body,
                    checkpoint=checkpoint,
                    should_cancel=should_cancel,
                    progress_cb=progress_cb,
                )
            except OSError as e:
                logger.error("复制 Mod 到容器失败: %s", e)
                if on_failed_cleanup is not None:
                    if created:
                        on_failed_cleanup(container)
                else:
                    fs.rmtree(container, ignore_errors=True)
                return None

        try:
            from src.core.mod_package import write_container_marker

            marker_written = write_container_marker(
                container,
                body.name,
                content_hash,
                mod_id=mod_id,
                imported_at=imported_at or datetime.now().isoformat(),
            )
        except Exception as e:
            logger.error("写容器 marker 失败，已回滚导入产物: %s", e)
            marker_written = False
        if not marker_written:
            if on_failed_cleanup is not None:
                on_failed_cleanup(container)
            else:
                fs.rmtree(container, ignore_errors=True)
            return None
        logger.info("Mod 已复制进容器: %s -> %s", src, body)
        return body

    @staticmethod
    def source_delete_target(source_path: str | Path) -> Path:
        """返回本体路径或其完整受管容器。"""
        path = Path(fs.normal_path(source_path))
        parent = path.parent
        if fs.is_file(parent / MOD_CONTAINER_MARKER):
            return parent
        if fs.is_dir(path) and fs.is_file(path / MOD_CONTAINER_MARKER):
            return path
        return path

    def ensure_source_recycle_compatible(self, source_path: str | Path) -> None:
        """回收站接口无法处理路径树时，在撤销部署前报告失败。"""
        target = self.source_delete_target(source_path)
        if fs.exists(target):
            fs.ensure_recycle_bin_compatible(target)

    @library_mutation
    def delete_source(
        self,
        source_path: str | Path,
        *,
        delete_method: str = "permanent",
    ) -> bool:
        """删除源 Mod，可选择永久删除或移入回收站。

        源位于容器中时一并删除整个容器及其身份、元数据，避免留下不完整目录。
        """
        if delete_method not in ("permanent", "recycle_bin"):
            raise ValueError(tr("未知 Mod 删除方式: {delete_method}", delete_method=delete_method))

        target = self.source_delete_target(source_path)
        if not fs.exists(target):
            logger.warning("要删除的 Mod 不存在: %s", target)
            return True

        if delete_method == "recycle_bin":
            try:
                fs.move_to_trash(target)
                logger.info("Mod 已移入回收站: %s", target)
                return True
            except fs.RecycleBinError as e:
                logger.warning("Mod 无法移入回收站 (%s): %s", target, e)
                raise

        try:
            if fs.is_file(target):
                fs.unlink(target)
                logger.info("Mod 源文件已删除: %s", target)
            else:
                fs.rmtree(target)
                logger.info("Mod 源文件夹已删除: %s", target)
            return True
        except OSError as e:
            logger.error("删除 Mod 源失败: %s", e)
            return False

    @library_mutation
    def delete_mod(
        self,
        source_path: str | Path,
        deploy_name: str = "",
        *,
        delete_method: str = "permanent",
        mod_id: str | None = None,
    ) -> bool:
        """删除 Mod: 从 3DMigoto 移除部署 + 删除源文件夹"""
        if delete_method not in ("permanent", "recycle_bin"):
            raise ValueError(tr("未知 Mod 删除方式: {delete_method}", delete_method=delete_method))
        if delete_method == "recycle_bin":
            self.ensure_source_recycle_compatible(source_path)
        if not self.undeploy_mod(source_path, deploy_name=deploy_name, mod_id=mod_id):
            return False
        return self.delete_source(source_path, delete_method=delete_method)

    @library_mutation
    def move_mod(self, src: str | Path, dst_parent: str | Path) -> Path | None:
        """移动 Mod 到新位置"""
        src = Path(src)
        dst_parent = Path(dst_parent)

        if not fs.exists(src):
            logger.error("源 Mod 不存在: %s", src)
            return None

        fs.ensure_dir(dst_parent)
        dst = dst_parent / src.name

        if fs.exists(dst):
            counter = 1
            while fs.exists(dst):
                dst = dst_parent / f"{src.stem}_{counter}{src.suffix}"
                counter += 1

        try:
            shutil.move(fs.native_path(src), fs.native_path(dst))
            logger.info("Mod 已移动: %s -> %s", src, dst)
            return dst
        except OSError as e:
            logger.error("移动 Mod 失败: %s", e)
            return None

    # 扫描

    def scan_library_directory(
        self,
        library_root: str | Path,
        *,
        invalid_metadata: list[str] | None = None,
        scope_filter: Callable[[Path], bool] | None = None,
        marker_reader=None,
    ) -> list[dict]:
        """扫描受管库的类别及 Mod 直接成员。

        scope_filter 限定与变化路径相关的分支，供目录事件触发的局部对账使用。
        返回包含 folder_name、path、character_folder 的记录列表。
        """
        results = []
        root = Path(library_root)
        if not fs.is_dir(root):
            return results

        with os.scandir(fs.native_path(root)) as entries:
            char_entries = sorted(entries, key=lambda child: os.path.normcase(child.name))
        for char_entry in char_entries:
            if char_entry.name.startswith(".") or not char_entry.is_dir():
                continue
            char_dir = Path(fs.normal_path(char_entry.path))
            if scope_filter is not None and not scope_filter(char_dir):
                continue
            with os.scandir(fs.native_path(char_dir)) as entries:
                children = sorted(entries, key=lambda child: os.path.normcase(child.name))
            for child in children:
                if child.name.startswith("."):
                    continue
                entry = Path(fs.normal_path(child.path))
                if scope_filter is not None and not scope_filter(entry):
                    continue
                if child.is_dir():
                    # 发现容器后返回本体路径和容器名称，避免将外层容器重复登记为资源。
                    from src.core.mod_package import (
                        InvalidManagedMetadataError,
                        read_container_marker,
                    )

                    try:
                        marker = (marker_reader or read_container_marker)(entry)
                    except InvalidManagedMetadataError as e:
                        logger.warning("跳过无效 Mod 容器 %s: %s", entry, e)
                        if invalid_metadata is not None and str(entry) not in invalid_metadata:
                            invalid_metadata.append(str(entry))
                        continue
                    if marker is not None:
                        body = entry / marker["body"]
                        results.append(
                            {
                                "folder_name": entry.name,
                                "path": str(body),
                                "character_folder": char_dir.name,
                                "content_hash": marker["content_hash"],
                            }
                        )
                        continue
                    results.append(
                        {
                            "folder_name": entry.name,
                            "path": str(entry),
                            "character_folder": char_dir.name,
                        }
                    )
                elif child.is_file() and is_resident_archive(entry):
                    results.append(
                        {
                            "folder_name": entry.stem,
                            "path": str(entry),
                            "character_folder": char_dir.name,
                        }
                    )
        return results

    # 工具方法

    def is_deployed(self, source_path: str | Path, deploy_name: str = "", *, mod_id: str = "") -> bool:
        """核对资源的持久身份，不依赖显示名。"""
        if not self._migoto_mods_dir:
            return False
        identity = mod_id or read_source_mod_id(source_path)
        location = self.scan_deployments().get(validate_mod_id(identity))
        return bool(location and location.active)

    # 内部方法

    @staticmethod
    def _is_junction(path: Path) -> bool:
        """检查路径是否是 NTFS junction"""
        isjunction = getattr(os.path, "isjunction", None)
        if callable(isjunction):
            try:
                return bool(isjunction(fs.native_path(path)))
            except OSError:
                pass
        try:
            st = os.lstat(fs.native_path(path))
            attrs = getattr(st, "st_file_attributes", 0)
            return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)) and not fs.is_symlink(path)
        except (OSError, ValueError, AttributeError):
            return False

    @classmethod
    def _remove_deploy(cls, deploy_path: Path, *, mod_id: str | None = None) -> bool:
        """移除部署；身份约束随每次重试重新验证，显式整目录清理可不传约束。"""
        last_error: OSError | None = None
        removing_empty_dirs = False

        def _onerror(func, path, _exc_info):
            """删除失败时重新核对部署身份，解除只读属性后重试原删除操作。"""
            try:
                if mod_id is not None and not removing_empty_dirs:
                    cls._check_deployment_identity(deploy_path, mod_id)
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                raise

        def _unlink_or_rmdir(path: Path, *, directory: bool = False) -> None:
            """按对象类型删除文件或空目录，忽略已缺失目标，其余错误进入只读重试。"""
            remove = os.rmdir if directory else os.unlink
            native = fs.native_path(path)
            try:
                remove(native)
            except FileNotFoundError:
                return
            except OSError:
                _onerror(remove, native, None)

        def _remove_child(path: Path) -> None:
            """按链接、联接、普通目录或文件类型移除子项，避免递归进入链接目标。"""
            if fs.is_symlink(path):
                _unlink_or_rmdir(path)
            elif cls._is_junction(path):
                _unlink_or_rmdir(path, directory=True)
            elif fs.is_dir(path):
                fs.rmtree(path, onerror=_onerror)
            else:
                _unlink_or_rmdir(path)

        def _remove_owned_directory(expected_id: str) -> None:
            """保留身份标记直至其他内容删除完毕，复核身份后删除根目录。

            根目录删除失败时仅在对象身份仍一致的条件下恢复标记，以支持后续安全重试。"""
            nonlocal removing_empty_dirs
            directory_stat = os.stat(fs.native_path(deploy_path), follow_symlinks=False)
            marker = deploy_path / DEPLOYMENT_IDENTITY_FILE
            # 仍有内容可能删除失败时保留身份凭据。
            for child in fs.list_children(deploy_path):
                if child.name.casefold() != DEPLOYMENT_IDENTITY_FILE.casefold():
                    _remove_child(child)
            identity = cls._check_deployment_identity(deploy_path, expected_id)
            try:
                _unlink_or_rmdir(marker)
                removing_empty_dirs = True
                _unlink_or_rmdir(deploy_path, directory=True)
            except OSError:
                # Windows 上空目录仍可能被占用，只恢复原目录，且不覆盖已有标记。
                try:
                    current_stat = os.stat(fs.native_path(deploy_path), follow_symlinks=False)
                    if os.path.samestat(directory_stat, current_stat):
                        try:
                            with open(fs.native_path(marker), "x", encoding="utf-8") as stream:
                                json.dump(identity, stream, ensure_ascii=False)
                                stream.flush()
                                os.fsync(stream.fileno())
                        except FileExistsError:
                            pass
                except OSError as restore_error:
                    logger.error("删除未完成且身份凭据恢复失败 (%s): %s", deploy_path, restore_error)
                raise
            finally:
                removing_empty_dirs = False

        # Windows 杀毒/游戏句柄释放可能有几十毫秒延迟；短重试解决瞬时占用，持续占用则
        # 返回 False，由上层保留 Mod 记录并在定时器/下次启动继续清理。
        for attempt in range(3):
            try:
                if not cls._deploy_path_exists(deploy_path):
                    return True
                if mod_id is not None:
                    cls._check_deployment_identity(deploy_path, mod_id)
                if fs.is_symlink(deploy_path):
                    fs.unlink(deploy_path)
                elif cls._is_junction(deploy_path):
                    fs.rmdir(deploy_path)  # 仅移除重解析点，不遍历其目标目录。
                elif fs.is_dir(deploy_path):
                    if mod_id is None:
                        fs.rmtree(deploy_path, onerror=_onerror)
                    else:
                        _remove_owned_directory(mod_id)
                else:
                    fs.unlink(deploy_path, missing_ok=True)
                if not cls._deploy_path_exists(deploy_path):
                    logger.info("已移除 3DMigoto 部署: %s", deploy_path)
                    return True
            except IdentityConflict as e:
                logger.error("部署身份已变更，停止移除 (%s): %s", deploy_path, e)
                return False
            except OSError as e:
                last_error = e
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))

        logger.error("移除部署失败: %s - %s", deploy_path, last_error or "路径仍存在")
        return False
