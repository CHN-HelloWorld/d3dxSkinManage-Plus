"""插件 SDK，提供受控的宿主公开接口。

插件代码只允许 `from src.plugin_sdk import ...`，禁止直接 import `src.core.*` / `src.ui.*`。
插件通过本层访问宿主，不依赖内部模块的结构。
"""

from __future__ import annotations

# 公开接口版本，由宿主在加载插件前校验。
API_VERSION = "2.1"

from src.plugin_sdk.base import PluginBase, PluginMeta  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.configuration import ConfigurationChange  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.context import PluginContext  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.disposable import Disposable, DisposableStore  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.errors import PermissionDenied, PluginError  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.imports import ModImportRequest  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.operations import OperationResult, OperationTask  # noqa: E402 — 先定义版本，避免循环导入。
from src.plugin_sdk.tasks import BackgroundTask, TaskCancelled, TaskContext, TaskProgress, TaskResult  # noqa: E402 — 先定义版本，避免循环导入。

__all__ = [
    "API_VERSION",
    "PluginBase",
    "PluginMeta",
    "PluginContext",
    "PluginError",
    "PermissionDenied",
    "OperationResult",
    "OperationTask",
    "ModImportRequest",
    "Disposable",
    "DisposableStore",
    "ConfigurationChange",
    "BackgroundTask",
    "TaskContext",
    "TaskResult",
    "TaskProgress",
    "TaskCancelled",
]
