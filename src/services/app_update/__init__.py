"""协调应用版本检查、下载、安装与更新器清理。"""

from src.services.app_update.checker import AppUpdateChecker, AppUpdateInfo
from src.services.app_update.installer import AppUpdateInstaller

__all__ = [
    "AppUpdateChecker",
    "AppUpdateInfo",
    "AppUpdateInstaller",
    "AppUpdateManager",
]


def __getattr__(name: str):
    """延迟导出更新管理器以避免控件循环导入，未声明的属性抛出 AttributeError。"""
    # 版本说明控件依赖检查器；延迟加载管理器，避免与其持有的控件形成循环导入。
    if name == "AppUpdateManager":
        from src.services.app_update.manager import AppUpdateManager

        return AppUpdateManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
