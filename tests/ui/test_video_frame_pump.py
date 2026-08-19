from threading import Event, get_ident
from time import sleep

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage


def test_conversion_continues_without_gui_turns_and_notices_are_coalesced(qapp, monkeypatch):
    from src.ui.backgrounds import video_pump

    main_thread = get_ident()
    converted, notifications = [], []
    entered = Event()

    def convert(frame, size, fit):
        converted.append((get_ident(), size, fit))
        image = QImage(*size, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        entered.set()
        return image, ""

    monkeypatch.setattr(video_pump, "convert_owned_frame", convert)
    pump = video_pump.VideoFramePump(lambda: object(), (20, 10), "cover", 60)
    pump.available.connect(lambda: notifications.append(True), Qt.ConnectionType.DirectConnection)
    pump.start()
    try:
        assert entered.wait(2)
        sleep(0.12)  # 不主动运行 Qt 事件循环。
        assert len(converted) >= 4
        assert all(row[0] != main_thread for row in converted)
        assert notifications == [True]
        result = pump.take_result()
        assert result is not None and result[1].size().width() == 20
        assert pump.take_result() is None
        pump.configure((40, 30), "contain", 30)
        sleep(0.08)
        assert converted[-1][1:] == ((40, 30), "contain")
        assert len(notifications) == 2
    finally:
        pump.stop()
        assert pump.wait(2000)


def test_stop_discards_conversion_that_finishes_late(qapp, monkeypatch):
    from src.ui.backgrounds import video_pump

    entered, release = Event(), Event()

    def convert(*args):
        entered.set()
        release.wait(2)
        return QImage(2, 2, QImage.Format.Format_RGB32), ""

    monkeypatch.setattr(video_pump, "convert_owned_frame", convert)
    pump = video_pump.VideoFramePump(lambda: object(), (2, 2), "cover", 60)
    notifications = []
    pump.available.connect(lambda: notifications.append(True), Qt.ConnectionType.DirectConnection)
    pump.start()
    try:
        assert entered.wait(2)
        pump.stop()
        release.set()
        assert pump.wait(2000)
        assert pump.take_result() is None
        assert notifications == []
    finally:
        release.set()
        pump.stop()
        pump.wait(2000)
