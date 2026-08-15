"""持续转换最新视频帧，独立于界面调度和图片工作池。"""

from __future__ import annotations

from threading import Event, Lock

from PySide6.QtCore import Qt, QThread, QTimer, Signal

from src.ui.backgrounds.video import convert_owned_frame
from src.ui.backgrounds.video_scaler import scaling_session


class VideoFramePump(QThread):
    """按目标帧率在专用线程转换最新视频帧，并合并尚未消费的可用通知。"""

    available = Signal()

    def __init__(self, take_frame, size, fit, fps):
        """保存取帧回调与缩放规格，建立控制状态锁、停止标志和单结果槽。"""
        super().__init__()
        self._take_frame = take_frame
        self._lock = Lock()
        self._stop = Event()
        self._revision = 0
        self._spec = (size, fit, max(1, fps))
        self._latest = None
        self._notified = False
        self._paused = False

    def configure(self, size, fit, fps):
        """更新显示规格并递增版本，使旧规格的待取结果及进行中转换失效。"""
        spec = (size, fit, max(1, fps))
        with self._lock:
            if spec != self._spec:
                self._spec = spec
                self._revision += 1
                self._latest = None

    def pause(self, paused):
        """设置暂停状态并递增版本，丢弃已有结果及随后返回的旧转换。"""
        with self._lock:
            self._paused = paused
            self._revision += 1
            self._latest = None

    def take_result(self):
        """原子取走最新转换结果并解除通知合并标记。"""
        with self._lock:
            result, self._latest = self._latest, None
            self._notified = False
            return result

    def stop(self):
        """请求线程事件循环退出并清空尚未消费的结果，不等待线程结束。"""
        self._stop.set()
        self.quit()
        with self._lock:
            self._latest = None

    def run(self):
        # 使用精确 Qt 定时器保持转换节奏，避免 Windows 事件等待精度限制帧率。
        """在当前线程用精确定时器驱动转换，并在退出时停止计时和释放缩放上下文。"""
        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)

        def convert():
            """按当前规格转换一张最新帧，丢弃过期结果并最多保留一个未消费通知。"""
            if self._stop.is_set():
                self.quit()
                return
            with self._lock:
                size, fit, fps = self._spec
                revision, paused = self._revision, self._paused
            interval = max(1, round(1000 / fps))
            if timer.interval() != interval:
                timer.setInterval(interval)
            if paused:
                return
            frame = self._take_frame()
            if frame is None:
                return
            image, error = convert_owned_frame(frame, size, fit)
            frame = None
            with self._lock:
                if self._stop.is_set() or self._paused or revision != self._revision:
                    return
                self._latest = (revision, image, error)
                notify = not self._notified
                self._notified = True
            if notify:
                self.available.emit()

        timer.timeout.connect(convert, Qt.ConnectionType.DirectConnection)
        with self._lock:
            timer.start(max(1, round(1000 / self._spec[2])))
        try:
            with scaling_session():
                if not self._stop.is_set():
                    self.exec()
        finally:
            timer.stop()
