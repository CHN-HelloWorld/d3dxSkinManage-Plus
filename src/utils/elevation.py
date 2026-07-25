"""在 Windows 启动时确保应用具备管理员权限。

部署和用户 INI 操作可能访问受保护路径。权限不足时通过 ShellExecuteW 的 runas
重新启动应用，原进程退出；用户拒绝或启动失败时显示说明。
打包程序通常由应用清单请求提权，本模块也支持从 Python 脚本启动。

is_admin 判断当前管理员权限，不等同于拖放适配中的 UAC 提权状态判断。
UAC 关闭时管理员账户仍应通过权限检查，避免重复启动。
"""

from __future__ import annotations

import ctypes
import os
import sys

from src.utils.i18n import t as tr
from src.utils.logger import logger

_IS_WIN = sys.platform == "win32"

# 显式跳过启动权限检查的命令行选项，重新启动时不传递该选项。
NO_ELEVATE_FLAG = "--no-elevate"

_SW_SHOWNORMAL = 1
#: ShellExecuteW 返回值 <= 32 即失败；5 = ERROR_ACCESS_DENIED，即用户点了「否」。
_SHELL_EXECUTE_MIN_OK = 32
_ERROR_CANCELLED = 5


def is_admin() -> bool:
    """判断当前进程是否具备管理员权限，包括管理员账户关闭 UAC 的情况。"""
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("管理员权限检测失败，按「非管理员」处理: %s", exc)
        return False


def get_relaunch_command() -> tuple[str, list[str], str]:
    """构造重新启动所需的程序、参数和工作目录。

    可执行文件从 argv[0] 获取，避免打包环境中的 sys.executable 指向不存在的解释器。
    脚本使用当前 Python 解释器启动，不依赖系统文件关联。
    """
    argv0 = os.path.abspath(sys.argv[0])
    if argv0.lower().endswith(".exe"):
        program = argv0
        arguments = list(sys.argv[1:])
    else:
        program = sys.executable
        arguments = [argv0] + list(sys.argv[1:])
    # 保留当前工作目录，避免提权后默认进入系统目录。
    try:
        working_dir = os.getcwd()
    except OSError:
        working_dir = os.path.dirname(program)
    return program, arguments, working_dir or os.path.dirname(program)


def _relaunch_as_admin() -> int:
    """以管理员权限启动应用，返回 ShellExecuteW 的结果。"""
    program, arguments, working_dir = get_relaunch_command()
    # 按 Windows 命令行规则引用各参数，保留路径中的空格、括号和非 ASCII 字符。
    params = " ".join(f'"{a}"' for a in arguments)
    logger.info("正在以管理员身份重启: %s %s (cwd=%s)", program, params, working_dir)
    return int(
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None,
            "runas",
            program,
            params,
            working_dir,
            _SW_SHOWNORMAL,
        )
    )


def _message_box(text: str, title: str) -> None:
    """使用系统错误消息框报告启动失败，支持没有控制台的运行方式。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # type: ignore[attr-defined]
    except Exception:
        pass


def ensure_elevated(app_name: str) -> int | None:
    """检查启动权限并在必要时请求提权。

    返回 None 表示继续启动；返回整数时当前进程应使用该退出码结束。
    零表示管理员进程已启动，三表示提权失败或被拒绝且已显示提示。
    非 Windows 平台不执行提权。
    """
    if not _IS_WIN:
        return None
    if NO_ELEVATE_FLAG in sys.argv:
        logger.warning("检测到 %s，跳过管理员权限检查", NO_ELEVATE_FLAG)
        return None
    if is_admin():
        logger.info("已以管理员权限运行")
        return None

    logger.info("当前非管理员权限，准备请求提权")
    try:
        ret = _relaunch_as_admin()
    except Exception as exc:
        logger.error("请求管理员权限时异常: %s", exc)
        _message_box(
            tr(
                "{app_name} 需要管理员权限才能运行，但请求提权时出错：\n{exc}\n\n请右键以管理员身份运行。",
                app_name=app_name,
                exc=exc,
            ),
            app_name,
        )
        return 3

    if ret > _SHELL_EXECUTE_MIN_OK:
        logger.info("管理员进程已启动，本进程退出")
        return 0

    if ret == _ERROR_CANCELLED:
        logger.warning("用户在 UAC 提示中拒绝了提权")
        _message_box(
            tr(
                "{app_name} 需要管理员权限才能运行。\n\n请在 UAC 提示中点击「是」，或右键以管理员身份运行。",
                app_name=app_name,
            ),
            app_name,
        )
    else:
        logger.error("ShellExecuteW(runas) 失败，返回值 %s", ret)
        _message_box(
            tr(
                "{app_name} 需要管理员权限才能运行，但提权失败（代码 {ret}）。\n\n请右键以管理员身份运行。",
                app_name=app_name,
                ret=ret,
            ),
            app_name,
        )
    return 3
