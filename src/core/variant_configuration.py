"""构造运行时偏好快照，原始 INI 只作为读取前提。"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from src.core import migoto_user_ini as user_ini
from src.core.mod_package import (
    _archive_top_prefix,
    body_ini_rels,
    is_resident_archive,
    read_body_ini_text,
)
from src.core.persist_vars import declaration_fingerprint, read_default_values, snapshot_variables
from src.core.variant_default_cache import DefaultIniCache
from src.core.variant_operation import FileChange
from src.utils.i18n import t as tr


def original_inis(mod_path: str) -> tuple[tuple[str, str], ...]:
    """一次归档读取获取全部原始 INI，包括覆盖文件已移除的成员。"""
    path = Path(mod_path)
    if is_resident_archive(path):
        from src.services.archive import ArchiveService

        raw = ArchiveService().read_all_inis(path)
        prefix = _archive_top_prefix(path)
        entries = []
        for member, contents in raw.items():
            rel = member.replace("\\", "/")
            if prefix and rel.startswith(prefix + "/"):
                rel = rel[len(prefix) + 1 :]
            entries.append((rel, contents.decode("utf-8", errors="surrogateescape")))
        return tuple(entries)
    entries = []
    for rel in body_ini_rels(path):
        text = read_body_ini_text(path, rel)
        if text is None:
            raise ValueError(tr("无法读取资源本体默认值：{rel}", rel=rel))
        entries.append((rel, text))
    return tuple(entries)


def default_values(mod, index, cache: DefaultIniCache) -> dict[str, str]:
    """按正文路径、摘要、大小和修改时间缓存原始 INI，并从声明索引解析默认变量值。"""
    stat = Path(mod.path).stat()
    key = (mod.path, mod.content_hash, stat.st_size, stat.st_mtime_ns)
    inis = cache.read(mod.mod_id, key, partial(original_inis, mod.path))
    return read_default_values(index, mod.mod_id, inis)


def configuration_changes(
    mods: dict, index, targets: dict, snapshot, active_ids: set, *, runtime_path=None, default_ids=(), source_checks=()
):
    """核对全部目标并冻结共享的命名或默认配置修改。"""
    defaults = set(default_ids)
    if not defaults <= targets.keys():
        raise ValueError(tr("默认目标不属于本次变体操作"))
    updates, keys = {}, []
    for mid, values in targets.items():
        if mid in defaults:
            declaration_fingerprint(index, mid)
            if values:
                raise ValueError(tr("加载器默认目标不能包含合成数值"))
        else:
            snapshot_variables(index, mid, values)
        if mid in active_ids:
            keys.extend(index.keys_of(mid) if mid in defaults else ())
            updates.update(values)

    changes = list(source_checks)
    if not keys and not updates:
        return changes, 0
    if runtime_path is None:
        raise ValueError(tr("未配置加载器用户配置文件，无法写入变体偏好"))
    before, text, encoding = None, "", "utf-8"
    if snapshot is not None:
        text, encoding = snapshot.text, snapshot.encoding
        before = user_ini.encode_ini_text(text, encoding)
        if user_ini.sha256_of(text, encoding) != snapshot.sha256:
            raise ValueError(tr("用户配置文件编码不能无损回写"))
    stripped = user_ini.remove_keys(text, keys)
    removed = len(user_ini.parse_persisted_values(text)) - len(user_ini.parse_persisted_values(stripped))
    updated = user_ini.apply_values(stripped, updates)
    after = user_ini.encode_ini_text(updated, encoding) if snapshot is not None or updates else None
    # 文件缺失也是比较前提，默认值本身不会创建文件。
    changes.append(FileChange(Path(runtime_path), before, after))
    return changes, max(removed, 0)
