"""在受管容器中原子保存独立变体文件。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from threading import RLock

from src.core.mod_package import _resolve_sidecar_dir
from src.models.mod_variant import ModVariant, normalize_variant_name, validate_variant_id
from src.utils import fs
from src.utils.i18n import t as tr

# 导入和保存操作可能为同一 Mod 创建多个存储实例。
VARIANT_STORE_LOCK = RLock()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """构造 JSON 对象时拒绝重复字段，避免变体内容被同名字段覆盖。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(tr("变体 JSON 包含重复字段：{key}", key=key))
        result[key] = value
    return result


class ModVariantStore:
    """在受管容器的独立元数据目录中校验、枚举和持久化变体文件。"""

    def __init__(self, mod_path: str | Path):
        """解析 Mod 旁置目录并确认变体存储位置安全，无法定位容器时拒绝初始化。"""
        sidecar = _resolve_sidecar_dir(mod_path)
        if sidecar is None:
            raise ValueError(tr("无法定位 Mod 的受管容器：{mod_path}", mod_path=mod_path))
        self._container = sidecar.parent
        self._directory = sidecar / "variants"
        self._check_directory()

    def _check_directory(self) -> None:
        """检查元数据及变体目录的类型与真实路径，拒绝链接、重解析点和重定向。"""
        expected = self._container.resolve()
        for directory in (self._directory.parent, self._directory):
            expected = expected / directory.name
            try:
                info = os.lstat(fs.native_path(directory))
            except FileNotFoundError:
                pass
            else:
                is_reparse = getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                )
                if stat.S_ISLNK(info.st_mode) or is_reparse or not stat.S_ISDIR(info.st_mode):
                    raise ValueError(
                        tr("变体元数据目录必须是独立目录，不能是链接或重解析点：{directory}", directory=directory)
                    )
            if directory.resolve() != expected:
                raise ValueError(tr("变体元数据目录被重定向，不能写入资源本体或其他位置"))

    def path_for(self, variant_id: str) -> Path:
        """按变体身份寻找唯一文件并检查大小写一致性，未存在时返回安全目标路径。"""
        validate_variant_id(variant_id)
        matches = [path for path in self._variant_paths() if path.stem.casefold() == variant_id.casefold()]
        if len(matches) > 1:
            raise ValueError(tr("变体存在多个相同身份的文件：{variant_id}", variant_id=variant_id))
        path = matches[0] if matches else self._directory / f"{variant_id}.json"
        if path.stem != variant_id:
            raise ValueError(tr("变体文件名与请求的 variant_id 不一致：{path}", path=path))
        return self._checked_path(path)

    def _checked_path(self, path: Path) -> Path:
        """重新校验目录并确保文件解析后仍在所属变体目录中。"""
        self._check_directory()
        if not path.resolve().is_relative_to(self._directory.resolve()):
            raise ValueError(tr("变体文件越出所属变体目录"))
        return path

    def get(self, variant_id: str) -> ModVariant:
        """按身份读取并返回已验证的变体对象。"""
        return self.get_snapshot(variant_id)[0]

    def get_snapshot(self, variant_id: str) -> tuple[ModVariant, bytes]:
        """从同一次读取返回模型和原始内容，事务须使用该内容比较，避免接受后续外部修改。"""
        return self._read_snapshot(self.path_for(variant_id))

    def _read_path(self, path: Path) -> ModVariant:
        """读取指定文件的变体对象，复用同时生成原始快照的校验流程。"""
        return self._read_snapshot(path)[0]

    def _read_snapshot(self, path: Path) -> tuple[ModVariant, bytes]:
        """读取变体及原始字节，拒绝重复 JSON 字段、无效内容和文件名身份不符。"""
        variant_id = validate_variant_id(path.stem)
        self._checked_path(path)
        try:
            raw = fs.read_bytes(path)
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            variant = ModVariant.from_dict(data)
        except ValueError as error:
            raise ValueError(tr("变体文件损坏：{path}：{error}", path=path, error=error)) from error
        if variant.variant_id != variant_id:
            raise ValueError(tr("变体文件名与 variant_id 不一致：{path}", path=path))
        return variant, raw

    def list(self) -> list[ModVariant]:
        """在共享变体存储锁内读取当前容器的全部有效变体。"""
        with VARIANT_STORE_LOCK:
            return self._list()

    def _variant_paths(self) -> list[Path]:
        """列出排序后的变体 JSON 路径，目录尚不存在时返回空列表。"""
        self._check_directory()
        try:
            with os.scandir(fs.native_path(self._directory)) as entries:
                names = sorted(entry.name for entry in entries if entry.name.casefold().endswith(".json"))
        except FileNotFoundError:
            return []
        return [self._directory / name for name in names]

    def _list(self) -> list[ModVariant]:
        """校验所有变体的名称和身份唯一性，并按名称及身份排序返回。"""
        variants = []
        used_names: set[str] = set()
        used_ids: set[str] = set()
        for path in self._variant_paths():
            variant = self._read_path(path)
            name_key, id_key = variant.name.casefold(), variant.variant_id.casefold()
            if name_key in used_names or id_key in used_ids:
                raise ValueError(tr("Mod 容器内存在重名或身份冲突的变体"))
            used_names.add(name_key)
            used_ids.add(id_key)
            variants.append(variant)
        return sorted(variants, key=lambda item: (item.name.casefold(), item.variant_id))

    def save(self, variant: ModVariant) -> None:
        """在共享变体存储锁内验证并保存给定变体。"""
        with VARIANT_STORE_LOCK:
            self._save(variant)

    def _save(self, variant: ModVariant) -> None:
        """校验变体类型及名称身份冲突，刷新临时文件后原子替换目标 JSON。"""
        if not isinstance(variant, ModVariant):
            raise ValueError(tr("只能保存 ModVariant"))
        self.path_for(variant.variant_id)
        for existing in self.list():
            if existing.variant_id == variant.variant_id:
                continue
            if existing.name.casefold() == variant.name.casefold():
                raise ValueError(tr("变体名称已存在：{name}", name=variant.name))
            if existing.variant_id.casefold() == variant.variant_id.casefold():
                raise ValueError(tr("变体身份存在大小写冲突"))
        self._check_directory()
        fs.ensure_dir(self._directory)
        self._check_directory()
        descriptor, temporary = tempfile.mkstemp(prefix=".variant-", suffix=".tmp", dir=fs.native_path(self._directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(variant.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, fs.native_path(self.path_for(variant.variant_id)))
        finally:
            fs.unlink(temporary, missing_ok=True)

    def delete(self, variant_id: str) -> None:
        """在锁内读取验证目标变体后删除其文件，保留读取或删除异常。"""
        with VARIANT_STORE_LOCK:
            self.get(variant_id)
            fs.unlink(self.path_for(variant_id))

    def new_id(self) -> str:
        """生成尚未占用对应变体文件路径的随机身份。"""
        while True:
            candidate = uuid.uuid4().hex
            if not fs.exists(self.path_for(candidate)):
                return candidate

    def unique_name(self, base: str) -> str:
        """规范基础名称并追加递增编号，生成不与现有变体重名的名称。"""
        name = normalize_variant_name(base)
        used = {item.name.casefold() for item in self.list()}
        candidate, suffix = name, 2
        while candidate.casefold() in used:
            candidate = f"{name} ({suffix})"
            suffix += 1
        return candidate
