"""管理插件后台工作的容量、协作取消和宿主线程交付。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from threading import Condition, Event, Thread, get_ident
from weakref import WeakSet

from src.plugin_sdk.errors import PluginError
from src.plugin_sdk.tasks import BackgroundTask, TaskCancelled, TaskContext, TaskProgress, TaskResult
from src.utils.i18n import t as tr
from src.utils.logger import logger


@dataclass
class _Job:
    """运行队列中的任务，仅取消信号和进度槽在工作线程之间共享。"""

    id: int
    owner: str
    task: BackgroundTask
    work: Callable | None
    cancelled: Event = field(default_factory=Event)
    started: bool = False
    progress: TaskProgress | None = None
    progress_posted: bool = False


class BackgroundTaskRunner:
    """使用固定数量守护线程；不合作的任务不会阻止应用退出。"""

    def __init__(
        self,
        post: Callable[[Callable[[], None]], None],
        *,
        error_sink=None,
        max_workers: int = 2,
        max_pending: int = 32,
    ) -> None:
        """注入主线程投递函数，初始化有界队列，首次提交时再启动线程。"""
        if max_workers < 1 or max_pending < 1:
            raise ValueError("任务容量必须为正数")
        self._post, self._error_sink = post, error_sink
        self._max_workers, self._max_pending = max_workers, max_pending
        self._thread_id = get_ident()
        self._condition = Condition()
        self._queue: deque[_Job] = deque()
        self._jobs: dict[int, _Job] = {}
        self._tasks: dict[str, WeakSet] = {}
        self._threads: list[Thread] = []
        self._next_id = 0
        self._shutdown = False

    def _require_thread(self) -> None:
        """要求队列管理和观察者变更都发生在宿主线程。"""
        if get_ident() != self._thread_id:
            raise PluginError(tr("后台任务只能在宿主线程提交或管理"))

    def submit(self, plugin_id: str, worker: Callable, *, title: str = "") -> BackgroundTask:
        """在总容量限制内创建任务，执行中的任务也占用容量。"""
        self._require_thread()
        if not callable(worker):
            raise TypeError("worker 必须可调用")
        with self._condition:
            if self._shutdown:
                raise PluginError(tr("后台任务服务已关闭"))
            if len(self._jobs) >= self._max_pending:
                raise PluginError(tr("后台任务队列已满，请等待已有任务完成"))
            self._next_id += 1
            ident = self._next_id
            task = BackgroundTask(partial(self._cancel, ident), partial(self._callback_error, plugin_id), title=title)
            job = _Job(ident, plugin_id, task, worker)
            self._jobs[ident] = job
            self._tasks.setdefault(plugin_id, WeakSet()).add(task)
            self._queue.append(job)
            if not self._threads:
                for index in range(self._max_workers):
                    thread = Thread(target=self._worker, name=f"plugin-worker-{index}", daemon=True)
                    self._threads.append(thread)
                    thread.start()
            self._condition.notify()
        return task

    def _callback_error(self, owner: str, source: str, error: BaseException) -> None:
        """把宿主线程中的插件观察者异常交给所属插件的故障处理。"""
        if self._error_sink is not None:
            self._error_sink(owner, source, error)
        else:
            logger.error("插件 '%s' 的后台任务回调失败：%s", owner, error)

    def _worker(self) -> None:
        """从条件队列取得任务，始终把结果和错误交回宿主线程。"""
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait()
                if self._shutdown:
                    return
                job = self._queue.popleft()
                job.started = True
                worker = job.work
                job.work = None
            context = TaskContext(job.cancelled, partial(self._report, job.id))
            try:
                context.raise_if_cancelled()
                result = TaskResult(value=worker(context))
                context.raise_if_cancelled()
            except TaskCancelled:
                result = TaskResult(cancelled=True)
            except BaseException as exc:
                result = TaskResult(error=f"{type(exc).__name__}: {exc}")
            finally:
                worker = None
            with self._condition:
                if self._shutdown or job.id not in self._jobs:
                    continue
            self._post_callback(partial(self._finish, job.id, result))
            job, context, result = None, None, None

    def _post_callback(self, callback: Callable) -> None:
        """宿主已销毁时放弃交付，避免守护线程因 Qt 对象失效而异常退出。"""
        try:
            self._post(callback)
        except RuntimeError:
            logger.debug("插件任务交付已关闭")

    def _report(self, ident: int, progress: TaskProgress) -> None:
        """以单槽合并未交付进度，每个任务至多保留一个待投递回调。"""
        with self._condition:
            job = self._jobs.get(ident)
            if job is None or job.cancelled.is_set() or self._shutdown:
                return
            job.progress = progress
            if job.progress_posted:
                return
            job.progress_posted = True
        self._post_callback(partial(self._deliver_progress, ident))

    def _deliver_progress(self, ident: int) -> None:
        """取得最新进度并释放投递槽，取消或关闭后的更新被丢弃。"""
        self._require_thread()
        with self._condition:
            job = self._jobs.get(ident)
            if job is None:
                return
            progress, job.progress = job.progress, None
            job.progress_posted = False
        if progress is not None and not job.cancelled.is_set():
            job.task._report(progress)

    def _finish(self, ident: int, result: TaskResult) -> None:
        """先释放队列容量再交付结果，使完成回调可以提交后续任务。"""
        self._require_thread()
        with self._condition:
            job = self._jobs.pop(ident, None)
        if job is not None:
            job.task._finish(TaskResult(cancelled=True) if job.cancelled.is_set() else result)

    def _cancel(self, ident: int) -> None:
        """取消排队任务或设置执行中任务的协作信号。"""
        self._require_thread()
        with self._condition:
            job = self._jobs.get(ident)
            if job is None:
                return
            job.cancelled.set()
            pending = not job.started
            if pending:
                self._queue.remove(job)
                job.work = None
        if pending:
            self._finish(ident, TaskResult(cancelled=True))

    def cancel_owner(self, plugin_id: str) -> None:
        """先断开该实例的全部任务观察者，再取消排队和运行中的工作。"""
        self._require_thread()
        for task in self._tasks.pop(plugin_id, WeakSet()):
            task._detach()
        with self._condition:
            jobs = [job for job in self._jobs.values() if job.owner == plugin_id]
        for job in jobs:
            self._cancel(job.id)
            job.task._finish(TaskResult(cancelled=True))

    def shutdown(self) -> None:
        """幂等关闭队列并抑制所有回调，不在界面线程等待工作函数结束。"""
        self._require_thread()
        if self._shutdown:
            return
        for owner in list(self._tasks):
            self.cancel_owner(owner)
        with self._condition:
            self._shutdown = True
            self._queue.clear()
            self._jobs.clear()
            self._condition.notify_all()
        self._error_sink = None
