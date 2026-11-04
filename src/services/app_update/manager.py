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
import weakref
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal
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
_active_workers: set[QThread] = set()
_connected_apps: weakref.WeakSet[QCoreApplication] = weakref.WeakSet()


def _finish_update_workers() -> None:
    """应用退出时等待更新准备线程，避免父窗口销毁后仍有线程访问原生对象。"""
    for worker in list(_active_workers):
        try:
            worker.wait()
        except RuntimeError:
            pass


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
        # 记录下载候选源和失败后的回退位置。
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

    def _track_worker(self, worker: QThread) -> None:
        """仅在原生工作期保活更新线程，允许管理器先销毁，并在结束后清除当前属性引用。"""
        worker.setParent(None)
        _active_workers.add(worker)
        manager_ref = weakref.ref(self)

        def cleanup() -> None:
            """原生线程已退出时解除登记；旧检查的结束不能清除新检查或安装线程。"""
            manager = manager_ref()
            if manager is not None:
                if manager._check_worker is worker:
                    manager._check_worker = None
                if manager._install_worker is worker:
                    manager._install_worker = None
            _active_workers.discard(worker)
            try:
                worker.deleteLater()
            except RuntimeError:
                pass

        worker.finished.connect(cleanup, Qt.ConnectionType.QueuedConnection)
        app = QCoreApplication.instance()
        if app is not None and app not in _connected_apps:
            _connected_apps.add(app)
            app.aboutToQuit.connect(_finish_update_workers)

    # 对外入口

    def check_on_startup(self) -> None:
        """启动后按启动检查偏好静默检查，距上次检查不足 24 小时则跳过。"""
        if not self._config.get("app_update.on_startup", True):
            return
        last = float(self._config.get("app_update.last_check_time", 0) or 0)
        if time.time() - last < _THROTTLE_SECONDS:
            logger.info("程序更新：未到检查节流间隔，跳过启动检查")
            return
        self.check_for_updates(silent=True)

    def check_for_updates(self, silent: bool = False) -> None:
        """独立于启动检查偏好和节流执行检查，静默模式仅在发现新版本时提示。"""
        if self._is_busy:
            logger.info("程序更新：已有任务进行中，忽略重复请求")
            return
        self._is_busy = True
        self._silent = silent
        self.check_started.emit()

        repo = (self._config.get("app_update.repo", "") or "").strip() or None
        checker = AppUpdateChecker(
            current_version=APP_VERSION,
            repo=repo,
            # 程序更新遵循代理总开关，不受 Mod 商店的代理设置影响。
            proxy=get_configured_proxy_url(self._config),
        )
        self._check_worker = _CheckWorker(checker, self)
        self._track_worker(self._check_worker)
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

    def _prompt_update(self, info: AppUpdateInfo) -> None:
        """显示版本说明并处理更新、跳过或稍后动作，只有发现新版本时才开始下载或保存跳过选择。"""
        dialog = UpdatePromptDialog(info.to_dict(), parent=self._win)
        try:
            dialog.exec()
            action = dialog.user_action
        finally:
            try:
                dialog.deleteLater()
            except RuntimeError:
                pass
        if action == "update" and info.has_update:
            self._start_download(info)
        elif action == "skip" and info.has_update:
            self._config.set("app_update.skipped_version", info.latest_version)
            logger.info("程序更新：用户跳过版本 %s", info.latest_version)
        else:
            logger.info("程序更新：用户选择稍后")

    # 下载

    def _start_download(self, info: AppUpdateInfo) -> None:
        """在打包环境准备下载缓存、候选源及进度对话框，然后启动首个下载源。"""
        if not is_frozen():
            self._info_box(
                tr("开发环境"),
                tr("检测到当前为源码/开发环境，已跳过自动安装。\n自更新仅在打包后的程序上生效。"),
            )
            return
        if not info.download_url:
            self._error_box(tr("下载失败"), tr("本次发布未找到 .zip 更新包资源。"))
            return

        self._is_busy = True
        self._dl_info = info
        self._dl_cancelled = False
        save_dir = get_app_dir() / "Updater" / "Cache" / "download"
        save_dir.mkdir(parents=True, exist_ok=True)
        self._dl_save_path = save_dir / (info.download_url.split("/")[-1] or "update.zip")

        # 按下载源设置构造候选地址，自动模式优先镜像并在失败后回退。
        self._dl_candidates = self._build_download_candidates(info.download_url)
        self._dl_index = 0

        self._progress = UpdateProgressDialog(tr("下载更新"), tr("正在下载更新包…"), self._win)
        self._progress.canceled.connect(self._on_download_cancel)
        self._progress.show()

        self._try_next_download()

    def _build_download_candidates(self, url: str) -> list[str]:
        """据 app_update.download_source 生成候选下载 URL（按尝试顺序）。"""
        from src.utils.constants import APP_UPDATE_DOWNLOAD_MIRRORS

        source = (self._config.get("app_update.download_source", "auto") or "auto").strip()
        prefixes = {key: prefix for key, _name, prefix in APP_UPDATE_DOWNLOAD_MIRRORS}

        def apply(prefix: str) -> str:
            """为原始下载地址添加镜像前缀，空前缀保留直连地址。"""
            return f"{prefix}{url}" if prefix else url

        if source != "auto" and source in prefixes:
            return [apply(prefixes[source])]

        # 自动模式优先使用镜像，最后使用官方地址，并按配置顺序去重。
        ordered = [p for k, _n, p in APP_UPDATE_DOWNLOAD_MIRRORS if p]  # 非空=镜像
        ordered.append("")  # 官方直连垫底
        seen, cands = set(), []
        for p in ordered:
            u = apply(p)
            if u not in seen:
                seen.add(u)
                cands.append(u)
        return cands

    def _try_next_download(self) -> None:
        """尝试候选列表中的下一个源；全部失败则报错。"""
        if self._dl_cancelled:
            return
        if self._dl_index >= len(self._dl_candidates):
            self._is_busy = False
            self._close_progress()
            self.update_failed.emit(tr("所有下载源均失败"))
            self._error_box(tr("下载失败"), tr("所有下载源均无法获取更新包，请检查网络/代理或稍后重试。"))
            return
        url = self._dl_candidates[self._dl_index]
        self._dl_index += 1
        n, total = self._dl_index, len(self._dl_candidates)
        logger.info("程序更新：尝试下载源 %d/%d -> %s", n, total, url)
        if self._progress:
            self._progress.setLabelText(tr("正在下载更新包…（源 {n}/{total}）", n=n, total=total))
            self._progress.setDownloadSpeed(None)
        self._download_rate.reset()
        self._speed_timer.start()
        self._download_worker = self._network.download_file(
            url=url,
            save_path=self._dl_save_path,
            on_progress=self._on_download_progress,
            on_finished=self._on_download_finished,
            on_error=self._on_download_error,
            # 更新下载遵循代理总开关，代理失败或超时后回退直连。
            proxy=get_configured_proxy_url(self._config),
        )

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        """更新已接收字节和速度采样，总大小未知时将进度框切换为不确定进度。"""
        if not self._progress:
            return
        self._download_rate.record(downloaded)
        self._update_download_speed()
        if total > 0:
            self._progress.setMaximum(100)
            self._progress.setValue(int(downloaded / total * 100))
            self._progress.setLabelText(
                tr(
                    "正在下载更新包…  {value:.1f} / {value_2:.1f} MB",
                    value=downloaded / 1024 / 1024,
                    value_2=total / 1024 / 1024,
                )
            )
        else:
            self._progress.setMaximum(0)  # 不确定进度
            self._progress.setLabelText(tr("已下载 {value:.1f} MB", value=downloaded / 1024 / 1024))

    def _update_download_speed(self) -> None:
        """仅在下载进度框存在且未取消时刷新采样速度。"""
        if self._progress is not None and not self._dl_cancelled:
            self._progress.setDownloadSpeed(self._download_rate.sample())

    def _on_download_cancel(self) -> None:
        """标记下载取消并停止速度定时器，向工作线程发出取消请求后解除忙碌状态。"""
        self._dl_cancelled = True
        self._close_progress()
        if self._download_worker:
            try:
                self._download_worker.cancel()
            except Exception:
                pass
        self._is_busy = False
        logger.info("程序更新：用户取消下载")

    def _on_download_error(self, message: str) -> None:
        """未取消时记录当前源失败并尝试下一个候选下载地址。"""
        if self._dl_cancelled:
            return
        logger.warning("程序更新：下载源失败（%s），尝试下一个源", message)
        self._try_next_download()

    def _on_download_finished(self, file_path: str) -> None:
        """检查下载包最小预期大小及可选 SHA256，校验失败换源，通过后关闭进度并进入安装准备。"""
        if self._dl_cancelled:
            return
        info = self._dl_info
        path = Path(file_path)

        # 校验①：大小（拦截镜像错误页/残包冒充完整包）→ 失败则换下一个源
        if info.download_size > 0:
            try:
                actual = path.stat().st_size
            except OSError:
                actual = 0
            if actual < info.download_size * 0.99:
                logger.warning("程序更新：源返回残包(%d/%d)，换下一个源", actual, info.download_size)
                self._safe_unlink(path)
                self._try_next_download()
                return

        # 校验②：SHA256（发布说明提供时才强校验）→ 失败则换下一个源
        if info.sha256 and not self._verify_sha256(path, info.sha256):
            logger.warning("程序更新：源 SHA256 不符，换下一个源")
            self._safe_unlink(path)
            self._try_next_download()
            return

        self._close_progress()

        self._install(path)

    # 安装：解压 → 备份 → 拉起 updater.exe → 退出

    def _install(self, zip_path: Path) -> None:
        """在工作线程解压并备份，完成后由主线程启动更新器并退出应用。"""
        self._installer = AppUpdateInstaller(zip_path)
        self._install_progress = UpdateProgressDialog(tr("安装更新"), tr("正在准备安装…"), self._win, cancellable=False)
        self._install_progress.show()

        backup = bool(self._config.get("app_update.backup_before_update", True))
        self._install_worker = _InstallWorker(self._installer, backup, self)
        self._track_worker(self._install_worker)
        self._install_worker.step.connect(self._on_install_step)
        self._install_worker.done.connect(self._on_install_done)
        self._install_worker.start()

    def _on_install_step(self, text: str) -> None:
        """在安装进度框仍存在时显示工作线程报告的准备阶段。"""
        if self._install_progress is not None:
            self._install_progress.setLabelText(text)

    def _on_install_done(self, ok: bool, message: str) -> None:
        """安装线程结束（主线程槽）：成功则拉起更新器并退出；失败则报错。"""
        if not ok:
            self._close_install_progress()
            self._is_busy = False
            if self._installer:
                self._installer.cleanup_extract()
            self.update_failed.emit(message)
            self._error_box(tr("安装失败"), message)
            return

        updater_exe = self._installer.locate_updater()
        if updater_exe is None:
            self._close_install_progress()
            self._is_busy = False
            self._error_box(
                tr("更新器缺失"),
                tr(
                    "未找到更新器 updater.exe（应位于程序目录的 Updater/ 下）。\n"
                    "请手动解压更新包完成更新，或重新安装完整版。"
                ),
            )
            return

        args = self._installer.prepare_for_updater()
        token = self._generate_launch_token()
        cmd = [
            str(updater_exe),
            str(args["pid"]),
            str(args["extract_dir"]),
            str(args["install_dir"]),
            json.dumps(args["preserve_files"]),
            token,
        ]
        logger.info("程序更新：拉起更新器 %s", " ".join(cmd))

        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        try:
            subprocess.Popen(cmd, creationflags=creationflags)
        except Exception as e:
            self._close_install_progress()
            self._is_busy = False
            logger.error("程序更新：拉起更新器失败: %s", e)
            self.update_failed.emit(str(e))
            self._error_box(tr("启动更新器失败"), tr("无法启动更新器：{e}", e=e))
            return

        self._close_install_progress()
        logger.info("程序更新：更新器已启动，退出主程序以让其替换文件")
        QApplication.quit()

    def _close_install_progress(self) -> None:
        """结束安装进度框并解除持有引用，容忍对话框已经关闭或释放。"""
        if self._install_progress is not None:
            dialog, self._install_progress = self._install_progress, None
            try:
                dialog.finish()
            except Exception:
                pass
            finally:
                try:
                    dialog.deleteLater()
                except RuntimeError:
                    pass

    # 工具

    @staticmethod
    def _generate_launch_token() -> str:
        """生成 <timestamp_hex>.<hmac_hex> 令牌，供 updater.exe 校验来源。"""
        ts_hex = format(int(time.time()), "x")
        mac = hmac.new(APP_UPDATE_TOKEN_SECRET, ts_hex.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{ts_hex}.{mac}"

    @staticmethod
    def _verify_sha256(path: Path, expected: str) -> bool:
        """分块计算文件 SHA256 并忽略大小写比较，文件读取失败时返回 False。"""
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest().lower() == expected.lower()
        except OSError:
            return False

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        """尽力删除无效下载包，路径不存在或删除失败时不向调用方抛出。"""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _close_progress(self) -> None:
        """停止下载速度定时器并结束进度框，随后解除管理器持有的对话框引用。"""
        self._speed_timer.stop()
        if self._progress is not None:
            dialog, self._progress = self._progress, None
            try:
                dialog.finish()
            except Exception:
                pass
            finally:
                try:
                    dialog.deleteLater()
                except RuntimeError:
                    pass

    def _info_box(self, title: str, text: str) -> None:
        """以主窗口为父级同步显示更新信息通知。"""
        dialog = UpdateNoticeDialog(title, text, self._win)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _error_box(self, title: str, text: str) -> None:
        """以错误样式同步显示更新失败通知，关闭后返回调用流程。"""
        dialog = UpdateNoticeDialog(title, text, self._win, error=True)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
