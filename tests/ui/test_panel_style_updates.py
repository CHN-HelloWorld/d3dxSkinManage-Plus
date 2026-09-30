from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPixmap

from src.models.mod import AgeRating, Mod, ModInfo
from src.ui.pages.overview import OverviewPage


class StyleChanges(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.StyleChange:
            self.count += 1
        return False


def test_repeated_empty_mod_selection_preserves_styles(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    first = Mod(mod_id="first", name="First")
    page._show_mod_panel(first)
    preview = StyleChanges(page._panel_preview._image_label)
    rating = StyleChanges(page._panel_rating_label)

    page._show_mod_panel(Mod(mod_id="second", name="Second"))

    assert preview.count == 0
    assert rating.count == 0
    assert page._panel_name_label.text() == "Second"
    assert "dashed" in page._panel_preview._image_label.styleSheet()


def test_cached_image_selection_has_no_placeholder_style_transition(
    qtbot,
    temp_data_dir,
    tmp_path,
):
    page = OverviewPage()
    qtbot.addWidget(page)
    image = QPixmap(32, 32)
    image.fill(QColor("red"))
    path = str(tmp_path / "preview.png")
    assert image.save(path)
    mod = Mod(mod_id="image", name="Image", preview_images=[path])
    card_image = QPixmap(1000, 1000)
    card_image.fill(QColor("red"))
    page._panel_preview.set_images_deferred(
        [path],
        cached_path=path,
        cached_pixmap=card_image,
    )
    changes = StyleChanges(page._panel_preview._image_label)

    page._show_mod_panel(mod)

    assert changes.count == 0
    assert "background: transparent" in page._panel_preview._image_label.styleSheet()
    assert not page._panel_preview._image_label.pixmap().isNull()


def test_rating_change_and_hidden_adult_restore_correct_styles(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page._show_mod_panel(Mod(mod_id="general"))
    previous = page._panel_rating_label.styleSheet()
    page._adult_mode = "hide"

    page._show_mod_panel(
        Mod(
            mod_id="adult",
            info=ModInfo(age_rating=AgeRating.X),
            preview_images=["hidden.png"],
        )
    )

    assert page._panel_rating_label.styleSheet() != previous
    assert page._panel_rating_label.text() == "X"
    assert page._panel_rating_label.toolTip() == "X · 18+ 内容"
    assert page._panel_preview._image_label.text() == "18+ 内容已隐藏"
    assert "dashed" in page._panel_preview._image_label.styleSheet()
