from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from src.ui.dialogs.operation_failure import OperationFailureDialog
from src.ui.dialogs.rebuild_progress import RebuildProgressDialog


def test_error_dialog_is_compact_and_wraps(qtbot):
    dialog = OperationFailureDialog("路径已变动，无法完成此次操作。" * 8)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.width() <= 380
    assert dialog.findChild(QLabel).wordWrap()
    dialog.close()


def test_check_dialog_has_task_title(qtbot):
    from time import sleep

    from src.ui.operation_check import run_file_check

    parent = QWidget()
    qtbot.addWidget(parent)
    titles = []
    timer = QTimer(parent)
    timer.timeout.connect(
        lambda: titles.extend(
            w.windowTitle() for w in QApplication.topLevelWidgets() if w.isVisible() and hasattr(w, "minimumDuration")
        )
    )
    timer.start(20)
    try:
        run_file_check(lambda: sleep(0.25), parent)
    finally:
        timer.stop()
    assert titles and set(titles) == {"检查文件"}


def test_rebuild_percentage_stays_outside_filled_track(qtbot):
    dialog = RebuildProgressDialog(None, "preview")
    qtbot.addWidget(dialog)
    dialog._started = True
    dialog.show()
    assert not dialog._bar.isTextVisible()
    for done, total, expected in [(0, 100, "0%"), (51, 100, "51%"), (100, 100, "100%"), (0, 0, "—")]:
        dialog._phase_progress("扫描", done, total)
        assert dialog._percentage.text() == expected
    assert not dialog._bar.geometry().intersects(dialog._percentage.geometry())
    dialog.close()


def test_retry_button_keeps_fill_inside_transparent_category_menu(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QMenu, QStyle, QStyleOptionButton

    from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar, _ScrollableCategoryPicker

    menu = QMenu()
    qtbot.addWidget(menu)
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    bar._style_category_menu(menu)
    picker = _ScrollableCategoryPicker(menu)
    button = picker._retry
    button.resize(120, 32)
    button.ensurePolished()
    image = QImage(120, 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOptionButton()
    option.initFrom(button)
    button.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, button)
    painter.end()
    assert image.pixelColor(8, 8).alpha() == 255
