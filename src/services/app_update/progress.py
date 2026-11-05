"""使用单调时钟采样已接收字节数，下载重试时重置采样。"""

from __future__ import annotations

import time
from collections.abc import Callable


class DownloadRate:
    """根据累计接收字节与单调时钟估计下载速度，重试或字节回退时重新建立采样基线。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """注入可替换的单调时钟并初始化空采样状态。"""
        self._clock = clock
        self.reset()

    def reset(self, downloaded: int = 0) -> None:
        """以已有分片建立字节基线，清除速度，避免把续传前缀计为新增下载量。"""
        self._time = self._clock()
        self._bytes = max(0, downloaded)
        self._sample_bytes = self._bytes
        self._speed: float | None = None

    def record(self, downloaded: int) -> None:
        """记录非负累计字节；累计值倒退表示新传输，先重置采样。"""
        if downloaded < self._bytes:
            self.reset()
        self._bytes = max(0, downloaded)

    def sample(self) -> float | None:
        """至少间隔 0.4 秒计算并平滑字节速度，未到采样时间时返回上次速度或 None。"""
        now = self._clock()
        elapsed = now - self._time
        if elapsed < 0.4:
            return self._speed
        current = max(0, self._bytes - self._sample_bytes) / elapsed
        self._speed = current if not self._speed or not current else self._speed * 0.6 + current * 0.4
        self._time = now
        self._sample_bytes = self._bytes
        return self._speed
