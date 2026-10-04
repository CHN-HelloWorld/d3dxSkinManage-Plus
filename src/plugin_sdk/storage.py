"""插件隔离 KV 存储（公开 API）。

每个插件一个独立目录 `plugins_data/<id>/store.json`，卸载时保留数据供重新安装使用。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.errors import PermissionDenied, PluginError
from src.utils.i18n import t as tr


class PluginStorage(ContextResource):
    """插件私有的持久化 KV 存储（JSON 写入磁盘，进程内缓存）。"""

    def __init__(self, data_dir: Path, *, granted: frozenset[str] | None = None) -> None:
        """绑定插件独立数据目录及 store.json，内容在首次访问时读取。"""
        super().__init__()
        self._granted = granted
        self._dir = data_dir
        self._file = data_dir / "store.json"
        self._cache: dict[str, Any] | None = None

    # 内部

    def _load(self) -> dict[str, Any]:
        """首次读取并缓存 JSON 对象，文件缺失使用空字典，损坏内容明确报错。"""
        self._require_open()
        if self._granted is not None and "storage" not in self._granted:
            raise PermissionDenied("storage")
        if self._cache is None:
            try:
                if self._file.exists():
                    data = json.loads(self._file.read_text("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError(tr("存储内容必须是 JSON 对象"))
                    json.dumps(data, allow_nan=False)
                    self._cache = data
                else:
                    self._cache = {}
            except (OSError, ValueError, TypeError) as exc:
                raise PluginError(tr("读取插件存储失败：{error}", error=exc)) from exc
        return self._cache

    def _flush(self, data: dict[str, Any]) -> None:
        """创建数据目录后先写临时 JSON，再替换存储文件，失败时传递异常。"""
        content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        committed = json.loads(content)
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        try:
            tmp.write_text(content + "\n", encoding="utf-8", newline="\n")
            tmp.replace(self._file)
        finally:
            tmp.unlink(missing_ok=True)
        self._cache = committed

    # 公开

    def get(self, key: str, default: Any = None) -> Any:
        """读取指定键，不存在时返回调用方提供的默认值。"""
        return deepcopy(self._load().get(key, default))

    def set(self, key: str, value: Any) -> None:
        """先原子保存键值，再发布内存缓存，失败时保留原状态。"""
        if not isinstance(key, str) or not key:
            raise ValueError(tr("存储键必须是非空字符串"))
        data = dict(self._load())
        data[key] = value
        self._flush(data)

    def delete(self, key: str) -> None:
        """删除存在的键并持久化；键不存在时不写文件。"""
        if key in self._load():
            data = dict(self._load())
            del data[key]
            self._flush(data)

    def all(self) -> dict[str, Any]:
        """返回完整副本，避免嵌套数据修改绕过持久化接口。"""
        return deepcopy(self._load())

    @property
    def dir(self) -> Path:
        """插件私有数据目录（可放缓存等）。"""
        self._load()
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir
