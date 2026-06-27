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
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
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
            p.mkdir(parents=True, exist_ok=True)
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
        return os.path.lexists(str(path)) or path.is_symlink() or cls._is_junction(path)

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
            if not os.path.lexists(target):
                return target
            identity = read_deployment_identity(target) if target.is_dir() else None
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
            os.replace(str(source), str(parked))
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
            os.replace(str(parked), str(target))
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
        deadline = time.monotonic() + 1.0
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
                    for child in list(extracted_path.iterdir()):
                        self._rename_staged_deployment(child, partial_path / child.name, should_cancel)
                    extracted_path.rmdir()
            else:
                raise ValueError(f"unsupported deployment source: {source}")
            if cancelled():
                raise InterruptedError("deployment materialization cancelled")
            from src.core.mod_package import list_overrides

            self._strip_deployed_meta(partial_path)
            self._apply_overrides_to_deploy(source, partial_path, list_overrides(str(source)))
            from src.models.mod import Mod

            write_deployment_identity(partial_path, mod_id, Mod.compute_sha1(source))

            if not partial_path.is_dir():
                raise OSError(f"partial deployment is not a directory: {partial_path}")
            with _DEPLOYMENT_PUBLISH_LOCK:
                # 解压期间同名位置可能被其他 Mod 占用，发布时重新选择名称而不覆盖。
                active_path = self.active_deployment_path(mod_id, deploy_name)
                disabled_path = active_path.with_name(self._parked_deploy_name(mod_id, active_path.name))
                target = active_path if active else disabled_path
                if os.path.lexists(target):
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
                target.parent.mkdir(parents=True, exist_ok=True)
                if op is not None and Path(op).is_file():
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
        src_body = Path(src_body)
        deploy_target = Path(deploy_target)

        # junction/symlink 整目录部署：用户改部署就是改源本体，不存在独立副本丢失问题。
        if not deploy_target.is_dir() or deploy_target.is_symlink() or self._is_junction(deploy_target):
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
            deployed_inis = sorted(p for p in deploy_target.rglob("*") if p.is_file() and p.suffix.lower() == ".ini")
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
                deployed_data = deployed.read_bytes()
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
            if op is not None and Path(op).is_file():
                reference = Path(op)
            elif src_body.is_dir() and (src_body / rel_path).is_file():
                reference = src_body / rel_path
            elif src_body.is_file():
                reference = src_body

            try:
                deployed_mtime = deployed.stat().st_mtime_ns
            except OSError:
                deployed_mtime = 0
            try:
                reference_mtime = reference.stat().st_mtime_ns if reference else -1
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
            if deploy_path.is_symlink() or deploy_path.exists():
                deploy_path.unlink()
        except OSError:
            try:
                if deploy_path.exists():
                    deploy_path.unlink()
            except OSError:
                pass
        # ① symlink 优先
        try:
            os.symlink(str(override_path), str(deploy_path))
            logger.debug("ini 已软链接: %s -> %s", deploy_path, override_path)
            return "symlink"
        except (OSError, NotImplementedError, AttributeError):
            pass
        # ② hardlink 回退（同卷）
        try:
            os.link(str(override_path), str(deploy_path))
            logger.debug("ini 已硬链接: %s == %s", deploy_path, override_path)
            return "hardlink"
        except OSError:
            pass
        # 链接不可用时复制内容；使用就地写入以保留可能存在的链接关系。
        try:
            data = override_path.read_bytes()
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
            if meta.is_dir() and not meta.is_symlink():
                shutil.rmtree(str(meta), ignore_errors=True)
        except Exception as e:
            logger.warning("清理部署产物 sidecar 失败: %s", e)
