import gc
import weakref

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QTableWidgetItem

from src.ui.dialogs.mod_import import ModImportDialog
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils.i18n import t


def test_closed_import_dialog_can_be_collected_without_callback_errors(qapp, qtbot, temp_data_dir):
    def close_dialog():
        dialog = ModImportDialog()
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        dialog.show()
        qapp.processEvents()
        dialog.close()
        return tuple(weakref.ref(obj) for obj in (dialog, dialog._table, dialog.findChild(TagFlowLayout)))

    with qtbot.captureExceptions() as exceptions:
        for _ in range(3):
            references = close_dialog()
            gc.collect()
            qapp.processEvents()
            assert all(reference() is None for reference in references)

    assert exceptions == []


def test_blank_import_table_click_clears_selection(qapp, qtbot, temp_data_dir):
    dialog = ModImportDialog()
    qtbot.addWidget(dialog)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    table = dialog._table
    table.setRowCount(1)
    table.setItem(0, 0, QTableWidgetItem("Example"))
    dialog.show()
    qapp.processEvents()
    table.setCurrentCell(0, 0)
    assert table.selectedIndexes()
    dialog._preview_label.setText("Previous selection")
    blank = QPoint(12, table.viewport().height() - 8)
    assert not table.indexAt(blank).isValid()

    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=blank)

    assert not table.selectedIndexes()
    assert table.currentRow() == -1
    assert dialog._preview_label.text() == t("无预览图")


def test_import_header_drag_preserves_user_width_on_window_resize(qapp, qtbot, temp_data_dir):
    dialog = ModImportDialog()
    qtbot.addWidget(dialog)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    dialog.show()
    qapp.processEvents()
    header = dialog._table.horizontalHeader()
    initial_width = header.sectionSize(0)
    start = QPoint(header.sectionViewportPosition(1) - 1, header.height() // 2)
    end = start + QPoint(60, 0)

    qtbot.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(header.viewport(), end)
    qtbot.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
    resized_width = header.sectionSize(0)
    assert resized_width > initial_width

    dialog.resize(dialog.width() + 120, dialog.height())
    qapp.processEvents()

    assert header.sectionSize(0) == resized_width
