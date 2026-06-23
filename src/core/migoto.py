"""3DMigoto 集成"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from src.utils.constants import MIGOTO_LOADER_NAME
from src.utils.logger import logger


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
        """保存游戏可执行文件路径，空值清除配置。"""
        self._game_exe_path = Path(path) if path else None

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
        """启动游戏"""
        if not self._game_exe_path or not self._game_exe_path.exists():
            logger.error("游戏路径未配置或不存在")
            return False

        try:
            subprocess.Popen(
                [str(self._game_exe_path)],
                cwd=str(self._game_exe_path.parent),
                creationflags=subprocess.DETACHED_PROCESS,
            )
            self.game_started.emit()
            logger.info("游戏已启动: %s", self._game_exe_path.name)
            return True
        except OSError as e:
            logger.error("启动游戏失败: %s", e)
            return False
