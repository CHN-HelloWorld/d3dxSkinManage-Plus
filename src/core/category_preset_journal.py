"""持久化分类发布凭据，并提供可重复执行的事务恢复。

凭据中的路径由本地生成。恢复时同时核对文件身份和内容，不按文件名推断所有权。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from pathlib import Path

from src.core.category_preset import CategoryPresetError
from src.utils.i18n import t as tr


def safe_path(path):
    """将路径转换为绝对路径，并拒绝其自身或祖先中的符号链接和目录联接。"""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CategoryPresetError(tr("路径不能穿过链接或目录联接：{part}", part=part))
    return path


def identity(path):
    """读取经路径安全检查的文件系统对象身份，返回设备号与文件编号。"""
    info = safe_path(path).lstat()
    return [info.st_dev, info.st_ino]


def digest(path):
    """分块计算安全路径指向文件的 SHA-256 摘要；读取错误交由调用方处理。"""
    result = hashlib.sha256()
    with safe_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def receipt(path):
    """记录对象身份与类型，并为普通文件附加内容摘要以供恢复时核对。"""
    path = safe_path(path)
    info = path.lstat()
    return {
        "identity": [info.st_dev, info.st_ino],
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
        "sha256": "" if stat.S_ISDIR(info.st_mode) else digest(path),
    }


def matches(path, expected):
    """比较磁盘对象与预期凭据；对象缺失、不可读或路径不安全时返回 False。"""
    try:
        actual = receipt(path)
        return actual == expected
    except (OSError, CategoryPresetError):
        return False


def inventory(root):
    """遍历目录树并按相对路径记录各对象凭据，遇到链接或读取失败时中止。"""
    root = safe_path(root)
    result = {".": receipt(root)}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = receipt(path)
    return result


def directory_anchors(paths):
    """持有原有父目录对象，不依赖可变化的目录时间戳。"""
    anchors = {}
    for path in paths:
        for parent in Path(path).parents:
            if str(parent) not in anchors and parent.is_dir():
                anchors[str(parent)] = receipt(parent)
    return anchors


def anchor_errors(anchors, path=None):
    """核对全部或指定路径祖先的目录凭据，返回身份已改变或无法读取的目录。"""
    candidates = (
        anchors
        if path is None
        else {str(parent): anchors[str(parent)] for parent in Path(path).parents if str(parent) in anchors}
    )
    return [location for location, expected in candidates.items() if not matches(location, expected)]


def published_errors(actions, *, directories_only=False):
    """核对发布动作的目标凭据，返回不匹配路径；可限制为仅检查目录。"""
    errors = []
    for action in actions:
        target = Path(action["target"])
        entries = action["entries"] if action["kind"] == "tree" else {".": action["after"]}
        for relative, expected in entries.items():
            if directories_only and expected["kind"] != "directory":
                continue
            path = target if relative == "." else target / relative
            if not matches(path, expected):
                errors.append(str(path))
    return errors


class StagingOwnership:
    """记录创建凭据，目录枚举结果不能作为所有权证明。"""

    def __init__(self, root, entries=None):
        """绑定暂存根目录并复制已有凭据；未提供凭据时记录根目录身份。"""
        self.root = Path(root)
        self.entries = {".": receipt(root)} if entries is None else dict(entries)

    def key(self, path):
        """将暂存树内的路径转换为正斜杠相对路径；树外路径会触发异常。"""
        return Path(path).relative_to(self.root).as_posix()

    def mkdir(self, path, *, parents=False):
        """创建暂存目录并立即登记身份；按需递归创建尚不存在的父目录。"""
        path = safe_path(path)
        if parents and path.parent != self.root and not path.parent.exists():
            self.mkdir(path.parent, parents=True)
        path.mkdir()
        self.entries[self.key(path)] = receipt(path)

    def opened_file(self, path, stream):
        """从已打开的文件句柄记录新文件身份，以空内容摘要建立初始凭据。"""
        info = os.fstat(stream.fileno())
        self.entries[self.key(path)] = {
            "identity": [info.st_dev, info.st_ino],
            "kind": "file",
            "sha256": hashlib.sha256(b"").hexdigest(),
        }

    def file_digest(self, path, sha256):
        """更新已登记文件的摘要，供分块写入时同步所有权凭据。"""
        self.entries[self.key(path)]["sha256"] = sha256

    def completed_file(self, path, sha256):
        """核对已完成文件的实际摘要，内容一致后记录完整文件凭据。"""
        value = receipt(path)
        if value["sha256"] != sha256:
            raise CategoryPresetError(tr("暂存文件发生变化：{path}", path=path))
        self.entries[self.key(path)] = value

    def subtree(self, root):
        """提取指定暂存子树的凭据，并将子树根的键重写为点号。"""
        prefix = self.key(root)
        return {
            "." if key == prefix else key[len(prefix) + 1 :]: value
            for key, value in self.entries.items()
            if key == prefix or key.startswith(prefix + "/")
        }

    def move(self, source, target):
        """排他重命名暂存子树，成功后将所有相关凭据迁移到目标相对路径。"""
        source_key, target_key = self.key(source), self.key(target)
        moved = {
            key: value for key, value in self.entries.items() if key == source_key or key.startswith(source_key + "/")
        }
        rename_exclusive(source, target)
        for key, value in moved.items():
            del self.entries[key]
            self.entries[target_key + key[len(source_key) :]] = value

    def unexpected(self):
        """枚举暂存树中未登记的对象路径；根目录不存在时返回空列表。"""
        unknown = []
        if not self.root.exists():
            return unknown
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if self.key(path) not in self.entries:
                    unknown.append(str(path))
        return unknown

    def verify(self):
        """检查未知对象和凭据变化，任一不符时拒绝使用当前暂存内容。"""
        errors = self.unexpected()
        errors.extend(
            str(self.root if key == "." else self.root / key)
            for key, value in self.entries.items()
            if not matches(self.root if key == "." else self.root / key, value)
        )
        if errors:
            raise CategoryPresetError(tr("暂存内容含未知或已改变的文件：") + "、".join(errors))

    def cleanup(self):
        """仅清理凭据仍匹配的暂存对象，返回未知或无法安全移除的路径。"""
        return sorted(set(self.unexpected() + remove_inventory(self.root, self.entries)))


def remove_inventory(root, entries):
    """仅删除凭据记录的对象，保留未知或已发生变化的对象。"""
    errors = []
    root = Path(root)
    for relative, expected in sorted(
        entries.items(), key=lambda pair: pair[0].count("/") + (pair[0] != "."), reverse=True
    ):
        path = root if relative == "." else root / relative
        if not os.path.lexists(path):
            continue
        if not matches(path, expected):
            errors.append(str(path))
            continue
        try:
            path.rmdir() if expected["kind"] == "directory" else path.unlink()
        except OSError:
            errors.append(str(path))
    return errors


def rename_exclusive(source, target):
    """以不覆盖目标的原子重命名发布，支持目录和并发发布者。"""
    safe_path(source)
    safe_path(target)
    if os.name == "nt":
        os.rename(source, target)  # 使用不覆盖现有目标的 MoveFileW。
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise OSError(tr("此系统不支持安全的排他发布"))
    result = function(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def rollback_actions(actions):
    """逆序撤销发布动作并核对前后凭据，保留外部修改且汇总无法恢复的路径。"""
    errors = []
    for action in reversed(actions):
        target = Path(action["target"])
        try:
            safe_path(target)
            if action["kind"] == "tree":
                source = Path(action["source"])
                # 原子撤回发布并保留持久身份凭据。源树始终处于发布前或回滚后的完整状态，
                # 避免递归删除在进程崩溃后留下无法判定归属的部分目录。
                current = source if os.path.lexists(source) else target
                actual = inventory(current)
                expected = action["entries"]
                changed = [
                    relative
                    for relative in actual.keys() | expected.keys()
                    if actual.get(relative) != expected.get(relative)
                ]
                if changed:
                    errors.extend(str(current if relative == "." else current / relative) for relative in changed)
                elif current == target:
                    rename_exclusive(target, source)
            elif action["kind"] == "replace":
                if matches(target, action["before"]):
                    continue
                # 恢复过程本身也可能在还原内容后中断。
                if matches(target, action["backup_receipt"]):
                    continue
                if not matches(target, action["after"]):
                    errors.append(str(target))
                elif not matches(action["backup"], action["backup_receipt"]):
                    errors.append(action["backup"])
                else:
                    os.replace(action["backup"], target)
            elif os.path.lexists(target):
                if matches(target, action["after"]):
                    target.unlink()
                else:
                    errors.append(str(target))
        except (OSError, CategoryPresetError):
            errors.append(str(target))
    return sorted(set(errors))
