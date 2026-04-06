"""有界的分类预设读写，快照暂存与最终发布由调用方负责。

所有函数同步执行，应由调用方的后台线程运行。输入字段不能决定文件系统目标。
写入器独占创建指定文件并读回校验，业务服务再使用不冲突的名称发布。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from src.core.category_preset import (
    CATEGORY_PRESET_EXTENSION,
    HEADER_SIZE,
    MAGIC,
    MANIFEST_KIND,
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_JSON_DEPTH,
    MAX_MANIFEST_BYTES,
    CategoryPresetCancelled,
    CategoryPresetDocument,
    CategoryPresetError,
    CategoryPresetFileAccessError,
    CategoryPresetFormatError,
    CategoryPresetImage,
    validate_manifest,
)
from src.utils import fs
from src.utils.i18n import t as tr

Cancelled = Callable[[], bool] | None
Progress = Callable[[str, int], None] | None
_HEADER = struct.Struct("<8sIQ32s")
_CHUNK_SIZE = 1024 * 1024
_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def _check_cancelled(cancelled: Cancelled) -> None:
    """调用取消检查器，收到取消请求时以分类预设专用异常终止当前操作。"""
    if cancelled is not None and cancelled():
        raise CategoryPresetCancelled(tr("分类预设操作已取消"))


def _progress(progress: Progress, phase: str, completed: int, total: int) -> None:
    """将已处理数量换算为零到一百的整数百分比，并通知可选的进度回调。"""
    if progress is not None:
        progress(phase, min(100, max(0, completed * 100 // max(1, total))))


def _signature(info: os.stat_result) -> tuple:
    """组合文件标识、大小和时间戳，供读取前后的来源一致性核对使用。"""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, fs.stat_identity_time_ns(info)


def _open_source(path: Path) -> BinaryIO:
    """读取内容前拒绝目录、链接及其他重解析点。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CategoryPresetError(tr("请选择普通文件，不能使用目录或链接"))
    stream = path.open("rb")
    if _signature(os.fstat(stream.fileno())) != _signature(info):
        stream.close()
        raise CategoryPresetError(tr("来源文件在打开时发生变化，请重新选择"))
    return stream


def _unchanged(stream: BinaryIO, path: Path, original: os.stat_result) -> None:
    """同时核对已打开句柄与路径当前指向的文件，任一身份或内容属性变化时拒绝继续。"""
    expected = _signature(original)
    if _signature(os.fstat(stream.fileno())) != expected or _signature(path.stat()) != expected:
        raise CategoryPresetError(tr("来源文件发生变化，请重新选择"))


def _remove_owned(path: Path, identity: tuple[int, int] | None) -> None:
    """仅删除仍具有本次创建身份的输出文件。"""
    if identity is None:
        return
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


class _ImageWindow(io.BufferedIOBase):
    """可定位的有界视图，限制 Pillow 只能读取指定图片的数据区。"""

    def __init__(self, stream: BinaryIO, offset: int, length: int, cancelled: Cancelled):
        """借用原始二进制流中的指定区间，建立独立读取位置并保存取消检查器。"""
        self._stream = stream
        self._offset = offset
        self._length = length
        self._position = 0
        self._cancelled = cancelled

    def readable(self):
        """声明图片区间支持读取，供图像解码器查询流能力。"""
        return True

    def seekable(self):
        """声明图片区间支持定位，允许解码器回读文件头或逐帧访问。"""
        return True

    def tell(self):
        """返回相对于图片区间起点的当前位置，不暴露底层流的绝对偏移。"""
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        """按图片区间的起点、当前位置或末尾定位；拒绝负位置与无效基准，并在定位前检查取消。"""
        _check_cancelled(self._cancelled)
        position = offset + (self._position if whence == io.SEEK_CUR else self._length if whence == io.SEEK_END else 0)
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END) or position < 0:
            raise ValueError("Invalid image seek")
        self._position = position
        return position

    def read(self, size=-1):
        """从当前位置读取至多区间内剩余字节，越过区间末尾时返回空数据。"""
        _check_cancelled(self._cancelled)
        remaining = max(0, self._length - self._position)
        amount = remaining if size is None or size < 0 else min(size, remaining)
        self._stream.seek(self._offset + self._position)
        data = self._stream.read(amount)
        self._position += len(data)
        return data

    def readinto(self, buffer):
        """将区间内可读数据复制到调用方缓冲区，并返回实际写入字节数。"""
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def _check_image_size(width: int, height: int) -> None:
    """拒绝非正尺寸或超过单张图片像素上限的图像。"""
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise CategoryPresetError(tr("单张图片像素数超过 40,000,000 或尺寸无效"))


