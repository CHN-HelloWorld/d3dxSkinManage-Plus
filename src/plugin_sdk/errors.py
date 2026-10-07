"""插件 SDK 的公开异常类型。"""

from __future__ import annotations

from src.utils.i18n import t as tr


class PluginError(Exception):
    """所有插件相关错误的基类。"""


class PermissionDenied(PluginError):
    """插件调用了清单权限未授权的方法。"""

    def __init__(self, permission: str, detail: str = "") -> None:
        """保存缺失权限标识，并组合可选详情生成本地化异常信息。"""
        self.permission = permission
        msg = tr("缺少权限: {permission}", permission=permission)
        if detail:
            msg = tr("{message}（{detail}）", message=msg, detail=detail)
        super().__init__(msg)
