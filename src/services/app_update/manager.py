"""编排更新检查、提示、下载、校验、更新器启动和主程序退出。

更新包解压至 <install>/Updater/Cache/extract，可按配置备份现有程序。
管理器通过带 HMAC 令牌的命令行启动 updater.exe；更新器等待主程序退出后
替换文件、清理并重启。自更新安装仅允许在打包后的可执行程序中执行。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from src.core.config import Config
from src.services.app_update.checker import AppUpdateChecker, AppUpdateInfo
from src.services.app_update.installer import AppUpdateInstaller
from src.services.app_update.progress import DownloadRate
from src.services.app_update.retirement import cleanup_retired_updaters
from src.services.network import NetworkService
from src.ui.dialogs.app_update_dialog import UpdatePromptDialog
from src.ui.dialogs.app_update_widgets import UpdateNoticeDialog, UpdateProgressDialog
from src.utils.constants import (
    APP_UPDATE_TOKEN_SECRET,
    APP_VERSION,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_app_dir, is_frozen
from src.utils.proxy import get_configured_proxy_url

_THROTTLE_SECONDS = 24 * 3600  # 启动静默检查节流：24h


class _CheckWorker(QThread):
    """在工作线程执行网络更新检查。"""

    finished_ok = Signal(object)  # AppUpdateInfo

    def __init__(self, checker: AppUpdateChecker, parent: QObject | None = None) -> None:
        """持有同步检查器并设置 Qt 父对象，网络工作在启动线程后执行。"""
        super().__init__(parent)
        self._checker = checker

    def run(self) -> None:
        """在工作线程执行版本检查，通过信号将结构化结果送回管理器。"""
        info = self._checker.check()
        self.finished_ok.emit(info)


class _InstallWorker(QThread):
    """在工作线程检查空间、解压更新包并按配置备份，通过信号汇报阶段和结果。

    文件准备不访问界面或共享数据库，更新器进程由管理器在准备完成后启动。
    """

    step = Signal(str)  # 步骤文本
    done = Signal(bool, str)  # (是否成功, 失败消息)

    def __init__(self, installer: AppUpdateInstaller, backup: bool, parent: QObject | None = None) -> None:
        """持有文件安装器及备份选项，由 Qt 父对象参与工作线程生命周期管理。"""
        super().__init__(parent)
        self._installer = installer
        self._backup = backup

    def run(self) -> None:
        """顺序执行磁盘空间检查、解压和可选备份，失败或异常通过结果信号报告。"""
        try:
            ok, msg = self._installer.check_disk_space()
            if not ok:
                self.done.emit(False, msg)
                return
            self.step.emit(tr("正在解压更新包…"))
            if not self._installer.extract_update():
                self.done.emit(False, tr("更新包解压失败，文件可能已损坏。"))
                return
            if self._backup:
                self.step.emit(tr("正在备份当前版本…"))
                self._installer.backup_current_version()
            self.step.emit(tr("正在启动更新器…"))
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, str(e))


class AppUpdateManager(QObject):
    """程序更新管理器（拥有提示/进度对话框，主窗口只需调用 check_for_updates）。"""

    check_started = Signal()
    check_finished = Signal(dict)  # AppUpdateInfo.to_dict()
    update_failed = Signal(str)

    def __init__(self, parent_window: QWidget | None = None, config: Config | None = None) -> None:
        """在界面线程建立网络服务、进度状态和定时器，打包运行时延迟启动旧更新器清理。"""
        super().__init__(parent_window)
        self._win = parent_window
        self._config = config or Config()
        self._network = NetworkService(self)

        self._check_worker: _CheckWorker | None = None
        self._download_worker = None
        self._progress: UpdateProgressDialog | None = None
        self._installer: AppUpdateInstaller | None = None
        self._install_worker: _InstallWorker | None = None
        self._install_progress: UpdateProgressDialog | None = None
        self._info: AppUpdateInfo | None = None
        self._silent = False
        self._is_busy = False
        # 下载候选源状态（任务5：下载源选择 + 失败回退）
        self._dl_info: AppUpdateInfo | None = None
        self._dl_candidates: list[str] = []
        self._dl_index = 0
        self._dl_save_path: Path | None = None
        self._dl_cancelled = False
        self._download_rate = DownloadRate()
        self._speed_timer = QTimer(self)
        self._speed_timer.setInterval(500)
        self._speed_timer.timeout.connect(self._update_download_speed)
        self._retirement_attempts = 0
        self._retirement_timer = QTimer(self)
        self._retirement_timer.setSingleShot(True)
        self._retirement_timer.setInterval(5000)
        self._retirement_timer.timeout.connect(self._cleanup_retired_updaters)
        if is_frozen():
            self._retirement_timer.start()

    def _cleanup_retired_updaters(self) -> None:
        """尝试清理待退役更新器，仍有遗留时以单次定时器重试，总尝试次数不超过五次。"""
        self._retirement_timer.stop()
        self._retirement_attempts += 1
        if cleanup_retired_updaters(get_app_dir()) and self._retirement_attempts < 5:
            self._retirement_timer.start()

    # 对外入口

    def check_on_startup(self) -> None:
        """启动后静默检查（受总开关 / 启动开关 / 24h 节流约束）。"""
        if not self._config.get("app_update.enabled", True):
            return
        if not self._config.get("app_update.on_startup", True):
            return
        last = float(self._config.get("app_update.last_check_time", 0) or 0)
        if time.time() - last < _THROTTLE_SECONDS:
            logger.info("程序更新：未到检查节流间隔，跳过启动检查")
            return
        self.check_for_updates(silent=True)

    def check_for_updates(self, silent: bool = False) -> None:
        """检查更新。silent=True 时不弹「已是最新 / 失败」提示，仅在有更新时打扰。"""
        if self._is_busy:
            logger.info("程序更新：已有任务进行中，忽略重复请求")
            return
        if not self._config.get("app_update.enabled", True) and not silent:
            self._info_box(tr("程序更新"), tr("程序自动更新已在设置中关闭。"))
            return

        self._is_busy = True
        self._silent = silent
        self.check_started.emit()

        repo = (self._config.get("app_update.repo", "") or "").strip() or None
        checker = AppUpdateChecker(
            current_version=APP_VERSION,
            repo=repo,
            # 任务3：软件更新无条件走代理（仅受代理总开关约束），与 Mod 商店「启用代理」无关
            proxy=get_configured_proxy_url(self._config),
        )
        self._check_worker = _CheckWorker(checker, self)
        self._check_worker.finished_ok.connect(self._on_check_finished)
        self._check_worker.start()

    # 检查结果

    def _on_check_finished(self, info: AppUpdateInfo) -> None:
        """保存检查时间并发布结果，按静默模式和跳过版本设置决定错误提示或更新对话框。"""
        self._is_busy = False
        self._info = info
        self._config.set("app_update.last_check_time", time.time())
        self.check_finished.emit(info.to_dict())

        if info.error:
            logger.warning("程序更新检查失败: %s", info.error)
            if not self._silent:
                self._error_box(tr("检查更新失败"), info.error)
            return

        if not info.has_update:
            logger.info("程序更新：当前已是最新版本")
            if not self._silent:
                self._prompt_update(info)
            return

        # 静默模式下尊重「跳过此版本」
        if self._silent and self._config.get("app_update.skipped_version", "") == info.latest_version:
            logger.info("程序更新：版本 %s 已被用户跳过", info.latest_version)
            return

        self._prompt_update(info)
