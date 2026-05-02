"""删除明确选择的预览，保留资源身份和回滚材料。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from src.core.library_snapshot import library_mutation, path_key
from src.core.operation_preflight import access_file, confirm_metadata, confirm_source, failure, file_version
from src.core.preview_cache_index import CACHE_FINGERPRINT
from src.models.mod import Mod
from src.services.archive import ArchiveService
from src.utils import fs
from src.utils.constants import IMAGE_EXTENSIONS
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _remember_directories(path, root, directories):
    """拒绝受管目录内部各层级的链接。"""
    relative = Path(path).relative_to(root)
    current = Path(root)
    for part in ("", *relative.parts):
        if part:
            current /= part
        version = file_version(current)
        if not stat.S_ISDIR(os.stat(fs.native_path(current)).st_mode):
            raise NotADirectoryError(str(current))
        directories[current] = version[:2]


def _check_directories(directories):
    """逐一核对预览相关目录的设备与文件身份，任一目录被替换时中止。"""
    for path, identity in directories.items():
        if file_version(path)[:2] != identity:
            raise OSError(tr("预览所在目录已变化：{path}", path=path))


def _check_writable(path):
    """确认目标为可写普通文件且父目录可写，访问检查前后版本必须一致。"""
    version = file_version(path)
    if not stat.S_ISREG(os.stat(fs.native_path(path), follow_symlinks=False).st_mode):
        raise OSError(tr("预览操作目标不是普通文件：{path}", path=path))
    with access_file(path, write=True), access_file(path.parent, write=True, directory=True):
        pass
    if file_version(path) != version:
        raise OSError(tr("预览操作目标已变化：{path}", path=path))
    return version


def _stage(target, temporaries, *, data=None, suffix=".tmp"):
    """在目标所在目录创建并登记临时文件，可写入指定字节并刷新磁盘。"""
    descriptor, name = tempfile.mkstemp(prefix=".preview-edit-", suffix=suffix, dir=fs.native_path(target.parent))
    path = Path(fs.normal_path(name))
    temporaries.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        if data is not None:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return path


def _json_bytes(data):
    """将数据序列化为保留中文的缩进 UTF-8 JSON 字节。"""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf8")