def _preflight_gif(window: _ImageWindow) -> None:
    """在 Pillow 为帧分配内存前限制 GIF 画布与各帧尺寸。

    仅遍历头部、调色板和带长度前缀的数据块，单次读取不超过 768 字节。
    像素内容仍交给 Pillow 完整解码校验。"""
    if window.read(6) not in (b"GIF87a", b"GIF89a"):
        return

    def exact(length):
        """读取指定长度的 GIF 数据，数据不足时报告截断而非继续解析。"""
        data = window.read(length)
        if len(data) != length:
            raise CategoryPresetError(tr("GIF 图片数据不完整"))
        return data

    def skip_palette(flags):
        """根据 GIF 标志位跳过存在的颜色表，并验证表数据完整。"""
        if flags & 0x80:
            exact(3 * (1 << ((flags & 7) + 1)))

    def skip_blocks():
        """逐个跳过 GIF 长度前缀数据块，直到零长度结束标记。"""
        while True:
            length = exact(1)[0]
            if not length:
                return
            exact(length)

    width, height, flags, _, _ = struct.unpack("<HHBBB", exact(7))
    _check_image_size(width, height)
    skip_palette(flags)
    has_frame = False
    while True:
        marker = exact(1)
        if marker == b";":
            if not has_frame:
                raise CategoryPresetError(tr("GIF 图片没有有效帧"))
            return
        if marker == b"!":
            exact(1)  # 扩展标签后的数据使用长度前缀。
            skip_blocks()
        elif marker == b",":
            left, top, frame_width, frame_height, flags = struct.unpack("<HHHHB", exact(9))
            _check_image_size(frame_width, frame_height)
            # 分配原生图像内存前，按 Pillow 的规则累计帧偏移和画布范围。

            width = max(width, left + frame_width)
            height = max(height, top + frame_height)
            _check_image_size(width, height)
            skip_palette(flags)
            exact(1)  # 读取 LZW 最小编码尺寸；压缩数据由 Pillow 校验。
            skip_blocks()
            has_frame = True
        else:
            raise CategoryPresetError(tr("GIF 图片包含无效数据块"))


def _decode_image(stream: BinaryIO, offset: int, length: int, cancelled: Cancelled) -> str:
    """校验受支持图片的容器结构并逐帧解码，检查像素上限和取消请求，返回媒体类型。"""

    try:
        _check_cancelled(cancelled)
        _preflight_gif(_ImageWindow(stream, offset, length, cancelled))
        with Image.open(_ImageWindow(stream, offset, length, cancelled)) as image:
            media_type = _IMAGE_FORMATS.get(image.format)
            if media_type is None:
                raise CategoryPresetError(tr("只支持 PNG、JPEG、BMP、GIF 和 WebP 图片"))
            _check_image_size(*image.size)
            image.verify()
        # verify 不会解码 JPEG/GIF 像素，需逐帧解码，
        # 同时避免保存全部帧或修改 Pillow 的全局设置。
        with Image.open(_ImageWindow(stream, offset, length, cancelled)) as image:
            frame = 0
            while True:
                _check_cancelled(cancelled)
                _check_image_size(*image.size)
                image.load()
                frame += 1
                try:
                    image.seek(frame)
                except EOFError:
                    break
        _check_cancelled(cancelled)
        return media_type
    except (CategoryPresetError, CategoryPresetCancelled):
        raise
    except (OSError, ValueError, SyntaxError, struct.error, IndexError, Image.DecompressionBombError) as exc:
        raise CategoryPresetError(tr("目录图片损坏或无法解码：{exc}", exc=exc)) from exc


