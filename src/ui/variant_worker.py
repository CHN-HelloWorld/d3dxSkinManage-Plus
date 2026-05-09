"""执行独立变体任务，不持有界面控件或活动索引。"""

from PySide6.QtCore import QThread


class VariantWorker(QThread):
    """在后台执行单项变体任务，取消时释放任务持有的独立资源。"""

    def __init__(self, task):
        """保存已准备的变体任务，并初始化后台错误字段。"""
        super().__init__()
        self.task = task
        self.error = None

    def run(self):
        """检查取消后执行任务并捕获异常；收到取消请求时在本线程释放任务。"""
        try:
            if self.isInterruptionRequested():
                raise InterruptedError("variant task cancelled")
            self.task.run()
        except Exception as error:
            self.error = error
        finally:
            if self.isInterruptionRequested():
                self.task.release()

    def stop(self):
        """请求任务中断；线程尚未运行或已经结束时立即释放任务资源。"""
        self.requestInterruption()
        if not self.isRunning() and self.task is not None:
            self.task.release()
