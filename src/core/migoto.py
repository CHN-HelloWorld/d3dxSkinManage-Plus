"""3DMigoto 集成"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from src.utils.constants import MIGOTO_LOADER_NAME
from src.utils.logger import logger


def _split_game_command(command: str) -> tuple[Path, str]:
    """分离已存在的程序与原始参数，带引号的路径明确取界，未加引号时优先匹配完整路径。"""
    value = command.strip()
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0 or (end + 1 < len(value) and not value[end + 1].isspace()):
            raise ValueError("程序路径引号未闭合，或路径与参数之间缺少空格")
        executable = Path(value[1:end])
        if not executable.is_file():
            raise FileNotFoundError(executable)
        return executable, value[end + 1 :].lstrip()

    # 保留纯路径配置；从最长前缀开始，避免把路径内部的空格当成参数分隔符。
    boundaries = [len(value), *(match.start() for match in reversed(list(re.finditer(r"\s+", value))))]
    for end in boundaries:
        try:
            executable = Path(value[:end])
            if executable.is_file():
                return executable, value[end:].lstrip()
        except (OSError, ValueError):
            continue
    raise FileNotFoundError("游戏可执行文件不存在")


class MigotoIntegration(QObject):
    """3DMigoto 集成管理

    处理 3DMigoto 的启动、配置读取等功能。
    """

    migoto_started = Signal()
    game_started = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        """初始化 Qt 对象并将注入器与游戏可执行文件路径设为未配置。"""
        super().__init__(parent)
        self._migoto_path: Path | None = None
        self._game_exe_path: Path | None = None
        self._game_command = ""

    @property
    def migoto_path(self) -> Path | None:
        """返回当前配置的注入器目录，未设置时返回 None。"""
        return self._migoto_path

    @migoto_path.setter
    def migoto_path(self, path: str | Path) -> None:
        """将注入器目录转换为路径对象，空值清除配置。"""
        self._migoto_path = Path(path) if path else None

    @property
    def game_exe_path(self) -> Path | None:
        """返回当前游戏可执行文件路径，未设置时返回 None。"""
        return self._game_exe_path

    @game_exe_path.setter
    def game_exe_path(self, path: str | Path) -> None:
        """保留路径属性的兼容行为，另存原始命令，避免 Path 改写参数中的反斜线。"""
        self._game_exe_path = Path(path) if path else None
        self._game_command = str(path).strip() if path else ""

    @property
    def has_loader(self) -> bool:
        """检查根目录中的独立加载器程序是否存在。

        结果仅决定加载器启动按钮是否可用。部分集成环境由外部启动器加载，
        没有独立加载器程序仍可使用其 INI、部署和运行状态功能。
        """
        if not self._migoto_path:
            return False
        try:
            return (self._migoto_path / MIGOTO_LOADER_NAME).is_file()
        except OSError:
            return False

    def launch_migoto(self) -> bool:
        """启动 3DMigoto Loader"""
        if not self._migoto_path:
            logger.error("3DMigoto 路径未配置")
            return False

        loader = self._migoto_path / MIGOTO_LOADER_NAME
        if not loader.exists():
            logger.error("3DMigoto Loader 不存在: %s", loader)
            return False

        try:
            subprocess.Popen(
                [str(loader)],
                cwd=str(self._migoto_path),
                creationflags=subprocess.DETACHED_PROCESS,
            )
            self.migoto_started.emit()
            logger.info("3DMigoto 已启动")
            return True
        except OSError as e:
            logger.error("启动 3DMigoto 失败: %s", e)
            return False

    def launch_game(self) -> bool:
        """直接启动已解析的游戏程序，将原始参数交给 Windows，工作目录保持为程序所在目录。"""
        if not self._game_command:
            logger.error("游戏路径未配置")
            return False

        try:
            executable, arguments = _split_game_command(self._game_command)
            command = subprocess.list2cmdline([str(executable)])
            if arguments:
                command += " " + arguments
            subprocess.Popen(
                command,
                executable=str(executable),
                cwd=str(executable.parent),
                creationflags=subprocess.DETACHED_PROCESS,
            )
            self.game_started.emit()
            logger.info("游戏已启动: %s", executable.name)
            return True
        except (OSError, ValueError) as e:
            logger.error("启动游戏失败: %s", e)
            return False
