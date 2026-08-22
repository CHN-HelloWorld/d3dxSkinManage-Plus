from types import SimpleNamespace

from PySide6.QtCore import QObject
from PySide6.QtGui import QColor, QImage

from src.ui.backgrounds.process_protocol import LatestImageSender, PacketReader, decode_json, encode_json
from src.ui.backgrounds.shared_image import SharedImageWriter
from src.ui.backgrounds.video_presenter import NativeVideoPresenter


def presenter_for(sender):
    # 保留真实 Qt 信号和槽，仅隔离辅助进程。
    presenter = NativeVideoPresenter.__new__(NativeVideoPresenter)
    QObject.__init__(presenter)
    presenter._sender = sender
    presenter._stopped = False
    presenter._pending_error = None
    presenter._socket = object()
    presenter._configuration = lambda: dict(suspended=False)
    return presenter


def test_frame_failure_is_reported_after_the_paint_callback_returns(qtbot):
    def fail(*args, **kwargs):
        raise OSError("cannot allocate foreground")

    presenter = presenter_for(SimpleNamespace(submit=fail))
    errors = []
    presenter.state_changed.connect(lambda *args: errors.append(args))
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    presenter._frame_ready(image)
    assert not errors, "stopping the presenter here deletes its active QGraphicsEffect"
    qtbot.waitUntil(lambda: bool(errors))
    assert errors[0][0] == "error"
    assert "cannot allocate foreground" in errors[0][1]


def test_stopping_before_deferred_failure_does_not_publish_a_stale_error(qtbot):
    presenter = presenter_for(None)
    errors = []
    presenter.state_changed.connect(lambda *args: errors.append(args))
    presenter._report_error("cannot allocate foreground")
    presenter._stopped = True
    qtbot.wait(10)
    assert not errors


def test_acknowledging_a_pending_resize_reports_shared_allocation_failure(qtbot, monkeypatch):
    writer = SharedImageWriter()
    wire = []
    sender = LatestImageSender(wire.append, image_writer=writer)
    presenter = presenter_for(sender)
    presenter._reader = PacketReader()
    errors = []
    presenter.state_changed.connect(lambda *args: errors.append(args))
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    try:
        sender.submit(image)
        first = decode_json(PacketReader().feed(wire[0])[0][1])
        sender.submit(image.scaled(8, 8))

        def fail(image):
            raise OSError("cannot resize foreground allocation")

        monkeypatch.setattr(writer, "write", fail)
        presenter._socket = SimpleNamespace(readAll=lambda: encode_json(dict(ack=first["sequence"])))
        presenter._read()
        qtbot.waitUntil(lambda: bool(errors))
        assert errors[0][0] == "error"
        assert "cannot resize foreground allocation" in errors[0][1]
    finally:
        writer.close()
