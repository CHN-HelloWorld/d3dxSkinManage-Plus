import gc
import weakref

import pytest
from PySide6.QtWidgets import QDialog, QProgressDialog

from src.ui.dialog_chrome import decorate_dialog


@pytest.mark.parametrize("kind", [QDialog, QProgressDialog])
def test_closed_dialog_can_be_collected_without_frame_callback_errors(
    qapp,
    qtbot,
    temp_data_dir,
    kind,
):
    def close_dialog():
        dialog = kind()
        decorate_dialog(dialog)
        dialog.show()
        dialog.close()
        return weakref.ref(dialog), weakref.ref(dialog._dialog_frame)

    with qtbot.captureExceptions() as exceptions:
        references = close_dialog()
        gc.collect()
        qapp.processEvents()

    assert exceptions == []
    assert all(reference() is None for reference in references)
