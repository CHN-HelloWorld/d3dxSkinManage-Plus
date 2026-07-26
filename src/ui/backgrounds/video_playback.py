"""在专用 Qt 线程管理媒体对象和硬件设备初始化。"""

from __future__ import annotations

import threading

import shiboken6
from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot
from PySide6.QtMultimedia import QVideoFrame

from src.ui.backgrounds.video_frame import retire_video_frame


class VideoPlayback(QThread):
    """在专用 Qt 线程播放媒体，通过锁保护控制状态和唯一待处理帧。"""

    error = Signal(str)
    _command = Signal()

    def __init__(self, source: QUrl, volume: float):
        """保存媒体来源和初始音量，并创建停止标志及跨线程状态锁。"""
        super().__init__()
        self.source = source
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._volume, self._paused, self._latest = volume, False, None

    def controls(self):
        """在锁内取得音量与暂停状态的一致快照。"""
        with self._lock:
            return self._volume, self._paused

    def setVolume(self, volume):
        """更新期望音量，并通过信号通知媒体所属线程同步。"""
        with self._lock:
            self._volume = volume
        self._command.emit()

    def play(self):
        """清除暂停状态并请求媒体线程恢复播放。"""
        with self._lock:
            self._paused = False
        self._command.emit()

    def pause(self):
        """设置暂停状态、异步释放待处理帧并通知媒体线程。"""
        with self._lock:
            self._paused = True
        self._discard_frame()
        self._command.emit()

    def stop(self):
        """标记停止并清空待处理帧，通过信号请求媒体线程退出。"""
        self._stopping.set()
        self._discard_frame()
        self._command.emit()

    def offer_frame(self, frame):
        """保留最新输入帧并异步释放被替换帧；暂停或停止时直接交由释放线程处理。"""
        with self._lock:
            if self._stopping.is_set() or self._paused:
                previous = frame
            else:
                previous, self._latest = self._latest, frame
        if previous is not None:
            retire_video_frame(previous)

    def take_frame(self):
        """在锁内取走待处理帧并转移其引用，无可用帧时返回 None。"""
        with self._lock:
            frame, self._latest = self._latest, None
        return frame

    def _discard_frame(self):
        """取走当前待处理帧并提交到专用线程释放。"""
        frame = self.take_frame()
        if frame is not None:
            retire_video_frame(frame)

    def run(self):
        """在当前线程创建、运行和销毁媒体对象，退出时清理帧并报告非停止期间的错误。"""
        session = _VideoSession(self)
        self._command.connect(session.synchronize)
        try:
            session.start()
            if not self._stopping.is_set():
                self.exec()
        except Exception as error:
            if not self._stopping.is_set():
                self.error.emit(str(error))
        finally:
            self._stopping.set()
            self._discard_frame()
            session.close()
            # 媒体对象的创建和销毁都留在当前线程。
            shiboken6.delete(session)


class _VideoSession(QObject):
    """承载仅在播放线程访问的播放器、音频输出和视频接收器。"""

    def __init__(self, owner):
        """绑定线程控制器并初始化尚未创建的媒体对象引用。"""
        super().__init__()
        self.owner = owner
        self.player = self.audio = self.sink = None
        self._playing = None

    def start(self):
        """在未停止时创建循环播放器，连接错误和帧信号并应用当前控制状态。"""
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        if self.owner._stopping.is_set():
            return
        self.player = QMediaPlayer(self)
        if self.owner._stopping.is_set():
            return
        self.audio = QAudioOutput(self)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.player.errorOccurred.connect(self._error)
        self.sink.videoFrameChanged.connect(self._frame)
        self.player.setSource(self.owner.source)
        self.synchronize()

    def _error(self, code, message):
        """将媒体错误转发给线程控制器，忽略停止过程中的错误。"""
        if not self.owner._stopping.is_set():
            self.owner.error.emit(message)

    @Slot(QVideoFrame)
    def _frame(self, frame):
        """为有效视频帧创建独立包装引用并交给控制器保存最新帧。"""
        if frame.isValid() and not self.owner._stopping.is_set():
            self.owner.offer_frame(QVideoFrame(frame))

    @Slot()
    def synchronize(self):
        """在媒体线程应用音量和播放状态；收到停止标志时退出线程事件循环。"""
        if self.owner._stopping.is_set():
            self.owner.quit()
            return
        if self.player is None or self.audio is None:
            return
        volume, paused = self.owner.controls()
        self.audio.setVolume(volume)
        if self._playing != (not paused):
            self._playing = not paused
            if paused:
                self.player.pause()
            else:
                self.player.play()

    def close(self):
        """停止已创建的播放器并清空媒体来源，释放其解码资源。"""
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
