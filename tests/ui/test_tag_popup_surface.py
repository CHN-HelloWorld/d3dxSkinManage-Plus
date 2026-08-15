import pytest
from PySide6.QtGui import QColor

from src.models.mod import Mod
from src.models.tag import TagDraft
from src.ui import theme
from src.ui.widgets.tag_picker import TagPicker


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_tag_popup_paints_its_surface_but_leaves_top_corners_transparent(qtbot, qapp, mode):
    previous_theme, previous_style = theme.current_theme(), qapp.styleSheet()
    try:
        theme.set_theme(mode)
        qapp.setStyleSheet(theme.render_qss(mode))
        picker = TagPicker(TagDraft.from_mod(Mod()), inline_editor=True)
        qtbot.addWidget(picker)
        picker.resize(360, 280)
        picker.show()
        picker.show_popup()
        qapp.processEvents()

        image = picker.popup.grab().toImage()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(image.width() - 1, 0).alpha() == 0
        assert image.pixelColor(image.width() // 2, 4) == QColor(theme.color("bg.layer"))
    finally:
        theme.set_theme(previous_theme)
        qapp.setStyleSheet(previous_style)
