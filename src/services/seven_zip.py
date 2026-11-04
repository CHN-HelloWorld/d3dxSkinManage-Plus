"""为单次 7z 读取限制解码输出块和同时存活的解码字典。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from src.utils import fs

_DECODE_CHUNK = 4 * 1024 * 1024


class _ChunkedDecoder:
    """保留原解码器的校验状态，仅限制每次请求的输出长度。"""

    def __init__(self, decoder) -> None:
        """持有当前压缩块的解码器，随压缩块切换释放其字典。"""
        self._decoder = decoder

    def decompress(self, source, max_length: int = -1) -> bytes:
        """限制高压缩比内容的临时输出，剩余数据由原解码循环继续消费。"""
        limit = _DECODE_CHUNK if max_length < 0 else min(max_length, _DECODE_CHUNK)
        return self._decoder.decompress(source, limit)

    def __getattr__(self, name):
        """将 CRC、摘要和完整性校验接口交回原解码器。"""
        return getattr(self._decoder, name)


@contextmanager
def open_7z(path: str | Path):
    """打开供一次提取使用的 7z，退出时关闭归档和输入流。

    py7zr 1.x 的 blocksize 只限制压缩输入，无法约束解码输出。适配实例的
    Worker 可保留原提取循环、CRC 和进度处理；传入文件流使库按压缩块顺序读取。
    调用方每次提取重新打开，不在此上下文中使用 reset 或写入模式。
    """
    import py7zr
    from py7zr.py7zr import Worker

    class _ChunkedWorker(Worker):
        """单个归档只保留当前压缩块的字典，避免多块归档累积内存。"""

        def __init__(self, reader) -> None:
            """复用已解析的成员和头信息，不改变归档格式及成员筛选。"""
            super().__init__(reader.files, reader.afterheader, reader.header)
            self._active_folder = None

        def decompress(self, fp, folder, fq, size, compressed_size, src_end, q=None) -> int:
            """换块时释放旧字典，并通过限长解码器执行原有完整性校验。"""
            if self._active_folder is not folder:
                if self._active_folder is not None:
                    self._active_folder.decompressor = None
                self._active_folder = folder
            if not isinstance(folder.decompressor, _ChunkedDecoder):
                folder.decompressor = _ChunkedDecoder(folder.get_decompressor(compressed_size))
            return super().decompress(fp, folder, fq, size, compressed_size, src_end, q)

    with open(fs.native_path(path), "rb") as source, py7zr.SevenZipFile(source, mode="r") as reader:
        reader.worker = _ChunkedWorker(reader)
        try:
            yield reader
        finally:
            # py7zr 关闭文件后仍有 folder -> files -> folder 循环引用；
            # 每个循环会保留整个解码字典，不能等待 Python 定期回收才释放。
            streams = reader.header.main_streams
            if streams is not None:
                for folder in streams.unpackinfo.folders:
                    folder.decompressor = None
                    folder.files = None
            reader.worker._active_folder = None