def _hash_bytes(stream, length, *, cancelled=None, output=None, progress=None, phase="校验文件"):
    """分块读取指定长度并计算 SHA-256，可同步复制到输出流；持续检查取消并报告进度。"""
    digest = hashlib.sha256()
    completed = 0
    while completed < length:
        _check_cancelled(cancelled)
        data = stream.read(min(_CHUNK_SIZE, length - completed))
        if not data:
            raise CategoryPresetError(tr("文件损坏或不完整，请重新获取或重新导出"))
        digest.update(data)
        if output is not None:
            output.write(data)
        completed += len(data)
        _progress(progress, phase, completed, length)
    _check_cancelled(cancelled)
    return digest.hexdigest()


def _parse_manifest(raw: bytes):
    """按 UTF-8 解析 JSON 清单，拒绝重复键、非有限数字和过深嵌套。"""

    def unique_object(pairs):
        """构建清单对象时拒绝重复字段，避免同一键出现歧义。"""
        value = {}
        for key, item in pairs:
            if key in value:
                raise CategoryPresetError(tr("清单存在重复 JSON 键：{key}", key=key))
            value[key] = item
        return value

    def invalid_constant(value):
        """拒绝 JSON 解析器默认接受的非有限数值常量。"""
        raise CategoryPresetError(tr("清单不能包含非有限数字：{value}", value=value))

    # 在 JSON 递归解析前检查嵌套深度，未知字段同样受限。
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
        elif byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise CategoryPresetError(tr("清单 JSON 嵌套超过 16 层"))
        elif byte in (93, 125):
            depth -= 1
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CategoryPresetError):
            raise
        raise CategoryPresetError(tr("清单不是有效 UTF-8 JSON：{exc}", exc=exc)) from exc


def _read_header(stream: BinaryIO, actual_length: int):
    """验证预设文件标识、总长及分区长度，返回清单长度、图片区长度和摘要。"""
    if not HEADER_SIZE < actual_length <= MAX_FILE_BYTES:
        raise CategoryPresetError(tr("文件损坏、不完整或超过 1 GiB"))
    header = stream.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE:
        raise CategoryPresetError(tr("文件头不完整"))
    magic, manifest_length, images_length, digest = _HEADER.unpack(header)
    if magic != MAGIC:
        raise CategoryPresetError(tr("请选择程序导出的分类预设文件（.d3dxcp）"))
    if not 0 < manifest_length <= MAX_MANIFEST_BYTES:
        raise CategoryPresetError(tr("清单长度无效或超过 16 MiB"))
    if HEADER_SIZE + manifest_length + images_length != actual_length:
        raise CategoryPresetError(tr("文件长度不一致，存在截断、尾部数据或超限长度"))
    return manifest_length, images_length, digest.hex()


