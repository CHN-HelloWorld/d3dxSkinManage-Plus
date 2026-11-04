"""用真实高压缩比固实归档验证解码分块、内容一致性和扫描次数。"""

from __future__ import annotations

import gc
import zlib
from weakref import WeakSet

import pytest

from src.core.mod_update_validation import validate_update_archive
from src.models.mod import Mod
from src.services.archive import ArchiveService
from src.ui.dialogs.mod_import import _detect_keys_from_archive

py7zr = pytest.importorskip("py7zr")


@pytest.fixture(scope="module")
def solid_archive(tmp_path_factory):
    """将大成员放在 INI 和图片前面，使定向读取也必须经过大段固实数据。"""
    root = tmp_path_factory.mktemp("solid_archive")
    payload = root / "texture.buf"
    with payload.open("wb") as output:
        for _ in range(16):
            output.write(b"\0" * (1024 * 1024))
    archive = root / "variants.7z"
    with py7zr.SevenZipFile(archive, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 1}]) as output:
        output.write(payload, "Mod/texture.buf")
        for index in range(21):
            output.writestr(f"[KeySwap]\nkey = key{index}\n", f"Mod/variant{index}/mod.ini")
        output.writestr(b"preview content", "Mod/variant0/preview.png")
    return archive


@pytest.fixture
def decoded_chunks(monkeypatch):
    """仅记录真实解码器返回的数据长度，保留全部解码和 CRC 校验行为。"""
    from py7zr.compressor import SevenZipDecompressor

    sizes = []
    original = SevenZipDecompressor.decompress

    def record(decoder, source, max_length=-1):
        """不保留数据本身，避免测量工具改变被测内存占用。"""
        data = original(decoder, source, max_length)
        sizes.append(len(data))
        return data

    monkeypatch.setattr(SevenZipDecompressor, "decompress", record)
    return sizes


def _exercise_archive_read(solid_archive, tmp_path, operation):
    """执行真实读取并校验内容，供分块与关闭后的资源回收测试共用。"""
    service = ArchiveService()
    preview = "Mod/variant0/preview.png"
    if operation == "read":
        assert service.read_file(solid_archive, preview) == b"preview content"
    elif operation == "inis":
        contents = service.read_all_inis(solid_archive)
        assert len(contents) == 21
        assert contents["Mod/variant20/mod.ini"] == b"[KeySwap]\nkey = key20\n"
    elif operation == "previews":
        paths = service.extract_files(solid_archive, [preview], tmp_path)
        assert paths == [tmp_path / preview]
        assert paths[0].read_bytes() == b"preview content"
    elif operation == "extract":
        root = service.extract(solid_archive, tmp_path)
        assert root == tmp_path / "Mod"
        assert (root / "texture.buf").stat().st_size == 16 * 1024 * 1024
        assert (root / "variant0/preview.png").read_bytes() == b"preview content"
    elif operation == "fingerprint":
        assert Mod._7z_entries_by_bytes(solid_archive, [preview]) == [(preview, 15, zlib.crc32(b"preview content"))]
    else:
        validate_update_archive(solid_archive, service)


@pytest.mark.parametrize("operation", ["read", "inis", "previews", "extract", "fingerprint", "validate"])
def test_solid_7z_reads_use_bounded_output(solid_archive, tmp_path, decoded_chunks, operation):
    _exercise_archive_read(solid_archive, tmp_path, operation)
    assert decoded_chunks
    assert max(decoded_chunks) <= 4 * 1024 * 1024, "单次解码不能随成员大小膨胀"


def test_key_scan_decodes_solid_archive_once_and_keeps_twenty_ini_limit(solid_archive, decoded_chunks):
    assert _detect_keys_from_archive(solid_archive) == [f"key{index}" for index in range(20)]
    assert sum(decoded_chunks) < 17 * 1024 * 1024, "读取多个 INI 不得反复解码前置纹理"


