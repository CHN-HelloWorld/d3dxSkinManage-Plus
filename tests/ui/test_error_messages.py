"""验证用户提示提供处理步骤且不泄露系统错误码和内部异常细节。"""

import errno

import pytest

from src.core.operation_preflight import OperationPreflightError
from src.core.variant_operation import VariantRecoveryError
from src.ui.error_messages import user_error_message


@pytest.mark.parametrize(
    ("error", "guidance"),
    [
        (PermissionError("[WinError 5] internal path"), "权限"),
        (FileNotFoundError("internal path"), "刷新"),
        (FileExistsError("deployment invariant conflict"), "避免覆盖"),
        (OSError(errno.ENOSPC, "disk full"), "腾出空间"),
        (VariantRecoveryError("INI 清单已被外部修改，无法应用陈旧声明"), "重试"),
        (RuntimeError("internal publication failed"), "日志"),
        (ValueError("invalid internal payload"), "日志"),
    ],
)
def test_error_message_explains_next_step_without_internal_details(error, guidance):
    message = user_error_message(error)
    assert guidance in message
    assert "WinError" not in message
    assert "陈旧声明" not in message
    assert "internal" not in message


def test_preflight_error_explains_original_file_access_failure():
    error = OperationPreflightError("mod", "hidden path", "unreadable")
    error.__cause__ = PermissionError("[WinError 5]")
    assert "权限" in user_error_message(error)


def test_actionable_validation_message_is_retained():
    assert user_error_message(ValueError("请先启用 Mod，再保存变体")) == "请先启用 Mod，再保存变体"
