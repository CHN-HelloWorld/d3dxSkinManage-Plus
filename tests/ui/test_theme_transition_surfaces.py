import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from src.ui import theme
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.toast import Toast


@pytest.fixture
def appearance(qapp):
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()

    def apply(mode, background=False):
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(theme.render_qss(mode) + (theme.background_overlay_qss(mode) if background else ""))

    yield apply
    mode, background, sheet = previous
    theme.set_theme(mode)
    theme.set_background_active(background)
    qapp.setStyleSheet(sheet)


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("refresh", ["visible", "recycled"])
def test_category_summary_follows_theme_without_recreating_card(
    qtbot,
    qapp,
    temp_data_dir,
    appearance,
    background,
    refresh,
):
    appearance("dark", background)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.show()
    record = {"id": "category", "name": "Category", "mod_count": 7, "enabled_count": 3}
    page.set_characters([record])
    qtbot.waitUntil(lambda: "category" in page._vs_card_cache)
    card = page._vs_card_cache["category"]
    card.set_selected(True)
    original_text = card._info_label.text()
    old_ink = card._info_label.palette().color(QPalette.ColorRole.WindowText)

    appearance("light", background)
    if refresh == "visible":
        page.retheme()
        assert page._vs_card_cache["category"] is card
        assert card._selected
    else:
        page._vs_create_card({"type": "category", "id": "category", "data": record}, pooled_card=card)
    qapp.processEvents()

    ink = card._info_label.palette().color(QPalette.ColorRole.WindowText)
    assert ink != old_ink, "Reusing a card must not retain the previous theme's summary color"
    assert ink == QColor(theme.surface_text_color("text.secondary"))
    assert card._info_label.text() == original_text
    page.close()


@pytest.mark.parametrize("start,end", [("light", "dark"), ("dark", "light")])
def test_visible_theme_notification_keeps_clear_corners(qtbot, qapp, appearance, start, end):
    appearance(start)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(700, 300)
    parent.show()
    toast = Toast(parent)
    toast.busy("Applying theme")
    qapp.processEvents()

    appearance(end)
    qapp.processEvents()
    for completed in (False, True):
        if completed:
            toast.done("Theme applied")
        image = toast.grab().toImage()
        for x, y in ((0, 0), (image.width() - 1, 0), (0, image.height() - 1), (image.width() - 1, image.height() - 1)):
            assert image.pixelColor(x, y).alpha() == 0, "Rounded corners must not retain a square palette fill"
        assert image.pixelColor(image.width() // 2, 4) == QColor(theme.color("bg.card"))
    toast.hide()
