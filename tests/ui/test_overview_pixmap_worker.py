from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy

from src.ui.pages.overview import _PixmapLoadWorker
from src.utils import fs


def test_pixmap_worker_uses_native_reader_and_bounds_cache(qtbot, tmp_path, monkeypatch):
    source_size = QSize(1600, 900)
    source = QImage(source_size, QImage.Format.Format_ARGB32)
    source.fill(QColor("#3a7bd5"))

    image_dir = tmp_path
    while len(str((image_dir / "preview-heart.png").resolve())) <= 280:
        image_dir /= "long-preview-segment-xxxxxxxxxxxxxxxxxxxxxxxx"
    fs.ensure_dir(image_dir)
    image_path = image_dir / "preview-heart.png"
    assert len(str(image_path.resolve())) > 260
    assert source.save(fs.native_path(image_path), "PNG")

    native_calls: list[str] = []
    original_native_path = fs.native_path

    def tracked_native_path(path) -> str:
        native_calls.append(str(path))
        return original_native_path(path)

    def forbidden_read_bytes(_path):
        raise AssertionError("preview worker must not decode through QImage.loadFromData")

    monkeypatch.setattr(fs, "native_path", tracked_native_path)
    monkeypatch.setattr(fs, "read_bytes", forbidden_read_bytes)

    worker = _PixmapLoadWorker()
    loaded = QSignalSpy(worker.image_loaded)
    worker.start()
    try:
        worker.enqueue("mod-id", str(image_path), 192, 179)
        qtbot.waitUntil(lambda: loaded.count() == 1, timeout=5000)

        item_id, source_path, request_id, result = loaded.at(0)
        image = result.image
        expected_size = source_size.scaled(
            worker._MAX_CACHE_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        assert item_id == "mod-id"
        assert source_path == str(image_path)
        assert request_id == 0
        assert result.is_current(source_path)
        assert isinstance(image, QImage)
        assert image.size() == expected_size
        # 文件身份检查和原生解码器均须支持长路径。
        assert native_calls and set(native_calls) == {str(image_path)}

        # 等待下一任务前须销毁图像读取器，避免 Windows 继续锁定上一张预览文件。
        fs.unlink(image_path)
        assert not image_path.exists()
    finally:
        worker.stop()
        assert worker.wait(1000)
