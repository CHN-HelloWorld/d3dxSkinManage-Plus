from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from src.models.mod import Mod
from src.models.tag import TagDraft
from src.ui import theme
from src.ui.widgets.tag_picker import TagPicker


class StyleChanges(QObject):
    def __init__(self, widgets):
        super().__init__()
        self.count = 0
        for widget in widgets:
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.StyleChange:
            self.count += 1
        return False


def make_picker(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    picker = TagPicker(TagDraft(Mod(name="Palette probe"), "", [], []), host)
    layout.addWidget(picker)
    host.show()
    picker.ensurePolished()
    return host, picker


def test_unchanged_palette_does_not_restyle_tag_children(qtbot):
    host, picker = make_picker(qtbot)
    watcher = StyleChanges(picker.findChildren(QWidget))
    picker.input.setText("unfinished tag")

    for _ in range(3):
        QApplication.sendEvent(picker, QEvent(QEvent.Type.PaletteChange))

    assert watcher.count == 0
    assert picker.input.text() == "unfinished tag"


def test_real_theme_and_accent_changes_still_update_tag_colors(qtbot, monkeypatch):
    previous = theme.current_theme()
    try:
        theme.set_theme("light")
        host, picker = make_picker(qtbot)
        watcher = StyleChanges(picker.findChildren(QWidget))
        picker.input.setText("unfinished tag")
        light_editor_style = picker._editor.styleSheet()

        theme.set_theme("dark")
        QApplication.sendEvent(picker, QEvent(QEvent.Type.PaletteChange))
        assert watcher.count > 0
        assert picker._editor.styleSheet() != light_editor_style
        assert picker.error_label.palette().color(QPalette.ColorRole.WindowText) == QColor(theme.color("danger"))

        original_popup = picker.popup.styleSheet()
        monkeypatch.setitem(theme._palette("dark"), "accent.subtle", "#123456")
        QApplication.sendEvent(picker, QEvent(QEvent.Type.PaletteChange))
        assert picker.popup.styleSheet() != original_popup
        assert "#123456" in picker.popup.styleSheet()
        assert picker.input.text() == "unfinished tag"

        watcher.count = 0
        QApplication.sendEvent(picker, QEvent(QEvent.Type.PaletteChange))
        assert watcher.count == 0
    finally:
        theme.set_theme(previous)
