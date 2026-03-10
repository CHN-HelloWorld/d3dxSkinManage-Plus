"""Mod 数据模型"""

from __future__ import annotations

import hashlib
import os
import uuid
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.paths import resolve_stored_path, to_stored_path

# .d3dxmeta sidecar 目录名（与 src/core/mod_package.META_DIR 同值）。
# 内容哈希必须忽略它：sidecar 是「随文件走的元数据」，不属于 Mod 内容本体，
# 否则写入/更新 sidecar 会改变 hash，破坏去重与部署键的稳定性。
_SIDECAR_DIR_NAME = ".d3dxmeta"
# 容器 marker 名（与 src/utils/constants.MOD_CONTAINER_MARKER 同值）。同样必须
# 容器标记不属于本体内容，计算哈希时排除，保持复制和重新导入后的身份一致。
_CONTAINER_MARKER_NAME = ".d3dxmod"
# 使用 1 MiB 缓冲减少读取与摘要更新次数，分块大小不影响最终摘要。
_HASH_CHUNK = 1 << 20
# 内容身份的字段分隔符：rel / size / crc32 三段之间与末尾各插一个 NUL。NUL 不可能出现在
# 成员名里，故「a\0b」与「ab」不会拼出同一串字节——没有它，成员名与数字的边界就有歧义，
# 不同内容可能撞出同一摘要。
_IDENTITY_SEP = b"\x00"


def _is_under_sidecar(arcname: str) -> bool:
    """ZIP/相对成员名是否属于应从内容哈希排除的元数据（.d3dxmeta 目录 或 .d3dxmod marker）。"""
    segs = arcname.replace("\\", "/").split("/")
    return _SIDECAR_DIR_NAME in segs or _CONTAINER_MARKER_NAME in segs


class ModStatus(Enum):
    """Mod 状态"""

    ENABLED = "enabled"
    DISABLED = "disabled"


class AgeRating(Enum):
    """年龄分级"""

    G = "G"  # 全年龄
    P = "P"  # 轻微敏感
    R = "R"  # 限制级
    X = "X"  # 成人级


@dataclass
class ModInfo:
    """Mod 元数据信息"""

    author: str = ""
    description: str = ""
    version: str = ""
    url: str = ""
    age_rating: AgeRating = AgeRating.G
    tags: list[str] = field(default_factory=list)
    custom_data: dict = field(default_factory=dict)


@dataclass
class Mod:
    """Mod 数据模型

    核心实体，表示一个已导入的 Mod。

    属性：
        mod_id: 稳定的本地身份，与内容指纹无关
        name: 显示名称
        folder_name: 实际文件夹名称
        path: Mod 文件所在路径
        status: 启用/禁用状态
        node_id: 关联角色 ID
        info: Mod 元数据
        preview_images: 预览图路径列表
        imported_at: 首次纳管时间
    """

    game_id: str = ""
    name: str = ""
    folder_name: str = ""
    path: str = ""
    mod_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # 本机私有运行状态：最近一次实际部署成功的完整路径。独立于 ModInfo，结构上不会进入
    # “附带元数据”分享信封；用于部署根变化、离线磁盘和外部删除后的精确清理。
    deployed_path: str = ""
    content_hash: str = ""
    status: ModStatus = ModStatus.DISABLED
    node_id: str = ""
    info: ModInfo = field(default_factory=ModInfo)
    preview_images: list[str] = field(default_factory=list)
    imported_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def is_enabled(self) -> bool:
        """是否已启用"""
        return self.status == ModStatus.ENABLED

    @property
    def display_name(self) -> str:
        """显示名称（优先自定义名称，否则使用文件夹名）"""
        return self.name if self.name else self.folder_name

    @property
    def primary_preview(self) -> str | None:
        """主预览图路径"""
        return self.preview_images[0] if self.preview_images else None

    def toggle(self) -> None:
        """切换启用状态"""
        if self.status == ModStatus.ENABLED:
            self.status = ModStatus.DISABLED
        else:
            self.status = ModStatus.ENABLED

    def enable(self) -> None:
        """启用 Mod"""
        self.status = ModStatus.ENABLED

    def disable(self) -> None:
        """禁用 Mod"""
        self.status = ModStatus.DISABLED

    def remember_deployment(self, path: str | Path) -> None:
        """在当前对象中记录实际部署的绝对路径，供持久化及后续副本清理使用。"""
        self.deployed_path = str(Path(path).resolve())

    def forget_deployment(self) -> None:
        """部署已验证移除后清除本机部署定位。"""
        self.deployed_path = ""

    @staticmethod
    def _strip_single_top(names: list[str]) -> dict[str, str]:
        """将归档成员名映射到解压后的本体相对路径，移除共同的顶层包装目录。

        路径规则与 ArchiveService.extract 一致，使目录和对应归档产生相同的内容指纹，
        保持重复检测、资源重定位和更新检查的身份一致性。
        """
        normalized = [n.replace("\\", "/") for n in names]
        tops = {n.split("/")[0] for n in normalized if n.split("/")[0]}
        if len(tops) == 1 and all("/" in n for n in normalized):
            top = tops.pop() + "/"
            return {raw: norm[len(top) :] for raw, norm in zip(names, normalized)}
        return dict(zip(names, normalized))

    @staticmethod
    def _digest_entries(
        entries: list[tuple[str, int, int]],
        *,
        checkpoint=None,
    ) -> str:
        """将成员三元组按路径排序，生成四十位小写 SHA-1 指纹。

        每项由路径、大小和八位十六进制 CRC-32 组成，字段及成员之间用 NUL 分隔。
        成员表读取和实际内容读取共用此入口，确保相同资源产生相同指纹。
        """
        sha1 = hashlib.sha1()
        # 按整个三元组排序而非只按 rel：畸形包可能有重复成员名，只按 rel 排序时
        # Python 的稳定排序把并列项的次序交给输入顺序（后端返回序不保证稳定）
        # → 同一个包两次算出不同摘要。
        for rel, size, crc in sorted(entries):
            if checkpoint is not None and not checkpoint():
                raise InterruptedError("hash cancelled")
            sha1.update(rel.encode("utf-8"))
            sha1.update(_IDENTITY_SEP)
            sha1.update(str(int(size)).encode("ascii"))
            sha1.update(_IDENTITY_SEP)
            sha1.update(("%08x" % (int(crc) & 0xFFFFFFFF)).encode("ascii"))
            sha1.update(_IDENTITY_SEP)
        return sha1.hexdigest()

    @staticmethod
    def _effective_triples(
        entries: list[tuple[str, int, int]],
    ) -> list[tuple[str, int, int]]:
        """排除元数据后规范化归档成员的路径、长度和校验和。

        须先排除管理成员，再判断并移除唯一顶层目录，使归档与文件夹使用一致的内容身份。
        """
        effective = [e for e in entries if not _is_under_sidecar(e[0])]
        rel_by_raw = Mod._strip_single_top([name for name, _s, _c in effective])
        return [(rel_by_raw[name], size, crc) for name, size, crc in effective]

    @staticmethod
    def _crc_of_stream(read_chunk, *, checkpoint=None) -> tuple[int, int]:
        """分块消费一个字节流，返回 ``(size, crc32)``。``read_chunk`` 返回空串即结束。"""
        crc = 0
        size = 0
        while True:
            if checkpoint is not None and not checkpoint():
                raise InterruptedError("hash cancelled")
            chunk = read_chunk()
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
        return size, crc & 0xFFFFFFFF
