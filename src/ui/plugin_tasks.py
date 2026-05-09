"""通过 Qt 队列把插件后台结果交付到宿主线程。"""

from PySide6.QtCore import QObject, Qt, Signal, Slot


class PluginTaskDispatcher(QObject):
    """由主窗口拥有的投递器，关闭后忽略已经排队的旧回调。"""

    _posted = Signal(object)

    def __init__(self, parent=None) -> None:
        """在创建线程建立队列连接，不创建 QApplication 或额外事件循环。"""
        super().__init__(parent)
        self._closed = False
        self._posted.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def post(self, callback) -> None:
        """允许工作线程请求交付，Qt 负责将回调放入所属线程队列。"""
        if not self._closed:
            self._posted.emit(callback)

    @Slot(object)
    def _deliver(self, callback) -> None:
        """仅在宿主仍存活时执行排队回调。"""
        if not self._closed:
            callback()

    def close(self) -> None:
        """停止交付已经排队或后续到达的回调。"""
        self._closed = True
