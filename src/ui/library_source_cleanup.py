"""并行暂存预设，同时由所有者清理资源文件。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal

from src.core.library_publication import LocalPublication
from src.core.preset_delete import (
    discard_prepared_preset_delete,
    write_prepared_preset_delete,
)
from src.utils.logger import logger


def _discard(candidate):
    """清理预设删除候选的临时文件，文件系统错误仅记录警告。"""
    try:
        discard_prepared_preset_delete(candidate)
    except OSError as error:
        logger.warning("预设删除临时文件清理失败 path=%s reason=%s", candidate.temporary, error)


@dataclass
class _Cleanup:
    """保存来源删除事务的候选文件、恢复令牌、索引快照与后台任务。"""

    mod: object
    game_id: str
    service: object
    recovery_token: object
    candidate: object
    protection: object
    row: object = None
    deployment: object = None
    artifacts_ok: bool = False
    future: object = None


class LibrarySourceCleanupController(QObject):
    """协调来源缺失后的预设持久化与索引删除，提交前重新核验游戏和恢复状态。"""

    completed = Signal(object)

    def __init__(self, manager, publish, resume, parent=None):
        """绑定管理器、发布和恢复回调，并建立返回界面线程的完成信号。"""
        super().__init__(parent)
        self.manager, self.publish, self.resume = manager, publish, resume
        self.active = None
        self.pool = None
        self.completed.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    @property
    def running(self):
        """返回是否仍有尚未完成的来源删除事务。"""
        return self.active is not None

    def purge(self, mod, game_id, *, locations=None):
        """准备预设级联删除并保护相关路径，在后台写候选文件，同时清理独立部署资源。

        已有任务或启动异步事务时返回 None，准备失败返回 False；无需预设写入时直接返回同步清理结果。"""
        if self.running:
            return None
        manager = self.manager
        try:
            candidate = manager._preset_manager.prepare_cascade_delete(mod.mod_id, game_id)
        except Exception as error:
            logger.warning("预设删除候选准备失败 mod=%s reason=%s", mod.mod_id, error)
            return False
        if candidate is None:
            return manager._purge_missing_source_mod(mod, game_id, locations=locations)
        service = manager._local_source_events
        job = _Cleanup(mod, game_id, service, service.recovery_token(), candidate, object())
        self.active = job
        try:
            paths = [mod.path, str(manager._index.paths.container_path(mod.mod_id))]
            paths.extend(str(item.path) for item in (locations or {}).values())
            manager.protect_library_operation(job.protection, paths=paths, mod_ids=(mod.mod_id,))
            if self.pool is None:
                self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-preset-save")
            job.future = self.pool.submit(write_prepared_preset_delete, candidate)
            # 数据库、INI 和当前游戏状态由所有者处理，仅独立序列化和临时文件刷新并发执行。
            job.artifacts_ok = manager._cleanup_missing_source_artifacts(mod, game_id, locations=locations)
            current = manager.get_mod(mod.mod_id)
            job.row = current.to_dict() if current is not None else None
            job.deployment = manager._index.get_deployment(mod.mod_id)
        except Exception as error:
            logger.warning("源删除准备失败，保留记录重试 mod=%s reason=%s", mod.mod_id, error)
            job.artifacts_ok = False
        if job.future is None:
            self._finish(job)
        else:

            def finished(_future):
                """将候选写入完成通知发送到界面线程；接收者已销毁时自行清理临时文件。"""
                try:
                    self.completed.emit(job)
                except RuntimeError:
                    _discard(candidate)

            job.future.add_done_callback(finished)
        return None

    def _finish(self, job):
        """重新核验游戏、恢复令牌、索引和缺失证据后提交删除，失败时保留记录并安排重试。"""
        if self.active is not job:
            return
        self.active = None
        manager, mid = self.manager, job.mod.mod_id
        manager.release_library_operation(job.protection)
        removed = False
        try:
            if job.future is not None:
                job.future.result()
            service = manager._local_source_events
            current = manager.get_mod(mid)
            valid = (
                job.artifacts_ok
                and job.game_id == manager.current_game_id
                and service is job.service
                and service.recovery_token() == job.recovery_token
                and current is not None
                and current.to_dict() == job.row
                and manager._index.get_deployment(mid) == job.deployment
            )
            if valid and service.confirm_missing(mid, generation=service.generation, scope_complete=True):
                removed = manager._finalize_missing_source_mod(current, job.game_id, preset_candidate=job.candidate)
        except Exception as error:
            logger.warning("源删除持久提交失败，保留记录重试 mod=%s reason=%s", mid, error)
        finally:
            _discard(job.candidate)
        if job.game_id != manager.current_game_id or manager._local_source_events is not job.service:
            return
        service = job.service
        if removed:
            service._missing.pop(mid, None)
            service._missing_evidence.pop(mid, None)
            getattr(service, "_cleanup_retry_after", {}).pop(mid, None)
            self.publish(LocalPublication(removed_ids={mid}))
        else:
            if not hasattr(service, "_cleanup_retry_after"):
                service._cleanup_retry_after = {}
            service._cleanup_retry_after[mid] = service.clock() + 0.5
            self.publish(LocalPublication(pending_missing_ids={mid}, retry_after_ms=500))
        self.resume()

    def stop(self):
        """解除当前事务的路径保护并清理候选，取消等待任务且异步关闭线程池。"""
        job, self.active = self.active, None
        if job is not None:
            self.manager.release_library_operation(job.protection)
            _discard(job.candidate)
            if job.future is not None:
                job.future.cancel()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None
