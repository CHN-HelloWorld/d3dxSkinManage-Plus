import struct

import pytest
from PySide6.QtGui import QColor, QImage


def test_packets_survive_partial_reads_and_multiple_messages():
    from src.ui.backgrounds.process_protocol import PacketReader, decode_json, encode_json

    packet = encode_json({"action": "pause"}) + encode_json({"action": "resume"})
    reader = PacketReader()
    assert reader.feed(packet[:3]) == []
    assert reader.feed(packet[3:7]) == []
    messages = reader.feed(packet[7:])
    assert [decode_json(payload) for kind, payload in messages if kind == b"J"] == [
        {"action": "pause"},
        {"action": "resume"},
    ]


def test_invalid_packet_length_is_rejected_before_allocating_payload():
    from src.ui.backgrounds.process_protocol import PacketReader

    for length in (0, 0xFFFFFFFF):
        with pytest.raises(ValueError):
            PacketReader().feed(struct.pack("!I", length))


def test_image_round_trip_keeps_alpha_dpr_and_owned_pixels():
    from src.ui.backgrounds.process_protocol import PacketReader, decode_image, encode_image

    source = QImage(3, 2, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor(80, 120, 160, 128))
    source.setDevicePixelRatio(1.5)
    kind, payload = PacketReader().feed(encode_image(source, b"F", 9))[0]
    assert kind == b"F"
    sequence, image = decode_image(payload)
    source.fill(QColor("black"))
    assert sequence == 9
    assert image.devicePixelRatio() == 1.5
    assert image.size().width() == 3
    assert image.pixelColor(2, 1).getRgb() == (80, 120, 159, 128)
    with pytest.raises(ValueError):
        decode_image(payload[:-1])


def test_foreground_backpressure_sends_latest_image_after_matching_ack():
    from src.ui.backgrounds.process_protocol import LatestImageSender, PacketReader, decode_image

    wire = []
    sender = LatestImageSender(wire.append)
    for color in ("red", "green", "blue"):
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        sender.submit(image)
    assert len(wire) == 1
    sequence, first = decode_image(PacketReader().feed(wire[0])[0][1])
    assert first.pixelColor(0, 0) == QColor("red")
    sender.acknowledge(sequence + 100)
    assert len(wire) == 1
    sender.acknowledge(sequence)
    assert len(wire) == 2
    _, last = decode_image(PacketReader().feed(wire[1])[0][1])
    assert last.pixelColor(0, 0) == QColor("blue")