def read_category_preset(path, *, cancelled=None, progress=None) -> CategoryPresetDocument:
    """校验完整文件，不在内存中保留图片二进制数据。"""
    path = Path(path)
    _check_cancelled(cancelled)
    if path.suffix.lower() != CATEGORY_PRESET_EXTENSION:
        raise CategoryPresetError(tr("请选择程序导出的分类预设文件（.d3dxcp）"))
    try:
        with _open_source(path) as stream:
            original = os.fstat(stream.fileno())
            try:
                manifest_length, images_length, digest = _read_header(stream, original.st_size)
                actual_digest = _hash_bytes(
                    stream,
                    manifest_length + images_length,
                    cancelled=cancelled,
                    progress=progress,
                )
                if actual_digest != digest or stream.read(1):
                    raise CategoryPresetError(tr("文件摘要不一致，请重新获取或重新导出"))
                stream.seek(HEADER_SIZE)
                raw_manifest = stream.read(manifest_length)
                if len(raw_manifest) != manifest_length:
                    raise CategoryPresetError(tr("文件清单不完整"))
                _check_cancelled(cancelled)
                document = validate_manifest(
                    _parse_manifest(raw_manifest),
                    fallback_name=path.stem,
                    images_length=images_length,
                )
                del raw_manifest
                for index, entry in enumerate(document.images):
                    offset = HEADER_SIZE + manifest_length + entry.offset
                    stream.seek(offset)
                    image_digest = _hash_bytes(stream, entry.length, cancelled=cancelled)
                    if image_digest != entry.image_id:
                        raise CategoryPresetError(tr("目录图片摘要不一致：{image_id}", image_id=entry.image_id))
                    if _decode_image(stream, offset, entry.length, cancelled) != entry.media_type:
                        raise CategoryPresetError(
                            tr("目录图片声明类型与实际类型不一致：{image_id}", image_id=entry.image_id)
                        )
                    _progress(progress, "校验图片", index + 1, len(document.images))
            except CategoryPresetError as exc:
                raise CategoryPresetFormatError(str(exc)) from exc
            _check_cancelled(cancelled)
            _unchanged(stream, path, original)
        result = replace(document, source_path=path.resolve(), manifest_length=manifest_length, payload_sha256=digest)
        _progress(progress, "读取完成", 1, 1)
        return result
    except OSError as exc:
        raise CategoryPresetFileAccessError(tr("无法读取分类预设文件：{exc}", exc=exc)) from exc


def validate_category_image(path, *, cancelled=None) -> tuple[str, str, int]:
    """返回内容 SHA-256、解码后的媒体类型与字节数。"""
    path = Path(path)
    _check_cancelled(cancelled)
    try:
        with _open_source(path) as stream:
            original = os.fstat(stream.fileno())
            if not 0 < original.st_size <= MAX_IMAGE_BYTES:
                raise CategoryPresetError(tr("单张目录图片必须为 1 字节至 20 MiB"))
            digest = _hash_bytes(stream, original.st_size, cancelled=cancelled)
            media_type = _decode_image(stream, 0, original.st_size, cancelled)
            _unchanged(stream, path, original)
            return digest, media_type, original.st_size
    except OSError as exc:
        raise CategoryPresetError(tr("无法读取目录图片：{exc}", exc=exc)) from exc


def _manifest_for_document(document, images):
    """将分类文档及图片位置索引转换为写入预设文件的清单结构。"""
    return {
        "kind": MANIFEST_KIND,
        "scope": "game",
        "name": document.name,
        "suggested_game": {"name": document.suggested_game_name, "name_en": document.suggested_game_name_en},
        "nodes": [asdict(node) for node in document.nodes],
        "images": [
            {"id": entry.image_id, "offset": entry.offset, "length": entry.length, "media_type": entry.media_type}
            for entry in images
        ],
    }


