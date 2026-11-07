"""将文件操作异常转换为可执行的用户指引，保留原始异常到日志。"""

from __future__ import annotations

import errno

from src.core.mod_identity import IdentityConflict
from src.core.operation_preflight import OperationPreflightError
from src.core.variant_operation import VariantRecoveryError
from src.utils.i18n import t as tr
from src.utils.logger import logger


def user_error_message(error: BaseException | str) -> str:
    """优先解释异常链中的文件访问原因，不向用户展示系统错误码或内部事务信息。"""
    if isinstance(error, BaseException):
        logger.warning("用户操作未完成: %s", error, exc_info=(type(error), error, error.__traceback__))
    causes = []
    seen = set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        causes.append(current)
        current = current.__cause__
    for cause in reversed(causes):
        if isinstance(cause, InterruptedError):
            return tr("操作已取消，未完成的改动不会继续应用。需要时可重新操作。")
        if isinstance(cause, FileNotFoundError):
            return tr("找不到所需的 Mod 文件或目录。请确认文件没有被移动或删除，刷新 Mod 列表后重试。")
        if isinstance(cause, FileExistsError):
            return tr("目标位置已有文件，为避免覆盖已有内容，操作已停止。请刷新列表，并更换名称或部署目录后重试。")
        if isinstance(cause, OSError) and getattr(cause, "winerror", None) in (32, 33):
            return tr(
                "系统报告 Mod 文件正在使用中，暂时无法修改。"
                "请关闭正在打开此 Mod 文件的窗口或编辑工具后重试；若仍失败，请提供日志以定位占用来源。"
            )
        if isinstance(cause, OSError) and (isinstance(cause, PermissionError) or getattr(cause, "winerror", None) == 5):
            return tr("无法修改 Mod 文件。请重试；若仍失败，请检查 Mod 库和部署目录的写入权限，并提供日志排查。")
        if isinstance(cause, OSError) and (cause.errno == errno.ENOSPC or getattr(cause, "winerror", None) == 112):
            return tr("磁盘剩余空间不足。请为 Mod 库和部署目录所在磁盘腾出空间后重试。")
    for cause in causes:
        if isinstance(cause, VariantRecoveryError):
            return tr(
                "Mod 配置与当前部署状态未能对齐，本次操作已停止以保护现有配置。"
                "请重试；若仍失败，请提供本次日志，程序会记录具体不一致的文件。"
            )
        if isinstance(cause, IdentityConflict):
            return tr(
                "此 Mod 的文件与列表记录不一致，为避免改动其他文件，操作已停止。"
                "请刷新 Mod 列表；若仍失败，请重新导入此 Mod。"
            )
        if isinstance(cause, OperationPreflightError):
            return tr("无法确认此 Mod 的文件状态。请确认文件仍在原位置、复制或修改已完成，刷新 Mod 列表后重试。")
        if isinstance(cause, (ValueError, OSError)) and any(
            hint in str(cause).lower()
            for hint in (
                "请",
                "先",
                "重试",
                "不能为空",
                "已存在",
                "please",
                "try again",
                "already",
                "enable",
                "choose",
                "enter",
            )
        ):
            return str(cause)
    return tr("操作未完成。请稍后重试；若仍失败，请打开日志并提供本次操作的记录，以便排查。")
