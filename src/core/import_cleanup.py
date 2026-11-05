"""持久保护插件导入的未提交容器和独立暂存副本，并在同卷隔离后安全清理。

原位置改名失败时不删除任何成员；恢复凭据位于临时目录之外，重启后仍阻止自动纳管。
文件准备和删除由调用方放在后台，索引身份仅通过所有者线程冻结的集合传入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from src.core.category_preset_journal import rename_exclusive
from src.core.library_schema import valid_relative
from src.core.mod_identity import validate_mod_id
from src.core.mod_package import CONTAINER_KIND
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir

_KIND = "d3dxSkinManage-Plus.import-cleanup"
_RETRY_DELAYS = (0.0, 0.05, 0.1, 0.2, 0.3)


class ImportCleanupPending(OSError):
    """清理未完成，调用方须展示残留路径且保留恢复凭据。"""

    def __init__(self, paths, reason="") -> None:
        """保存绝对路径及诊断，避免取消提示掩盖未完成的文件清理。"""
        self.paths = tuple(str(path) for path in paths)
        message = tr("导入产物尚待清理，下次导入时将重试：{paths}", paths="、".join(self.paths))
        super().__init__(message + ("\n" + str(reason) if reason else ""))


def _safe_path(path) -> Path:
    """规范化绝对路径，并拒绝自身或祖先中的符号链接、目录联接和其他重解析点。"""
    result = Path(os.path.abspath(fs.normal_path(path)))
    for part in (result, *result.parents):
        try:
            info = os.lstat(fs.native_path(part))
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(tr("清理路径包含链接或目录联接：{path}", path=part))
    return result


def _identity(path) -> list[int]:
    """读取已校验路径的设备号和文件编号，不使用可随内容变化的目录时间戳。"""
    info = os.lstat(fs.native_path(_safe_path(path)))
    return [info.st_dev, info.st_ino]


def _version(path) -> list:
    """采集对象身份与文件版本，拒绝恢复凭据之外的文件类型。"""
    info = os.lstat(fs.native_path(_safe_path(path)))
    if stat.S_ISDIR(info.st_mode):
        return ["directory", info.st_dev, info.st_ino, 0, 0]
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=path))
    return ["file", info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _inventory(root: Path) -> dict[str, list]:
    """枚举自有容器的普通文件和目录，不跟随链接，也不重新计算大型归档摘要。"""
    result = {".": _version(root)}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(fs.native_path(directory)) as children:
            for child in children:
                path = directory / child.name
                version = _version(path)
                result[path.relative_to(root).as_posix()] = version
                if version[0] == "directory":
                    pending.append(path)
    return result


def _remove_owned_file(path: Path, expected: list) -> None:
    """仅对身份仍匹配的隔离副本清除 Windows 只读属性，原始来源保持只读。"""
    try:
        fs.unlink(path)
    except PermissionError:
        info = os.lstat(fs.native_path(_safe_path(path)))
        if os.name != "nt" or not getattr(info, "st_file_attributes", 0) & 1 or _version(path) != expected:
            raise
        os.chmod(fs.native_path(path), info.st_mode | stat.S_IWRITE)
        fs.unlink(path)


@dataclass
class _Receipt:
    """保存新容器及父目录的创建身份，隔离路径只能由已验证的原位置推导。"""

    token: str
    game_id: str
    root: str
    relative: str
    root_identity: list[int]
    parent_identity: list[int]
    container_identity: list[int]
    mod_id: str
    content_hash: str
    body_name: str
    marker_digest: str = ""
    commit_started: bool = False
    entries: dict[str, list] | None = None
    active: bool = True
    location_kind: str = "library"

    @property
    def original(self) -> Path:
        """返回位于冻结游戏库或应用暂存根目录内的原容器路径。"""
        return Path(self.root) / self.relative

    @property
    def quarantine(self) -> Path:
        """将隔离目录固定在原父目录的元数据目录下，避免跨卷移动或被资源扫描发现。"""
        return self.original.parent / ".d3dxmeta" / "import-cleanup" / self.token


class ImportCleanupRecovery:
    """为插件导入保存可恢复的清理所有权，默认导入流程无需创建凭据。"""

    def __init__(self, manager) -> None:
        """绑定保护登记表，凭据目录与记录均惰性创建。"""
        self._manager = manager
        self._directory = get_data_dir() / "recovery" / "import-cleanup"
        self._lock = threading.RLock()
        self._receipts: dict[str, _Receipt] = {}

    def _protect(self, receipt: _Receipt) -> None:
        """以独立令牌保护原位置、隔离位置及身份，不随普通导入令牌的释放而消失。"""
        self._manager.protect_library_operation(
            "import-cleanup:" + receipt.token,
            paths=(receipt.original, receipt.quarantine),
            mod_ids=(receipt.mod_id,) if receipt.location_kind == "library" else (),
        )

    def _save(self, receipt: _Receipt) -> None:
        """在破坏性操作之前原子保存并同步凭据，写入失败时不继续删除。"""
        directory = _safe_path(self._directory)
        fs.ensure_dir(directory)
        target = directory / (receipt.token + ".json")
        temporary = directory / (receipt.token + "." + uuid.uuid4().hex + ".tmp")
        data = asdict(receipt)
        data.update(kind=_KIND, version=1)
        data.pop("active")
        try:
            with open(fs.native_path(temporary), "x", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(fs.native_path(temporary), fs.native_path(target))
        finally:
            fs.unlink(temporary, missing_ok=True)

    @staticmethod
    def _validate_staging_location(root: Path, relative: str, body_name: str) -> None:
        """暂存凭据仅允许应用临时根目录中独立创建的单层归档副本。"""
        if (
            root != _safe_path(get_data_dir() / "temp")
            or re.fullmatch(r"plugin-import-[0-9a-f]{32}", relative) is None
            or body_name not in {"source.zip", "source.7z", "source.rar"}
        ):
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=root / relative))

    def begin(self, *, game_id, root, container, body_name, mod_id, content_hash, location_kind="library") -> str:
        """在新空容器创建后、正文复制前持久记录所有权，失败时仅撤销仍为空的自有目录。"""
        root, container = _safe_path(root), _safe_path(container)
        relative = container.relative_to(root).as_posix()
        if not valid_relative(relative) or not valid_relative(body_name) or Path(body_name).name != body_name:
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=container))
        if location_kind == "staging":
            self._validate_staging_location(root, relative, body_name)
        elif location_kind != "library":
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=container))
        receipt = _Receipt(
            uuid.uuid4().hex,
            game_id,
            str(root),
            relative,
            _identity(root),
            _identity(container.parent),
            _identity(container),
            validate_mod_id(mod_id),
            content_hash,
            body_name,
            location_kind=location_kind,
        )
        with self._lock:
            self._receipts[receipt.token] = receipt
        self._protect(receipt)
        try:
            self._save(receipt)
        except Exception as error:
            if _identity(container) == receipt.container_identity:
                try:
                    fs.rmdir(container)
                    self._forget(receipt)
                except OSError:
                    pass
            raise ImportCleanupPending((container,), error) from error
        return receipt.token

    def prepared(self, token: str) -> None:
        """记录容器标记或完整暂存清单，后续回收拒绝被外部修改的副本。"""
        receipt = self._receipts[token]
        if receipt.location_kind == "staging":
            entries = _inventory(receipt.original)
            if set(entries) != {".", receipt.body_name} or entries[receipt.body_name][0] != "file":
                raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=receipt.original))
            receipt.entries = entries
        else:
            self._validate_marker(receipt, receipt.original)
            receipt.marker_digest = hashlib.sha256(fs.read_bytes(receipt.original / ".d3dxmod")).hexdigest()
        self._save(receipt)

    def _forget(self, receipt: _Receipt) -> None:
        """先移除耐久凭据再释放保护；已提交 Mod 仅移除凭据，不访问其容器内容。"""
        fs.unlink(_safe_path(self._directory) / (receipt.token + ".json"), missing_ok=True)
        with self._lock:
            self._receipts.pop(receipt.token, None)
        self._manager.release_library_operation("import-cleanup:" + receipt.token)

    def committed(self, token: str) -> None:
        """提交后的后台收尾移除清理凭据，不能回收已提交的资源容器。"""
        if not token:
            return
        receipt = self._receipts.get(token)
        if receipt is not None:
            if receipt.location_kind != "library":
                raise ValueError("staging cleanup cannot be committed")
            receipt.active = False
            self._forget(receipt)

    def committing(self, token: str) -> None:
        """索引写入之前耐久记录提交意向，索引丢失后也不能再据此回收完整资源。"""
        receipt = self._receipts[token]
        if receipt.location_kind != "library":
            raise ValueError("staging cleanup cannot be committed")
        receipt.commit_started = True
        self._save(receipt)

    @staticmethod
    def _validate_marker(receipt: _Receipt, location: Path) -> None:
        """存在标记时核对请求身份及摘要；已完成容器在原位置不允许丢失标记。"""
        marker = location / ".d3dxmod"
        if not fs.exists(marker):
            if receipt.marker_digest and location == receipt.original:
                raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=marker))
            return
        raw = fs.read_bytes(_safe_path(marker))
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("kind") != CONTAINER_KIND
            or data.get("mod_id") != receipt.mod_id
            or data.get("content_hash") != receipt.content_hash
            or data.get("body") != receipt.body_name
            or (receipt.marker_digest and hashlib.sha256(raw).hexdigest() != receipt.marker_digest)
        ):
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=marker))

    def _attempt(self, receipt: _Receipt) -> None:
        """验证完整原容器后原子隔离；仅在隔离目录内删除仍匹配凭据的成员。"""
        original, quarantine = receipt.original, receipt.quarantine
        if receipt.location_kind == "staging":
            self._validate_staging_location(_safe_path(receipt.root), receipt.relative, receipt.body_name)
        if _identity(receipt.root) != receipt.root_identity or _identity(original.parent) != receipt.parent_identity:
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=original.parent))
        quarantined = fs.exists(_safe_path(quarantine))
        location = quarantine if quarantined else original
        if not fs.exists(_safe_path(location)):
            self._forget(receipt)
            return
        if _identity(location) != receipt.container_identity:
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=location))
        actual = _inventory(location)
        allowed = {".", receipt.body_name}
        if receipt.location_kind == "library":
            allowed.update({".d3dxmod", ".d3dxmeta"})
        if any((key if receipt.location_kind == "staging" else key.split("/")[0]) not in allowed for key in actual):
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=location))
        self._validate_marker(receipt, location)
        if receipt.entries is None:
            receipt.entries = actual
            self._save(receipt)
        expected = receipt.entries
        if any(expected.get(key) != version for key, version in actual.items()) or (
            not quarantined and actual.keys() != expected.keys()
        ):
            raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=location))
        if not quarantined:
            fs.ensure_dir(_safe_path(quarantine.parent))
            rename_exclusive(fs.native_path(original), fs.native_path(quarantine))
            location = quarantine
        # 正文优先、标记最后；被占用时不先剥离描述和身份，重试仍可证明归属。
        files = sorted(
            (key for key in expected if expected[key][0] == "file" and key != ".d3dxmod"),
            key=lambda key: (key != receipt.body_name, key),
        )
        directories = sorted(
            (key for key in expected if expected[key][0] == "directory" and key != "."),
            key=lambda key: key.count("/"),
            reverse=True,
        )
        for key in [*files, *directories, ".d3dxmod", "."]:
            path = location if key == "." else location / key
            if not fs.exists(_safe_path(path)):
                continue
            if expected.get(key) != _version(path):
                raise ValueError(tr("清理对象的身份或内容已变化：{path}", path=path))
            if expected[key][0] == "directory":
                fs.rmdir(path)
            else:
                _remove_owned_file(path, expected[key])
        self._forget(receipt)

    def cancel(self, token: str, *, deadline: float | None = None) -> None:
        """后台有限重试占用导致的失败，最终仍失败时保存保护并向调用方报告路径。"""
        receipt = self._receipts.get(token)
        if receipt is None:
            return
        receipt.active = False
        if receipt.commit_started:
            location = receipt.quarantine if fs.exists(receipt.quarantine) else receipt.original
            raise ImportCleanupPending((location,), tr("导入已进入提交阶段，无法确认是否提交，请检查保留的文件"))
        error = None
        for delay in _RETRY_DELAYS:
            if delay:
                if deadline is not None and time.monotonic() + delay > deadline:
                    break
                time.sleep(delay)
            try:
                self._attempt(receipt)
                return
            except Exception as caught:
                error = caught
                if not isinstance(caught, OSError):
                    break
        location = receipt.quarantine if fs.exists(receipt.quarantine) else receipt.original
        raise ImportCleanupPending((location,), error) from error

    def retry_pending(self, game_id: str, indexed_ids: frozenset[str]) -> None:
        """后台恢复同游戏容器及独立暂存副本，只有库内容器使用索引身份判断提交。"""
        with self._lock:
            pending = tuple(
                row
                for row in self._receipts.values()
                if not row.active and (row.game_id == game_id or row.location_kind == "staging")
            )
        deadline = time.monotonic() + 1.0
        for receipt in pending:
            if receipt.location_kind == "library" and receipt.mod_id in indexed_ids:
                self._forget(receipt)
            else:
                self.cancel(receipt.token, deadline=deadline)

    def contains(self, path) -> bool:
        """仅排除挂起容器及其后代，不能因祖先重叠屏蔽其他正常分类。"""
        key = os.path.normcase(os.path.abspath(fs.normal_path(path)))
        with self._lock:
            roots = tuple(str(root) for row in self._receipts.values() for root in (row.original, row.quarantine))
        return any(key == os.path.normcase(root) or key.startswith(os.path.normcase(root) + os.sep) for root in roots)

    def restore_pending(self) -> None:
        """启动时只读取凭据并恢复保护，等待下一次后台导入再进行删除重试。"""
        if not fs.is_dir(self._directory):
            return
        roots = self._manager._index.paths.roots
        indexed = self._manager._index.get_mod_ids()
        for path in _safe_path(self._directory).glob("*.json"):
            try:
                data = json.loads(fs.read_text(_safe_path(path), encoding="utf-8"))
                if data.pop("kind", "") != _KIND or data.pop("version", None) != 1:
                    raise ValueError("unsupported import cleanup receipt")
                # 缺少提交阶段信息的旧凭据不能证明资源未提交，按模糊提交保守保留。
                data.setdefault("commit_started", True)
                receipt = _Receipt(**data, active=False)
                if (
                    len(receipt.token) != 32
                    or any(char not in "0123456789abcdef" for char in receipt.token)
                    or path.stem != receipt.token
                    or not valid_relative(receipt.relative)
                    or not valid_relative(receipt.body_name)
                    or Path(receipt.body_name).name != receipt.body_name
                    or receipt.location_kind not in {"library", "staging"}
                    or (receipt.location_kind == "library" and receipt.game_id not in roots)
                    or validate_mod_id(receipt.mod_id) != receipt.mod_id
                    or (
                        len(receipt.content_hash) != 40
                        if receipt.location_kind == "library"
                        else receipt.content_hash != ""
                    )
                    or not isinstance(receipt.commit_started, bool)
                ):
                    raise ValueError("invalid import cleanup receipt")
                for identity in (receipt.root_identity, receipt.parent_identity, receipt.container_identity):
                    if (
                        not isinstance(identity, list)
                        or len(identity) != 2
                        or not all(type(v) is int for v in identity)
                    ):
                        raise ValueError("invalid import cleanup identity")
                if receipt.entries is not None:
                    for key, version in receipt.entries.items():
                        if (
                            key != "."
                            and not valid_relative(key)
                            or not isinstance(version, list)
                            or len(version) != 5
                            or version[0] not in {"file", "directory"}
                            or not all(type(value) is int for value in version[1:])
                        ):
                            raise ValueError("invalid import cleanup inventory")
                if receipt.location_kind == "staging":
                    receipt.root = str(_safe_path(receipt.root))
                    self._validate_staging_location(Path(receipt.root), receipt.relative, receipt.body_name)
                else:
                    receipt.root = str(_safe_path(roots[receipt.game_id]))
                if receipt.location_kind == "library" and receipt.mod_id in indexed:
                    self._forget(receipt)
                    continue
                with self._lock:
                    self._receipts[receipt.token] = receipt
                self._protect(receipt)
            except Exception as error:
                logger.error("导入清理凭据无法恢复，保留供检查：%s: %s", path, error)
