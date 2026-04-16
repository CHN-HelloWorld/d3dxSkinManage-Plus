"""在后台执行归档部署，通过信号报告进度及结果。

任务只使用预先准备的路径和配置执行文件操作，不访问界面或线程绑定的索引。
主线程负责结果校验、数据库提交、预设同步及事件发布。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from src.utils.logger import logger


class DeployWorker(QThread):
    """后台解压部署单个 Mod。

    progress(mod_id, frac)：解压进度 0~1（zip/rar 逐成员较准、7z 只起止两点）。
    finished_deploy(mod_id, ok, deploy_path)：结束；ok=False 表示部署失败。
    """

    progress = Signal(str, float)
    finished_deploy = Signal(str, bool, str)
    _PROGRESS_INTERVAL = 0.05

    def __init__(
        self,
        mod_id: str,
        src_path: str,
        storage,
        parent=None,
        deploy_name: str = "",
        restore_existing: bool = False,
        remembered_path: str = "",
        stage_only: bool = False,
        content_hash: str = "",
    ):
        """捕获部署来源、目标命名和恢复策略，初始化供界面线程读取的结果与身份凭据。"""
        super().__init__(parent)
        self._mod_id = mod_id
        self._src_path = src_path
        self._storage = storage
        self._deploy_name = deploy_name
        self._restore_existing = restore_existing
        self._remembered_path = remembered_path
        self._stage_only = stage_only
        self._content_hash = content_hash
        self.created_deployment = False
        # 主窗口等 QThread.finished 后再提交/释放；退出时也可在 wait 后补提交结果。
        self.result: tuple[bool, str] = (False, "")
        self.error = None
        self.source_receipt = None

    def run(self) -> None:
        """在工作线程确认来源身份并创建或恢复部署，最终通过信号报告结果。

        文件变更前检查取消请求；部署后再次核验来源，失败时清理本次创建内容或停放已有部署。
        异常保存在 error，供界面线程完成发布与提示。
        """
        mod_id = self._mod_id
        try:
            if mod_id:
                from src.core.operation_preflight import confirm_metadata, confirm_source

                self.source_receipt = confirm_source(self._src_path, mod_id, self._content_hash)
                confirm_metadata(self.source_receipt)
            last_progress = None
            last_reported_at = 0.0

            def _cb(frac: float, _mid=mod_id):
                """将进度限制在零到一之间，仅发送递增值，并对中间进度限频以减少界面事件积压。"""
                nonlocal last_progress, last_reported_at
                value = max(0.0, min(1.0, float(frac)))
                now = time.monotonic()
                if last_progress is not None:
                    if value <= last_progress:
                        return
                    if value < 1.0 and now - last_reported_at < self._PROGRESS_INTERVAL:
                        return
                # 发出跨线程信号前限制频率，避免大量小文件进度占用界面事件队列。
                # 第一条和最终进度立即上报，中间最多 20 次/秒。
                last_progress = value
                last_reported_at = now
                self.progress.emit(_mid, value)

            if self._mod_id and self._storage.get_deploy_dir() is not None:
                location = None
                if self._restore_existing and self._remembered_path:
                    _cb(0.0)
                    from pathlib import Path

                    from src.core.mod_identity import DeploymentLocation
                    from src.core.operation_preflight import confirm_deployment

                    confirm_deployment(mod_id, self._remembered_path)
                    location = DeploymentLocation(mod_id, Path(self._remembered_path), self.source_receipt.content_hash)
                if self.isInterruptionRequested():
                    raise InterruptedError("deployment cancelled before filesystem changes")
                if location is not None:
                    self._storage._check_deployment_identity(location.path, self._mod_id)
                    if location.active or self._stage_only:
                        deploy_path = location.path
                    else:
                        name = self._storage.active_name_from_parked(location.path, self._mod_id)
                        target = self._storage.active_deployment_path(self._mod_id, name)
                        deploy_path = self._storage.restore_parked_deployment(location.path, target.name)
                else:
                    deploy_path = self._storage.materialize_deployment(
                        self._src_path,
                        self._mod_id,
                        self._deploy_name,
                        progress_cb=_cb,
                        should_cancel=self.isInterruptionRequested,
                        **({"active": False} if self._stage_only else {}),
                    )
                    self.created_deployment = deploy_path is not None
            else:
                deploy_path = self._storage.deploy_mod(self._src_path, progress_cb=_cb, deploy_name=self._deploy_name)
            ok = deploy_path is not None
            if ok:
                if self.source_receipt is not None:
                    try:
                        self.source_receipt.recheck()
                    except OSError:
                        if self.created_deployment:
                            self._storage._check_deployment_identity(deploy_path, mod_id)
                            self._storage.remove_deploy_path(deploy_path)
                        elif not self._stage_only:
                            self._storage.park_deployment(deploy_path, mod_id)
                        raise
                _cb(1.0)
            self.result = (ok, str(deploy_path) if deploy_path else "")
        except Exception as e:
            self.error = e
            logger.error("后台部署失败 (%s): %s", mod_id, e)
            self.result = (False, "")
        self.finished_deploy.emit(mod_id, *self.result)
