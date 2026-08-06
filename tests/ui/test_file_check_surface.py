from PySide6.QtCore import Qt

from src.ui.operation_check import create_file_check_dialog
from src.ui.widgets.rounded_progress import RoundedProgressBar


def test_file_check_uses_clipped_busy_bar_and_stops_timer_when_hidden(qtbot):
    dialog = create_file_check_dialog(None)
    qtbot.addWidget(dialog)
    bar = dialog.findChild(RoundedProgressBar)
    assert bar is not None
    assert (bar.minimum(), bar.maximum()) == (0, 0)
    dialog.show()
    qtbot.wait(40)
    assert bar._timer.isActive()
    dialog.hide()
    assert not bar._timer.isActive()


def test_busy_indicator_keeps_rounded_track_at_both_animation_ends(qtbot):
    bar = RoundedProgressBar()
    qtbot.addWidget(bar)
    bar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    bar.resize(160, 18)
    bar.setRange(0, 0)
    bar.show()

    class Clock:
        value = 0

        def elapsed(self):
            return self.value

    clock = Clock()
    bar._clock = clock
    for elapsed in (0, 800, 1599):
        clock.value = elapsed
        image = bar.grab().toImage()
        assert image.pixelColor(0, 6).alpha() < 100
        assert image.pixelColor(159, 6).alpha() < 100
        assert image.pixelColor(80, 8).alpha() > 200
    bar.close()
