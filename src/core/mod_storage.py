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
