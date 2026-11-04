"""验证 Mod 更新终态只保留展示历史，不保留工作进程及准备资源。"""

from __future__ import annotations

import gc
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject

from src.core.mod_update_process import UpdateProcess
from src.core.mod_update_tasks import ModUpdateTasks, _UpdateTask


class _Manager:
    """提供更新任务测试需要的当前游戏和 Mod 查询。"""

    current_game_id = "game-a"

    @staticmethod
    def get_mod(_mid):
        """返回可用于重试创建下载身份的最小 Mod。"""
        return SimpleNamespace(display_name="示例 Mod")


class _Config:
    """为不进入真实调度的测试提供固定并发配置。"""

    @staticmethod
    def get(_key, default=None):
        """返回正并发数，其他默认值原样返回。"""
        return 1 if default is None else default


class _RunningProcess(QObject):
    """模拟尚未退出的工作进程，记录取消请求但不立即结束。"""

    def __init__(self, parent=None) -> None:
        """初始化运行态及取消记录。"""
        super().__init__(parent)
        self.running = True
        self.cancelled = False

    def cancel(self) -> None:
        """记录协作取消，真实退出由测试显式推进。"""
        self.cancelled = True


class _ShutdownProcess(_RunningProcess):
    """在 shutdown 调用栈内同步发布终态，模拟真实进程刚好退出。"""

    def __init__(self, finished, parent=None) -> None:
        """保存终态回调，初始保持运行状态。"""
        super().__init__(parent)
        self._finished = finished

    def shutdown(self) -> None:
        """同步退出并触发所有者清空 job.process。"""
        self.running = False
        self._finished()


def _file(file_id=1):
    """创建快照和重试路径使用的最小远端文件描述。"""
    return SimpleNamespace(file_name="update.zip", size_bytes=10, file_id=file_id, md5_checksum="")


def _job(tasks, tmp_path: Path, *, mid="mod-a"):
    """创建已退出但尚未由任务表收尾的真实 UpdateProcess。"""
    job = _UpdateTask(
        "game-a",
        mid,
        _file(),
        SimpleNamespace(mod_id=7, name="示例 Mod"),
        SimpleNamespace(mod_id=7),
        info=SimpleNamespace(local_mod_id=mid),
    )
    process = UpdateProcess({"work_root": str(tmp_path), "payload": "x" * 1024}, parent=tasks)
    process._started = True
    process._terminal = True
    job.process = process
    job.plan = {"work_root": str(tmp_path), "payload": "p" * 1024}
    job.prepared = {"body": str(tmp_path / "body.7z"), "payload": "r" * 1024}
    job.download = SimpleNamespace(payload="d" * 1024)
    tasks._records[job.key] = job
    return job, process


def _flush_deferred_deletes() -> None:
    """投递 Qt 延迟删除并运行 Python 回收，观察真实原生对象生命周期。"""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()


@pytest.mark.parametrize("phase", ["done", "failed", "canceled"])
def test_terminal_update_retires_process_and_heavy_resources(qapp, tmp_path, phase):
    """成功、失败和取消终态均只保留界面历史字段。"""
    tasks = ModUpdateTasks(_Manager(), _Config())
    cleanup = []
    tasks._cleanup = cleanup.append
    job, process = _job(tasks, tmp_path)
    process_ref = weakref.ref(process)
    plan = job.plan

    tasks._finish(job, phase, "failure" if phase == "failed" else "")

    assert cleanup == [plan]
    assert job.process is None
    assert job.plan is None
    assert job.prepared is None
    assert job.download is None
    assert job.snapshot()["phase"] == phase
    assert job.file.file_name == "update.zip"
    _flush_deferred_deletes()
    assert not shiboken6.isValid(process)
    assert tasks.findChildren(UpdateProcess) == []
    del process
    _flush_deferred_deletes()
    assert process_ref() is None


def test_running_update_is_cancelled_before_terminal_resources_are_retired(qapp, tmp_path):
    """取消运行中进程只发请求，直到退出后的终态收尾才销毁对象。"""
    tasks = ModUpdateTasks(_Manager(), _Config())
    job = _UpdateTask("game-a", "mod-a", _file(), SimpleNamespace(), SimpleNamespace())
    process = _RunningProcess(tasks)
    job.process = process
    job.plan = None
    tasks._records[job.key] = job

    tasks._cancel_job(job)

    assert process.cancelled
    assert process.running
    assert job.process is process
    assert job.active
    assert not tasks._finish(job, "canceled")
    assert job.active
    assert job.process is process
    process.running = False
    assert tasks._finish(job, "canceled")
    assert job.process is None
    _flush_deferred_deletes()
    assert not shiboken6.isValid(process)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "canceled"])
def test_real_update_process_emits_terminal_signals_after_running_becomes_false(qapp, tmp_path, outcome):
    """真实 UpdateProcess 必须先关闭运行态，再通知任务表进入终态。"""
    process = UpdateProcess({"work_root": str(tmp_path)})
    process._started = True
    seen = []
    signal = getattr(process, outcome)
    signal.connect(lambda *_args: seen.append(process.running))

    process._finish(outcome, {} if outcome == "succeeded" else "failure")

    assert seen == [False]
    process.deleteLater()
    _flush_deferred_deletes()


def test_retry_and_history_clear_leave_no_old_update_process(qapp, tmp_path):
    """失败后重试及成功历史确认都不得累积旧 UpdateProcess 子对象。"""
    tasks = ModUpdateTasks(_Manager(), _Config())
    tasks._cleanup = lambda _plan: None
    tasks._pump = lambda: None
    failed, old_process = _job(tasks, tmp_path)
    tasks._finish(failed, "failed", "failed")
    _flush_deferred_deletes()
    assert not shiboken6.isValid(old_process)

    tasks.start(
        failed.mid,
        _file(2),
        SimpleNamespace(mod_id=7),
        info=failed.info,
        archive=tmp_path / "retry.zip",
    )
    retry = tasks._records[failed.key]
    assert retry is not failed
    assert tasks.findChildren(UpdateProcess) == []

    retry_process = UpdateProcess({"work_root": str(tmp_path)}, parent=tasks)
    retry_process._started = True
    retry_process._terminal = True
    retry.process = retry_process
    tasks._finish(retry, "done")
    assert tasks.clear_completed()
    _flush_deferred_deletes()
    assert tasks._records == {}
    assert tasks.findChildren(UpdateProcess) == []


def test_shutdown_handles_terminal_signal_clearing_process_reentrantly(qapp):
    """关闭等待期间同步到达的终态信号不得再次解引用已清空字段。"""
    tasks = ModUpdateTasks(_Manager(), _Config())
    job = _UpdateTask("game-a", "mod-a", _file(), SimpleNamespace(), SimpleNamespace())
    process = _ShutdownProcess(lambda: tasks._finish(job, "canceled"), tasks)
    job.process = process
    tasks._records[job.key] = job

    assert tasks.shutdown()
    assert job.process is None
    assert job.phase == "canceled"
    _flush_deferred_deletes()
    assert not shiboken6.isValid(process)
