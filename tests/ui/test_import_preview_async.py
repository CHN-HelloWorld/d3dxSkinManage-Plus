from threading import Event, get_ident
from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from src.ui.dialogs import mod_import


def test_import_selection_decodes_off_gui_thread_and_discards_old_result(qtbot, monkeypatch):
    gui_thread = get_ident()
    started, release = Event(), Event()
    threads = []

    def decode(path, size, **kwargs):
        threads.append(get_ident())
        if path == "first":
            started.set()
            release.wait(2)
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        image.fill(QColor("red" if path == "first" else "blue"))
        return image

    monkeypatch.setattr(mod_import, "load_scaled_qimage_file", decode)
    dialog = mod_import.ModImportDialog()
    qtbot.addWidget(dialog)
    dialog._entries = [
        SimpleNamespace(previews=[path], selected_preview_path="", selected_preview_index=0)
        for path in ("first", "second")
    ]
    try:
        dialog._show_current_preview(0)
        qtbot.waitUntil(started.is_set)
        assert threads and all(thread != gui_thread for thread in threads)
        dialog._show_current_preview(1)
        qtbot.waitUntil(
            lambda: (
                bool(dialog._preview_label.pixmap())
                and dialog._preview_label.pixmap().toImage().pixelColor(0, 0) == QColor("blue")
            )
        )
        release.set()
        qtbot.wait(50)
        assert dialog._preview_label.pixmap().toImage().pixelColor(0, 0) == QColor("blue")
    finally:
        release.set()


def test_import_preview_reads_real_image_at_display_size(qtbot, tmp_path):
    path = tmp_path / "preview.jpg"
    image = QImage(2400, 1200, QImage.Format.Format_RGB32)
    image.fill(QColor("green"))
    assert image.save(str(path))
    dialog = mod_import.ModImportDialog()
    qtbot.addWidget(dialog)
    dialog._entries = [SimpleNamespace(previews=[str(path)], selected_preview_path="", selected_preview_index=0)]
    dialog._show_current_preview(0)
    assert dialog._entries[0].selected_preview_path == str(path)
    qtbot.waitUntil(lambda: not dialog._preview_label.pixmap().isNull())
    pixmap = dialog._preview_label.pixmap()
    assert pixmap.width() <= dialog._preview_label.width() * dialog.devicePixelRatioF()
    assert pixmap.height() <= dialog._preview_label.height() * dialog.devicePixelRatioF()
    assert abs(pixmap.width() / pixmap.height() - 2) < 0.01


def test_import_clear_invalidates_inflight_preview(qtbot, monkeypatch):
    started, release, finished = Event(), Event(), Event()

    def decode(*args, **kwargs):
        started.set()
        release.wait(2)
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        finished.set()
        return image

    monkeypatch.setattr(mod_import, "load_scaled_qimage_file", decode)
    dialog = mod_import.ModImportDialog()
    qtbot.addWidget(dialog)
    dialog._entries = [SimpleNamespace(previews=["first"], selected_preview_path="", selected_preview_index=0)]
    try:
        dialog._show_current_preview(0)
        qtbot.waitUntil(started.is_set)
        dialog._entries.clear()
        dialog._show_current_preview()
        release.set()
        qtbot.waitUntil(finished.is_set)
        qtbot.wait(30)
        assert dialog._preview_label.pixmap().isNull()
        assert dialog._current_preview_path == ""
    finally:
        release.set()
