"""同步容器 INI 覆盖文件与已启用部署中的 INI 内容。

每对路径保存上次一致的内容摘要。两侧相同则不写入；仅一侧变化时同步到另一侧；
两侧都变化时以部署内容为准，以保存运行期间的修改。写入后更新共同摘要，
防止同步产生的事件再次触发反向写入。

监控全部已启用部署的 INI，必要时创建源覆盖文件。事件按路径防抖，
由定时线程执行文件操作，不访问 Qt 控件或线程绑定的数据库连接。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterable
from functools import wraps
from pathlib import Path

from src.core.mod_package import (
    override_path_for,
    resolve_ini_overrides_dir,
)
from src.core.mod_runtime_prefs import PREFS_LOCK
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _configuration_io(method):
    """获取同步对锁之前先获取配置事务锁。"""

    @wraps(method)
    def wrapped(*args, **kwargs):
        """在共享偏好锁内调用被包装方法，串行化配置文件读写。"""
        with PREFS_LOCK:
            return method(*args, **kwargs)

    return wrapped


try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


def _hash_file(path: Path) -> str | None:
    """文件内容 sha1（不存在返回 None）。用于对账三方内容异同。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


def _read_bytes(path: Path) -> bytes | None:
    """读取文件全部字节；任何文件访问错误均返回 None。"""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _has_key_bindings(path: Path) -> bool | None:
    """该 ini 是否含软件会展示的 ``key=`` 按键项；读取失败返回 None。"""
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        from src.core.ini_keys import parse_ini_text

        text = data.decode("utf-8", errors="surrogateescape")
        return bool(parse_ini_text(text, str(path)))
    except Exception:
        return None


def _is_linked(path: Path) -> bool:
    """path 是否为链接（symlink 或 hardlink nlink>1）。判断是否须就地写以保链接。"""
    try:
        if path.is_symlink():
            return True
        if path.exists() and path.stat().st_nlink > 1:
            return True
    except OSError:
        return False
    return False


def _path_key(path: str | Path) -> str:
    """watchdog 路径索引键：绝对化 + 平台大小写规范化，但不 resolve 链接目标。"""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _atomic_write(path: Path, data: bytes) -> bool:
    """写入文件并保留符号链接或硬链接关系。

    链接文件就地截断写入，使另一端继续读取同一文件内容；普通文件使用原子替换。
    原子替换会改变硬链接所指向的文件身份，因此不能用于链接文件。失败时返回 False。
    """
    import os

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_linked(path):
            with open(path, "wb") as fh:  # 就地写入以保留文件身份和链接关系。
                fh.write(data)
            return True
        tmp = path.with_name(path.name + ".synctmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(str(tmp), str(path))
        return True
    except OSError as e:
        logger.warning("ini 同步写入失败 (%s): %s", path, e)
        return False