def test_multiblock_archive_releases_previous_decoder(tmp_path, monkeypatch):
    """多个独立压缩块不能同时保留全部字典；换块后仍须完整提取各成员。"""
    from py7zr.compressor import SevenZipDecompressor

    archive = tmp_path / "blocks.7z"
    for index in range(3):
        with py7zr.SevenZipFile(archive, "w" if index == 0 else "a") as output:
            output.writestr(bytes([index]) * (1024 * 1024), f"Mod/part{index}.buf")
    live = WeakSet()
    counts = []
    original = SevenZipDecompressor.__init__

    def record(decoder, *args, **kwargs):
        """通过弱引用观察真实解码器存活数，不延长其生命周期。"""
        original(decoder, *args, **kwargs)
        live.add(decoder)
        counts.append(len(live))

    monkeypatch.setattr(SevenZipDecompressor, "__init__", record)
    root = ArchiveService().extract(archive, tmp_path / "out")
    assert root is not None
    for index in range(3):
        assert (root / f"part{index}.buf").read_bytes() == bytes([index]) * (1024 * 1024)
    assert max(counts) == 1, "单个归档不能按压缩块数量累积解码字典"


@pytest.mark.parametrize("operation", ["read", "inis", "previews", "extract", "fingerprint", "validate"])
def test_completed_reads_release_decoders_without_cyclic_gc(solid_archive, tmp_path, monkeypatch, operation):
    """读取上下文结束就释放解码字典，不依赖不可预测的循环垃圾回收时机。"""
    from py7zr.compressor import SevenZipDecompressor

    live = WeakSet()
    original = SevenZipDecompressor.__init__

    def record(decoder, *args, **kwargs):
        """弱引用解码器以观察实际释放，保持后端初始化行为。"""
        original(decoder, *args, **kwargs)
        live.add(decoder)

    monkeypatch.setattr(SevenZipDecompressor, "__init__", record)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for index in range(3):
            _exercise_archive_read(solid_archive, tmp_path / str(index), operation)
            assert not live, "归档已关闭后仍保留了解码字典"
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("method", ["list_contents", "list_entries"])
def test_archive_listing_releases_member_lists_without_cyclic_gc(solid_archive, monkeypatch, method):
    """成员清单也有 folder/files 互相引用，关闭后不应逐轮保留完整目录树。"""
    from py7zr.py7zr import ArchiveFileList

    live = WeakSet()
    original = ArchiveFileList.__init__

    def record(members, *args, **kwargs):
        """记录实际成员容器，不保留其强引用。"""
        original(members, *args, **kwargs)
        live.add(members)

    monkeypatch.setattr(ArchiveFileList, "__init__", record)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(3):
            assert len(getattr(ArchiveService(), method)(solid_archive)) == 23
            assert not live, "成员列表在归档关闭后仍被循环引用保留"
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("failure", ["checkpoint", "decode_limit"])
def test_interrupted_reads_release_decoder(solid_archive, monkeypatch, failure):
    """中途取消和解压限制异常也必须退出资源上下文，不能保留已分配的字典。"""
    from py7zr.compressor import SevenZipDecompressor
    from py7zr.exceptions import DecompressionBombError
    from py7zr.io import NullIOFactory

    from src.services.seven_zip import open_7z

    live = WeakSet()
    original = SevenZipDecompressor.__init__

    def record(decoder, *args, **kwargs):
        """记录取消前真实创建的解码器，不延长其生命周期。"""
        original(decoder, *args, **kwargs)
        live.add(decoder)

    calls = 0

    def checkpoint():
        """允许启动及第一个数据块写入，在下一次写入时取消。"""
        nonlocal calls
        calls += 1
        return calls < 3

    monkeypatch.setattr(SevenZipDecompressor, "__init__", record)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        if failure == "checkpoint":
            with pytest.raises(InterruptedError):
                Mod._7z_entries_by_bytes(solid_archive, ["Mod/texture.buf"], checkpoint=checkpoint)
        else:
            with pytest.raises(DecompressionBombError), open_7z(solid_archive) as reader:
                reader.max_extract_size = 1
                reader.extractall(factory=NullIOFactory())
        assert not live, "读取异常后仍保留解码字典"
    finally:
        if was_enabled:
            gc.enable()
