"""插件系统入口。

plugin_sdk 提供公开接口，runtime 负责发现、权限和生命周期。
插件包位于应用根目录的 plugins 子目录，由 PluginManager 管理。
"""

from src.plugins.runtime import Manifest, ManifestError, PluginManager

__all__ = ["PluginManager", "Manifest", "ManifestError"]
