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

    @staticmethod
    def _digest_raw_bytes(path: Path, *, checkpoint=None) -> str:
        """无法读取成员清单时计算整个文件的字节摘要。

        该结果仅用于区分不可解析的输入，不承诺与解压目录具有相同身份。
        """
        sha1 = hashlib.sha1()
        with open(fs.native_path(path), "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                if checkpoint is not None and not checkpoint():
                    raise InterruptedError("hash cancelled")
                sha1.update(chunk)
        return sha1.hexdigest()

    @staticmethod
    def compute_sha1(file_path: str | Path, *, checkpoint=None) -> str:
        """计算 Mod 内容指纹，返回四十位小写 SHA-1。

        优先读取归档成员的路径、解压后大小和 CRC-32；目录或无法获取摘要的归档
        通过读取成员内容计算同一三元组，并统一交给 _digest_entries。
        归档与解压目录采用相同公式，容器元数据不参与指纹。

        指纹用于内容识别，不是抵抗恶意构造的密码学完整性证明。
        不返回空字符串；checkpoint 返回 False 时抛出 InterruptedError。
        """
        path = Path(file_path)
        if checkpoint is not None and not checkpoint():
            raise InterruptedError("hash cancelled")

        if fs.is_file(path):
            if path.suffix.lower() in (".zip", ".7z", ".rar"):
                from src.services.archive import ArchiveService

                # 优先仅读取成员表；后端缺失、数据损坏或校验信息缺失时返回 None。
                entries = ArchiveService().list_entries(path)
                if entries is None:
                    entries = Mod._archive_entries_by_bytes(path, path.suffix.lower(), checkpoint=checkpoint)
                if entries is not None:
                    return Mod._digest_entries(Mod._effective_triples(entries), checkpoint=checkpoint)
                # 连成员表都枚举不出来（包已损坏）→ 退化为原始字节摘要
            return Mod._digest_raw_bytes(path, checkpoint=checkpoint)

        if fs.is_dir(path):
            # 目录逐文件计算 CRC，与归档使用相同的摘要公式。
            return Mod._digest_entries(Mod._folder_triples(path, checkpoint=checkpoint), checkpoint=checkpoint)

        # 路径不存在：空成员表 → 空内容摘要（40 位合法但等价于空），
        # 由调用方按「内容哈希为空」处置（重建会把它记成 unreadable 而不是落库）。
        return Mod._digest_entries([])

    @staticmethod
    def _folder_triples(
        path: Path,
        *,
        checkpoint=None,
    ) -> list[tuple[str, int, int]]:
        """收集目录成员的相对路径、大小和校验值。

        路径相对于目录本体，统一排序由 _digest_entries 负责。
        """
        triples: list[tuple[str, int, int]] = []
        for root, dirs, files in fs.walk(path):
            if checkpoint is not None and not checkpoint():
                raise InterruptedError("hash cancelled")
            # 不下钻 .d3dxmeta —— sidecar 不计入内容哈希
            if _SIDECAR_DIR_NAME in dirs:
                dirs.remove(_SIDECAR_DIR_NAME)
            for fname in files:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, path).replace("\\", "/")
                if _is_under_sidecar(rel):
                    continue  # 跳过本体中的管理标记，与归档成员过滤规则一致。
                try:
                    with open(fs.native_path(fpath), "rb") as f:
                        size, crc = Mod._crc_of_stream(lambda f=f: f.read(_HASH_CHUNK), checkpoint=checkpoint)
                except OSError:
                    # 读不出的单个文件不该毁掉整次哈希，但要留下可区分的稳定痕迹。
                    # size=-1 不可能与真实成员冲突（真实 size ≥ 0）。
                    size, crc = -1, zlib.crc32(b"<unreadable>")
                triples.append((rel, size, crc))
        return triples

    @staticmethod
    def _archive_entries_by_bytes(
        path: Path,
        suffix: str,
        *,
        checkpoint=None,
    ) -> list[tuple[str, int, int]] | None:
        """读取成员字节以计算相对路径、长度和 CRC 三元组。

        无法从归档目录取得完整校验信息时使用此路径，与目录读取结果交给同一摘要函数。
        成员名保持归档原样，顶层目录由后续规范化统一处理；任一内容不可读时返回 None。
        """
        try:
            if suffix == ".zip":
                import zipfile

                out: list[tuple[str, int, int]] = []
                with zipfile.ZipFile(fs.native_path(path), "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir() or _is_under_sidecar(info.filename):
                            continue
                        with zf.open(info, "r") as f:
                            size, crc = Mod._crc_of_stream(lambda f=f: f.read(_HASH_CHUNK), checkpoint=checkpoint)
                        out.append((info.filename, size, crc))
                return out

            from src.services.archive import ArchiveService

            svc = ArchiveService()
            names = [n for n in svc.list_contents(path) if not n.endswith("/") and not _is_under_sidecar(n)]
            if not names:
                # 空清单无法区分空归档与读取失败，不据此构建身份。
                return None
            if suffix == ".7z":
                # 使用 WriterFactory 一次流式读取 7z 成员，避免固实归档为每个文件重复解码。
                return Mod._7z_entries_by_bytes(path, names, checkpoint=checkpoint)
            out = []
            for n in names:
                if checkpoint is not None and not checkpoint():
                    raise InterruptedError("hash cancelled")
                data = svc.read_file(path, n)
                if data is None:
                    return None  # 成员读取失败时不生成不完整的内容身份。
                out.append((n, len(data), zlib.crc32(data) & 0xFFFFFFFF))
            return out
        except InterruptedError:
            raise
        except Exception:
            return None

    @staticmethod
    def _7z_entries_by_bytes(
        path: Path,
        names: list[str],
        *,
        checkpoint=None,
    ) -> list[tuple[str, int, int]] | None:
        """一次解码全部 7z 成员，返回路径、长度和 CRC 三元组。

        结果交给共用摘要函数，保持与目录成员读取方式一致。
        """
        from src.utils.temp_dirs import temporary_directory

        try:
            from py7zr.io import Py7zIO, WriterFactory
        except ImportError:
            return None

        def check_continue() -> None:
            """检查调用方取消状态，中止时抛出 InterruptedError。"""
            if checkpoint is not None and not checkpoint():
                raise InterruptedError("hash cancelled")

        class _TemporaryMemberIO(Py7zIO):
            """将解码成员写入临时文件的 py7zr 流适配器，避免将整个成员驻留内存。"""

            def __init__(self, target: Path) -> None:
                """创建可读写的成员临时文件，并持有其句柄直到 close。"""
                self.target = target
                self._handle = open(fs.native_path(target), "w+b")

            def write(self, data: bytes | bytearray) -> int:
                """确认任务未取消后写入解码数据，返回写入字节数。"""
                check_continue()
                return self._handle.write(data)

            def read(self, size: int | None = None) -> bytes:
                """确认任务未取消后读取成员字节；size 为 None 时读取剩余全部内容。"""
                check_continue()
                return self._handle.read(-1 if size is None else size)

            def seek(self, offset: int, whence: int = 0) -> int:
                """按文件流约定移动位置并返回新的字节偏移。"""
                return self._handle.seek(offset, whence)

            def flush(self) -> None:
                """将 Python 文件缓冲写入底层临时文件。"""
                self._handle.flush()

            def size(self) -> int:
                """返回临时成员的字节长度，同时保留当前读写位置。"""
                current = self._handle.tell()
                self._handle.seek(0, os.SEEK_END)
                size = self._handle.tell()
                self._handle.seek(current)
                return size

            def close(self) -> None:
                """释放成员文件句柄，允许重复调用。"""
                if not self._handle.closed:
                    self._handle.close()

        class _TemporaryMemberFactory(WriterFactory):
            """为归档成员分配独立临时文件，并检测规范化后重复的成员名称。"""

            def __init__(self, root: Path) -> None:
                """记录临时目录，初始化成员路径映射及待释放的流集合。"""
                self.root = root
                self.paths: dict[str, Path] = {}
                self.writers: list[_TemporaryMemberIO] = []
                self.duplicate_name = False

            def create(self, filename: str) -> Py7zIO:
                """为成员创建按序号命名的临时流，避免归档路径直接决定写入位置。"""
                key = filename.replace("\\", "/")
                if key in self.paths:
                    self.duplicate_name = True
                target = self.root / f"{len(self.writers):08d}.member"
                writer = _TemporaryMemberIO(target)
                self.paths[key] = target
                self.writers.append(writer)
                return writer

            def close(self) -> None:
                """关闭本工厂创建的全部成员流。"""
                for writer in self.writers:
                    writer.close()

        temp_dir = temporary_directory(prefix="d3dx_hash_7z_")
        factory = _TemporaryMemberFactory(Path(temp_dir.name))
        try:
            check_continue()
            normalized_names = [name.replace("\\", "/") for name in names]
            from src.services.seven_zip import open_7z

            with open_7z(path) as archive:
                archive.extract(targets=names, factory=factory)
            check_continue()
            if factory.duplicate_name:
                # 文件系统无法表达同名重复成员；该罕见畸形包沿用外层原始字节回退。
                return None

            triples: list[tuple[str, int, int]] = []
            # factory.paths 按 normalized 原名索引；rel 的规范化/剥顶层留给
            # _effective_triples，这里只负责把字节变成 (size, crc32)。
            for name in normalized_names:
                check_continue()
                extracted = factory.paths.get(name)
                if extracted is None or not fs.is_file(extracted):
                    return None
                with open(fs.native_path(extracted), "rb") as handle:
                    size, crc = Mod._crc_of_stream(
                        lambda handle=handle: handle.read(_HASH_CHUNK), checkpoint=checkpoint
                    )
                triples.append((name, size, crc))
            return triples
        except InterruptedError:
            raise
        except Exception:
            # py7zr 并行解码可能包装 writer 抛出的 InterruptedError；再次检查可把
            # 它恢复为正常取消，而不是误走原始字节哈希。
            check_continue()
            return None
        finally:
            factory.close()
            temp_dir.cleanup()

    def to_dict(self) -> dict:
        """序列化为可持久字典，应用内的源、部署和图片引用使用相对路径。"""
        return {
            "game_id": self.game_id,
            "name": self.name,
            "folder_name": self.folder_name,
            "path": to_stored_path(self.path),
            "mod_id": self.mod_id,
            "deployed_path": to_stored_path(self.deployed_path),
            "content_hash": self.content_hash,
            "status": self.status.value,
            "node_id": self.node_id,
            "info": {
                "author": self.info.author,
                "description": self.info.description,
                "version": self.info.version,
                "url": self.info.url,
                "age_rating": self.info.age_rating.value,
                "tags": self.info.tags,
                "custom_data": deepcopy(self.info.custom_data),
            },
            "preview_images": [to_stored_path(path) for path in self.preview_images],
            "imported_at": self.imported_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Mod:
        """从字典读取 Mod；不为缺失的 mod_id 生成身份，避免掩盖损坏的部署或预设引用。"""
        info_data = data.get("info", {})
        info = ModInfo(
            author=info_data.get("author", ""),
            description=info_data.get("description", ""),
            version=info_data.get("version", ""),
            url=info_data.get("url", ""),
            age_rating=AgeRating(info_data.get("age_rating", "G")),
            tags=info_data.get("tags", []),
            custom_data=deepcopy(info_data.get("custom_data", {})),
        )
        return cls(
            game_id=data.get("game_id", ""),
            name=data.get("name", ""),
            folder_name=data.get("folder_name", ""),
            path=resolve_stored_path(data.get("path", "")),
            mod_id=data.get("mod_id", ""),
            deployed_path=resolve_stored_path(data.get("deployed_path", "")),
            content_hash=data.get("content_hash", ""),
            status=ModStatus(data.get("status", "disabled")),
            node_id=data.get("node_id", ""),
            info=info,
            preview_images=[resolve_stored_path(path) for path in data.get("preview_images", [])],
            imported_at=data.get("imported_at", ""),
        )


def validate_mod_record(mod: Mod, game_id: str) -> str:
    """入库前校验 Mod 的身份、游戏和存储位置。

    索引写入与导入提交共用此规则，返回空字符串表示有效，否则返回错误原因。
    """
    if not (mod.path or "").strip():
        return tr("path 为空")
    if not (game_id or "").strip():
        return tr("game_id 为空")
    return ""
