"""有界本地通信数据包，保留预乘透明度和设备像素比。"""

from __future__ import annotations

import copy
import json
import math
import struct

from PySide6.QtGui import QImage

_LENGTH = struct.Struct("!I")
_IMAGE = struct.Struct("!QIIId")  # 字段为序号、宽度、高度、行跨度和设备像素比。
_MAX_PACKET = 256 * 1024 * 1024


def _packet(kind: bytes, payload: bytes) -> bytes:
    """为单字节类型及负载添加网络字节序长度头，拒绝超过协议上限的数据包。"""
    size = len(payload) + 1
    if len(kind) != 1 or not 1 <= size <= _MAX_PACKET:
        raise ValueError("Invalid background packet size")
    return _LENGTH.pack(size) + kind + payload


def encode_json(value: dict) -> bytes:
    """将命令字典编码为 UTF-8 JSON 数据包。"""
    return _packet(b"J", json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8"))


def decode_json(payload: bytes) -> dict:
    """解析 JSON 负载并要求顶层为字典，否则抛出解析异常或 ValueError。"""
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Background command must be an object")
    return value


def encode_image(image: QImage, kind: bytes = b"F", sequence: int = 0) -> bytes:
    """将非空图像转成预乘 ARGB32，连同序号、几何信息及像素比封装发送。"""
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise ValueError("Empty background image")
    header = _IMAGE.pack(sequence, image.width(), image.height(), image.bytesPerLine(), image.devicePixelRatio())
    return _packet(kind, header + bytes(image.constBits()))


def decode_image(payload: bytes) -> tuple[int, QImage]:
    """校验尺寸、行跨度、像素比和负载长度，返回序号及拥有独立像素的图像。"""
    if len(payload) < _IMAGE.size:
        raise ValueError("Truncated background image")
    sequence, width, height, stride, ratio = _IMAGE.unpack_from(payload)
    if (
        not 0 < width <= 16384
        or not 0 < height <= 16384
        or stride != width * 4
        or not math.isfinite(ratio)
        or not 0 < ratio <= 16
        or len(payload) - _IMAGE.size != stride * height
    ):
        raise ValueError("Invalid background image geometry")
    # 接收缓冲复用前取得独立像素所有权。
    image = QImage(payload[_IMAGE.size :], width, height, stride, QImage.Format.Format_ARGB32_Premultiplied).copy()
    image.setDevicePixelRatio(ratio)
    return sequence, image


class PacketReader:
    """累积流式输入并按长度头拆包，保留尚未接收完整的尾部数据。"""

    def __init__(self):
        """创建用于保存未完成数据包的接收缓冲。"""
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[bytes, bytes]]:
        """追加字节并取出完整的类型与负载列表；非法包长度会抛出 ValueError。"""
        self._buffer.extend(data)
        packets = []
        consumed = 0
        while len(self._buffer) - consumed >= _LENGTH.size:
            size = _LENGTH.unpack_from(self._buffer, consumed)[0]
            if not 1 <= size <= _MAX_PACKET:
                raise ValueError("Invalid background packet length")
            end = consumed + _LENGTH.size + size
            if len(self._buffer) < end:
                break
            start = consumed + _LENGTH.size
            packets.append((bytes(self._buffer[start : start + 1]), bytes(self._buffer[start + 1 : end])))
            consumed = end
        if consumed:
            del self._buffer[:consumed]
        return packets


class LatestImageSender:
    """最多保留一个发送中数据包和一张待替换图片，不累计帧队列。"""

    def __init__(self, send, *, image_writer=None):
        """配置发送回调及可选共享内存写入器，并初始化单帧确认状态。"""
        self._send = send
        self._image_writer = image_writer
        self._sequence = 0
        self._in_flight = None
        self._pending = None
        self._configuration = None

    @property
    def busy(self):
        """返回是否有尚未收到确认的已发送图像。"""
        return self._in_flight is not None

    def submit(self, image: QImage, *, configuration=None):
        """保存图像和配置快照，覆盖上一张待发图像并在通道空闲时发送。"""
        self._pending = QImage(image), copy.deepcopy(configuration)
        self._flush()

    def discard_pending(self):
        """尺寸或播放状态变化时丢弃尚未发送的旧帧。"""
        self._pending = None
        self._configuration = None

    def acknowledge(self, sequence: int):
        """仅接受当前在途序号的确认，并继续发送最新待发图像。"""
        if sequence == self._in_flight:
            self._in_flight = None
            self._flush()

    def _flush(self):
        """在无在途帧时发送待发图像，并记住序号以等待接收确认。

        使用共享内存时仅附带发生变化的配置；编码、写入和发送异常由调用方处理。
        """
        if self.busy or self._pending is None:
            return
        (image, configuration), self._pending = self._pending, None
        self._sequence += 1
        if self._image_writer is None:
            packet = encode_image(image, b"F", self._sequence)
        else:
            message = dict(action="foreground", sequence=self._sequence, image=self._image_writer.write(image))
            if configuration is not None and configuration != self._configuration:
                message["configuration"] = configuration
                self._configuration = configuration
            packet = encode_json(message)
        self._in_flight = self._sequence
        self._send(packet)
