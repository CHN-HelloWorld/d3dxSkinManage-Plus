"""合并设置页后台统计请求，并绑定页面生命周期。"""

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot

from src.utils.logger import logger


class _Signals(QObject):
    """向界面线程传递下载占用扫描结果及异常。"""

    finished = Signal(object, object)


class _Scan(QRunnable):
    """在线程池中扫描下载目录和数据库引用，不直接访问页面。"""

    def __init__(self, paths):
        """保存数据库与下载目录路径，并创建跨线程结果信号容器。"""
        super().__init__()
        self.paths = paths
        self.signals = _Signals()

    def run(self):
        """计算下载占用量，将统计结果或异常作为互斥参数发出。"""
        from src.services.gamebanana.download_store import scan_library_download_usage

        try:
            result = scan_library_download_usage(*self.paths)
        except Exception as error:
            self.signals.finished.emit(None, error)
        else:
            self.signals.finished.emit(result, None)


class DownloadUsageLoader(QObject):
    """串行合并页面的下载占用查询，只发布最新请求的统计结果。"""

    ready = Signal(object, object, object, object)

    def __init__(self, parent):
        """将加载器绑定到页面，并初始化运行任务与待处理路径。"""
        super().__init__(parent)
        self._active = None
        self._pending = None

    def request(self, db_path, download_dir):
        """启动线程池扫描；已有任务运行时仅保留最新路径组合。"""
        paths = (db_path, download_dir)
        if self._active is not None:
            self._pending = paths
            return
        task = _Scan(paths)
        self._active = task
        task.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        # 线程池独立持有运行任务，页面销毁仅断开接收者，不销毁活动线程。
        QThreadPool.globalInstance().start(task)

    @Slot(object, object)
    def _finished(self, usage, error):
        """结束当前扫描后优先执行待处理请求，否则发布占用统计或记录错误。"""
        self._active = None
        if self._pending is not None:
            paths, self._pending = self._pending, None
            self.request(*paths)
            return
        if error is not None:
            logger.warning("下载占用统计失败: %s", error)
        elif usage is not None:
            self.ready.emit(usage.total_bytes, usage.total_count, usage.purgeable_bytes, usage.purgeable_count)
