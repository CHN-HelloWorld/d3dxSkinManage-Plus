"""为后台文件工作提供协作式 GIL 时间片。"""

import threading
import time


class LibraryWorkBudget:
    """为每个后台读取线程维护独立时间预算，定期让出解释器执行机会。"""

    def __init__(self):
        """创建线程局部存储，避免多个读取线程共享同一工作截止时间。"""
        self._local = threading.local()

    def checkpoint(self, cancelled):
        """先检查取消状态；耗尽四毫秒预算后短暂休眠释放 GIL，再返回最新取消状态。"""
        if cancelled():
            return True
        now = time.monotonic()
        deadline = getattr(self._local, "deadline", None)
        if deadline is not None and now >= deadline:
            # 界面回调需要重新获取 GIL，每个读取线程独立计算工作配额。
            time.sleep(0.001)
            now = time.monotonic()
            self._local.deadline = now + 0.004
        elif deadline is None:
            self._local.deadline = now + 0.004
        return cancelled()
