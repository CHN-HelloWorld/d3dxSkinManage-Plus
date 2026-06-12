import json
import subprocess
import sys

import pytest
from PySide6.QtGui import QColor, QImage

from src.ui.backgrounds.process_protocol import LatestImageSender, PacketReader, decode_json


def test_shared_pixels_keep_alpha_dpr_and_survive_buffer_reuse():
    from src.ui.backgrounds.shared_image import SharedImageReader, SharedImageWriter

    writer, reader = SharedImageWriter(), SharedImageReader()
    try:
        source = QImage(5, 3, QImage.Format.Format_ARGB32_Premultiplied)
        source.fill(QColor(80, 120, 160, 128))
        source.setDevicePixelRatio(1.5)
        first = reader.read(writer.write(source))
        source.fill(QColor("blue"))
        second = reader.read(writer.write(source))
        assert first.size().width() == 5 and first.size().height() == 3
        assert first.devicePixelRatio() == 1.5
        assert first.pixelColor(4, 2).getRgb() == (80, 120, 159, 128)
        assert second.pixelColor(4, 2) == QColor("blue")
    finally:
        writer.close()
        reader.close()
    assert first.pixelColor(0, 0).getRgb() == (80, 120, 159, 128)


def test_shared_sender_keeps_in_flight_pixels_until_the_matching_ack():
    from src.ui.backgrounds.shared_image import SharedImageReader, SharedImageWriter

    writer, reader = SharedImageWriter(), SharedImageReader()
    wire = []
    sender = LatestImageSender(wire.append, image_writer=writer)
    try:
        for color in ("red", "green", "blue"):
            source = QImage(1024, 768, QImage.Format.Format_RGB32)
            source.fill(QColor(color))
            sender.submit(source)
        assert len(wire) == 1
        assert len(wire[0]) < 2048, "foreground pixels must not go through the GUI socket"
        first = decode_json(PacketReader().feed(wire[0])[0][1])
        assert first["action"] == "foreground"
        assert reader.read(first["image"]).pixelColor(0, 0) == QColor("red")
        sender.acknowledge(first["sequence"] + 10)
        assert len(wire) == 1
        sender.acknowledge(first["sequence"])
        assert len(wire) == 2
        latest = decode_json(PacketReader().feed(wire[1])[0][1])
        assert reader.read(latest["image"]).pixelColor(0, 0) == QColor("blue")
        sender.acknowledge(latest["sequence"])
        assert not sender.busy
    finally:
        reader.close()
        writer.close()


def test_pending_frame_keeps_its_geometry_and_can_be_retired_on_state_change():
    from src.ui.backgrounds.shared_image import SharedImageWriter

    writer = SharedImageWriter()
    wire = []
    sender = LatestImageSender(wire.append, image_writer=writer)
    source = QImage(40, 30, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    config = dict(size=[40, 30], paused=False, suspended=False)
    try:
        sender.submit(source, configuration=config)
        first = decode_json(PacketReader().feed(wire[0])[0][1])
        assert first["configuration"] == config
        sender.submit(source, configuration=config)
        sender.discard_pending()  # 暂停或最小化使尚未发送的状态失效。
        sender.acknowledge(first["sequence"])
        assert len(wire) == 1

        config["paused"] = True
        sender.submit(source, configuration=config)
        second = decode_json(PacketReader().feed(wire[-1])[0][1])
        assert second["configuration"]["paused"]
        larger = QImage(80, 60, QImage.Format.Format_RGB32)
        larger.fill(QColor("blue"))
        config["size"] = [80, 60]
        sender.submit(larger, configuration=config)
        config["size"] = [160, 120]
        sender.acknowledge(second["sequence"])
        latest = decode_json(PacketReader().feed(wire[-1])[0][1])
        assert latest["configuration"]["size"] == [80, 60]
        assert [latest["image"]["width"], latest["image"]["height"]] == [80, 60]

        sender.acknowledge(latest["sequence"])
        sender.submit(larger, configuration=latest["configuration"])
        unchanged = decode_json(PacketReader().feed(wire[-1])[0][1])
        assert "configuration" not in unchanged
    finally:
        writer.close()


@pytest.mark.parametrize(
    "geometry",
    [
        {"width": 0},
        {"height": 16385},
        {"stride": 1},
        {"dpr": float("nan")},
        {"width": 16384, "height": 16384, "stride": 65536},
        {"width": 40, "height": 40, "stride": 160},
    ],
)
def test_shared_reader_rejects_geometry_outside_the_allocation(geometry):
    from src.ui.backgrounds.shared_image import SharedImageReader, SharedImageWriter

    writer, reader = SharedImageWriter(), SharedImageReader()
    source = QImage(4, 4, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    try:
        descriptor = writer.write(source)
        with pytest.raises(ValueError):
            reader.read(descriptor | geometry)
        assert reader.read(descriptor).pixelColor(3, 3) == QColor("red")
    finally:
        reader.close()
        writer.close()


def test_shared_reader_attaches_after_resize_and_from_another_process():
    from src.ui.backgrounds.shared_image import SharedImageReader, SharedImageWriter

    writer, reader = SharedImageWriter(), SharedImageReader()
    try:
        small = QImage(2, 2, QImage.Format.Format_RGB32)
        small.fill(QColor("red"))
        assert reader.read(writer.write(small)).pixelColor(1, 1) == QColor("red")
        large = QImage(100, 80, QImage.Format.Format_RGB32)
        large.fill(QColor("blue"))
        descriptor = writer.write(large)
        assert reader.read(descriptor).pixelColor(99, 79) == QColor("blue")
        code = (
            "import json, sys\n"
            "from src.ui.backgrounds.shared_image import SharedImageReader\n"
            "reader = SharedImageReader()\n"
            "image = reader.read(json.loads(sys.argv[1]))\n"
            "reader.close()\n"
            "print(json.dumps([image.width(), image.height(), image.pixelColor(99, 79).getRgb()]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, json.dumps(descriptor)], capture_output=True, text=True, timeout=15, check=True
        )
        assert json.loads(result.stdout) == [100, 80, [0, 0, 255, 255]]
    finally:
        reader.close()
        writer.close()
