"""插件运行时（宿主侧，插件不可见）。

包含 manifest 解析、事件总线、命令表、插件生命周期管理与熔断。
插件代码**不应** import 本包；它们只允许 import `src.plugin_sdk`。
"""

from src.plugins.runtime.contributions import Contribution, ContributionRegistry
from src.plugins.runtime.manager import PluginManager
from src.plugins.runtime.manifest import Manifest, ManifestError

__all__ = [
    "PluginManager",
    "Manifest",
    "ManifestError",
    "Contribution",
    "ContributionRegistry",
]