def write_category_preset(path, document, images: Mapping[str, Path], *, cancelled=None, progress=None):
    """独占创建临时预设文件，返回读回校验的结果。"""
    path = Path(path)
    _check_cancelled(cancelled)
    if path.suffix.lower() != CATEGORY_PRESET_EXTENSION:
        raise CategoryPresetError(tr("临时分类预设文件必须使用 .d3dxcp 扩展名"))
    if not isinstance(document, CategoryPresetDocument):
        raise CategoryPresetError(tr("必须提供完整分类预设文档"))
    referenced = {node.image_id for node in document.nodes if node.image_id}
    if set(images) != referenced:
        raise CategoryPresetError(tr("只能提供节点引用的全部目录图片"))
    # 图片读取前使用暂定长度校验路径和引用。
    ids = sorted(referenced)
    provisional = [CategoryPresetImage(image_id, index, 1, "image/png") for index, image_id in enumerate(ids)]
    validate_manifest(_manifest_for_document(document, provisional), fallback_name=path.stem, images_length=len(ids))
    entries = []
    total = 0
    for index, image_id in enumerate(ids):
        digest, media_type, length = validate_category_image(images[image_id], cancelled=cancelled)
        if digest != image_id:
            raise CategoryPresetError(tr("导出图片内容与引用 ID 不一致"))
        entries.append(CategoryPresetImage(image_id, total, length, media_type))
        total += length
        if total + HEADER_SIZE > MAX_FILE_BYTES:
            raise CategoryPresetError(tr("分类预设文件超过 1 GiB"))
        _progress(progress, "准备图片", index + 1, len(ids))
    manifest = _manifest_for_document(document, entries)
    validate_manifest(manifest, fallback_name=path.stem, images_length=total)
    raw_manifest = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw_manifest) > MAX_MANIFEST_BYTES or HEADER_SIZE + len(raw_manifest) + total > MAX_FILE_BYTES:
        raise CategoryPresetError(tr("清单超过 16 MiB 或分类预设文件超过 1 GiB"))
    identity = None
    try:
        _check_cancelled(cancelled)
        with path.open("xb") as output:
            created = os.fstat(output.fileno())
            identity = created.st_dev, created.st_ino
            output.write(_HEADER.pack(MAGIC, len(raw_manifest), total, bytes(32)))
            output.write(raw_manifest)
            payload_digest = hashlib.sha256(raw_manifest)
            written = 0
            for entry in entries:
                _check_cancelled(cancelled)
                with _open_source(Path(images[entry.image_id])) as source:
                    image_digest = hashlib.sha256()
                    remaining = entry.length
                    while remaining:
                        _check_cancelled(cancelled)
                        chunk = source.read(min(_CHUNK_SIZE, remaining))
                        if not chunk:
                            raise CategoryPresetError(tr("导出图片在准备后发生变化"))
                        image_digest.update(chunk)
                        payload_digest.update(chunk)
                        output.write(chunk)
                        remaining -= len(chunk)
                        written += len(chunk)
                        _progress(progress, "写入文件", written, total)
                    if source.read(1) or image_digest.hexdigest() != entry.image_id:
                        raise CategoryPresetError(tr("导出图片在准备后发生变化"))
            output.seek(0)
            output.write(_HEADER.pack(MAGIC, len(raw_manifest), total, payload_digest.digest()))
            output.flush()
            os.fsync(output.fileno())
        _check_cancelled(cancelled)
        verified = read_category_preset(path, cancelled=cancelled, progress=progress)
        _check_cancelled(cancelled)
        _progress(progress, "写入完成", 1, 1)
        return verified
    except BaseException:
        _remove_owned(path, identity)
        raise


def copy_category_preset_image(document, image_id, destination, *, cancelled=None):
    """将已校验的图片复制到调用方明确指定的目标。

    图片标识仅作为查询键，不覆盖已有文件；失败或取消时只删除本次创建的文件。"""
    _check_cancelled(cancelled)
    entry = next((item for item in document.images if item.image_id == image_id), None)
    if entry is None or document.source_path is None or not document.payload_sha256:
        raise CategoryPresetError(tr("图片不属于已校验的分类预设"))
    destination = Path(destination)
    source_path = Path(document.source_path)
    identity = None
    try:
        with _open_source(source_path) as source:
            original = os.fstat(source.fileno())
            manifest_length, images_length, digest = _read_header(source, original.st_size)
            if (
                manifest_length != document.manifest_length
                or images_length != document.images_length
                or digest != document.payload_sha256
            ):
                raise CategoryPresetError(tr("已校验的分类预设来源发生变化"))
            if entry.offset < 0 or entry.length <= 0 or entry.offset + entry.length > images_length:
                raise CategoryPresetError(tr("图片索引区间无效"))
            source.seek(HEADER_SIZE + manifest_length + entry.offset)
            with destination.open("xb") as output:
                created = os.fstat(output.fileno())
                identity = created.st_dev, created.st_ino
                actual_digest = _hash_bytes(source, entry.length, cancelled=cancelled, output=output)
                if actual_digest != entry.image_id:
                    raise CategoryPresetError(tr("已校验的目录图片发生变化"))
                output.flush()
            _check_cancelled(cancelled)
            _unchanged(source, source_path, original)
    except BaseException:
        _remove_owned(destination, identity)
        raise
