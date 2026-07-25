"""管理控制台及文件日志、异常记录和保留期清理。

导入时创建全局日志器，应用启动时附加文件处理器。日志按日期及启动序号分文件，
同时清理主程序和更新器的过期日志。

文件处理器覆盖应用与根日志器，应用日志关闭传播以避免重复。
线程异常、Qt 原生日志和异步任务异常分别由对应处理器接入。
"""

from __future__ import annotations

import logging
import shutil
import stat
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

_LOG_NAME = "d3dxSkinManage-Plus"
_FORMATTER = logging.Formatter(
    "[%(asctime)s] [%(levelname)-7s] [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_DEFAULT_RETENTION_DAYS = 14
# 第三方日志默认记录 INFO 及以上级别，保留请求结果并限制逐数据包调试日志。
_THIRD_PARTY_LEVEL = logging.INFO

# 当前文件日志路径（未启用文件日志时为 None）——供应用内日志查看器读取
_log_file: Path | None = None


def setup_logger(
    name: str = _LOG_NAME,
    log_dir: str | Path | None = None,
    level: int = logging.DEBUG,
    console: bool = True,
) -> logging.Logger:
    """配置并返回 logger。

    可重复调用：控制台和文件处理器分别只附加一次。

    参数：
        name: logger 名称
        log_dir: 日志输出目录；为 None 时不写文件，可后续通过 attach_file_logging 启用
        level: 日志级别
        console: 是否输出到控制台
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 精确匹配 StreamHandler，避免将其子类 FileHandler 误认为已有控制台处理器。
    has_console = any(type(h) is logging.StreamHandler for h in logger.handlers)
    if console and not has_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(_FORMATTER)
        logger.addHandler(console_handler)

    if log_dir:
        attach_file_logging(log_dir, logger=logger, level=level)

    return logger


def attach_file_logging(
    log_dir: str | Path,
    *,
    logger: logging.Logger | None = None,
    level: int = logging.DEBUG,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> Path | None:
    """为本次运行附加文件日志并清理过期日志。

    重复调用复用同一处理器，返回当前日志路径；目录不可写时返回 None。
    """
    global _log_file
    log = logger if logger is not None else logging.getLogger(_LOG_NAME)

    # 复用本次运行已有的文件日志。
    for h in log.handlers:
        if isinstance(h, logging.FileHandler):
            _log_file = Path(h.baseFilename)
            return _log_file

    try:
        log_root = Path(log_dir)
        # 按天分目录，文件名 = 当天启动序号（顺延已有最大序号 +1）
        day_dir = log_root / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        log_file = _next_session_file(day_dir).resolve()

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(_FORMATTER)
        log.addHandler(file_handler)
        _log_file = log_file
        _attach_third_party_logging(log, file_handler)

        _cleanup_old_logs(log_root, retention_days)
        log.info("文件日志已启用：%s（按天分目录，保留 %d 天）", log_file, retention_days)
        return log_file
    except OSError as e:
        # 写入磁盘失败不影响程序运行（控制台 handler 仍在）
        log.warning("启用文件日志失败：%s", e)
        return None


def _attach_third_party_logging(
    app_logger: logging.Logger,
    file_handler: logging.Handler,
) -> None:
    """将应用与第三方库日志写入同一文件。

    共用带锁的处理器，避免多个文件对象交错写入同一路径。
    应用日志关闭向根日志器传播，防止重复记录。
    """
    app_logger.propagate = False
    root = logging.getLogger()
    # 只放宽根日志阈值，避免 httpx 的 INFO 记录在到达文件处理器前被过滤。
    if root.level == logging.NOTSET or root.level > _THIRD_PARTY_LEVEL:
        root.setLevel(_THIRD_PARTY_LEVEL)
    if file_handler not in root.handlers:
        root.addHandler(file_handler)
    # 控制台存在时也输出第三方日志。
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(_THIRD_PARTY_LEVEL)
        console.setFormatter(_FORMATTER)
        root.addHandler(console)
    # 限制 httpcore 的详细连接日志，保留 httpx 请求结果。
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _next_session_file(day_dir: Path) -> Path:
    """当天目录内的下一个 session 日志路径：已有最大数字序号 +1（从 1 起）。

    不持久化"今天第几次启动"，纯按目录里已有 ``<n>.log`` 顺延。
    """
    max_seq = 0
    for f in day_dir.glob("*.log"):
        try:
            max_seq = max(max_seq, int(f.stem))
        except ValueError:
            continue
    return day_dir / f"{max_seq + 1}.log"


def _cleanup_old_logs(log_root: Path, retention_days: int) -> None:
    """按同一保留期清理主程序日期目录和更新器日志，其它文件/目录保持原样。"""
    if retention_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    if not log_root.exists():
        return
    _cleanup_updater_logs(log_root, cutoff)
    for d in log_root.iterdir():
        if not d.is_dir():
            continue
        try:
            stamp = datetime.strptime(d.name, "%Y-%m-%d")
        except ValueError:
            continue  # 更新器已单独处理，其它非日期目录不动
        if stamp < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _cleanup_updater_logs(log_root: Path, cutoff: datetime) -> None:
    """清理 updater 子目录中的过期日志；不穿过链接，失败留待下次启动重试。"""
    directory = log_root / "updater"
    name_format = "%Y%m%d_%H%M%S.log"
    try:
        for parent in (log_root, directory):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                return
        for log_file in directory.iterdir():
            try:
                stamp = datetime.strptime(log_file.name, name_format)
                if stamp.strftime(name_format) != log_file.name:
                    continue  # strptime 允许部分字段省略前导零，清理时要求精确命名
                info = log_file.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    continue
                # 与主程序日期目录一致，按日志日期判断，不依赖文件修改时间。
                if stamp.replace(hour=0, minute=0, second=0) < cutoff:
                    log_file.unlink()
            except (OSError, ValueError):
                continue
    except OSError:
        return


def get_log_file() -> Path | None:
    """当前文件日志路径；未启用文件日志时为 None。"""
    return _log_file


_excepthook_installed = False
_qt_handler_installed = False


def install_qt_message_handler() -> None:
    """将 Qt 的各级日志接入 Python 日志系统。

    在创建 QApplication 前安装 qInstallMessageHandler，确保无控制台程序
    也能记录 Qt 初始化错误。重复安装不会增加处理器。
    """
    global _qt_handler_installed
    if _qt_handler_installed:
        return
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as e:
        logger.debug("Qt 消息钩子安装跳过：%s", e)
        return

    _level_of = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_logger = logging.getLogger("Qt")

    def _handler(mode, context, message):
        """将 Qt 消息及来源位置转发到 Python 日志，阻止日志异常越过原生回调边界。"""
        try:
            where = ""
            if getattr(context, "file", None):
                where = f" [{context.file}:{context.line}]"
            qt_logger.log(_level_of.get(mode, logging.INFO), "%s%s", message, where)
        except Exception:
            pass

    qInstallMessageHandler(_handler)
    _qt_handler_installed = True


def init_app_logging() -> Path | None:
    """初始化文件日志与未捕获异常处理器，可重复调用。

    所有应用入口共用此初始化，使无控制台运行也能保存日志和异常信息。
    """
    global _excepthook_installed
    log_file = None
    try:
        from src.utils.paths import get_log_dir

        log_file = attach_file_logging(get_log_dir())
    except Exception as e:
        logger.warning("文件日志初始化失败：%s", e)

    if not _excepthook_installed:
        _prev_hook = sys.excepthook

        def _hook(exc_type, exc, tb):
            """记录主线程未捕获异常，再交给此前安装的异常处理器。"""
            if not issubclass(exc_type, KeyboardInterrupt):
                logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))
            _prev_hook(exc_type, exc, tb)

        sys.excepthook = _hook

        # 线程异常使用独立钩子接入日志，补充仅处理主线程的 sys.excepthook。
        _prev_thread_hook = threading.excepthook

        def _thread_hook(args):
            """记录线程名称及未捕获异常，再交给此前安装的线程异常处理器。"""
            if not issubclass(args.exc_type, KeyboardInterrupt):
                logger.critical(
                    "子线程未捕获异常（thread=%s）",
                    getattr(args.thread, "name", "?"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            _prev_thread_hook(args)

        threading.excepthook = _thread_hook
        _excepthook_installed = True

    return log_file


# 全局 logger（导入即建 console handler；文件日志在 app 启动时 attach）
logger = setup_logger()
