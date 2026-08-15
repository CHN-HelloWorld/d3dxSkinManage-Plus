import pytest
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QColor, QEnterEvent, QPixmap
from PySide6.QtWidgets import QFrame, QWidget

from src.models.mod import AgeRating, Mod, ModInfo, ModStatus
from src.ui import theme
from src.ui.widgets.mod_card import ModCard


class _StyleEvents(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.changed = []

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.StyleChange:
            self.changed.append(watched)
        return False


@pytest.mark.parametrize("operation", ["selection", "enabled", "rebind"])
def test_card_state_does_not_repolish_unrelated_children(qtbot, operation):
    card = ModCard(Mod(mod_id="one", info=ModInfo(tags=["Shared"])))
    qtbot.addWidget(card)
    card.ensurePolished()
    events = _StyleEvents(card)
    watched = [card._preview_container, card._info_widget, card._name_label, card._type_badge, card._tag_row]
    for child in watched:
        child.installEventFilter(events)
    card.set_selected(True)
    events.changed.clear()

    if operation == "selection":
        card.set_selected(False)
    elif operation == "enabled":
        card._mod.enable()
        card._update_status()
    else:
        card.set_mod_deferred(Mod(mod_id="two", info=ModInfo(tags=["Shared"])))

    assert events.changed == [], "card chrome changes must not send StyleChange down the subtree"


def _hover(widget, enabled, qapp):
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, enabled)
    event = QEnterEvent(QPointF(), QPointF(), QPointF()) if enabled else QEvent(QEvent.Type.Leave)
    qapp.sendEvent(widget, event)


def _old_style(enabled, selected, cut=False):
    if cut:
        return f"#modCard {{ border: 2px dashed {theme.color('warning')}; background-color: rgba(232,168,56,0.08); }}"
    if enabled:
        border = f"2px solid {theme.color('success')}"
    elif selected:
        border = f"1px solid {theme.color('accent')}"
    else:
        return ""
    background = f"background-color: {theme.surface_color('accent.subtle', 'card')};" if selected else ""
    return f"#modCard {{ border: {border}; {background} }}"


@pytest.mark.parametrize("mode,background", [("light", False), ("dark", False), ("light", True), ("dark", True)])
@pytest.mark.parametrize(
    "enabled,selected,hover,cut",
    [
        (False, False, False, False),
        (False, False, True, False),
        (False, True, True, False),
        (True, False, False, False),
        (True, False, True, False),
        (True, True, True, False),
        (False, False, False, True),
        (True, True, True, True),
    ],
)
def test_painted_surface_matches_original_qss_colors(
    qtbot, qapp, monkeypatch, mode, background, enabled, selected, hover, cut
):
    previous_sheet = qapp.styleSheet()
    monkeypatch.setattr(theme, "_current_theme", mode)
    monkeypatch.setattr(theme, "_background_active", background)
    qapp.setStyleSheet(theme.render_qss(mode) + (theme.background_overlay_qss(mode) if background else ""))
    host = QWidget()
    host.setObjectName("paintHost")
    host.setStyleSheet("#paintHost { background: #314159; }")
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(host)
    host.resize(440, 300)
    card = ModCard(Mod(mod_id="one", status=ModStatus.ENABLED if enabled else ModStatus.DISABLED), parent=host)
    card.move(10, 10)
    card.set_selected(selected)
    card.set_cut_state(cut)
    reference = QFrame(host)
    reference.setObjectName("modCard")
    reference.setGeometry(230, 10, 200, 280)
    reference.setStyleSheet(_old_style(enabled, selected, cut))
    for child in card.findChildren(QWidget):
        child.hide()
    try:
        host.show()
        for widget in (card, reference):
            widget.setProperty("_appearanceTheme", mode)
            widget.setProperty("_backgroundSurface", background)
            _hover(widget, hover, qapp)
        image = host.grab().toImage()
        # 空白内部像素验证悬停色和重复混合，顶部直边验证启用或选中边框。
        samples = [(100, 240), (0, 0)] if cut else [(100, 240), (100, 0), (0, 0)]
        for x, y in samples:
            actual = image.pixelColor(10 + x, 10 + y)
            expected = image.pixelColor(230 + x, 10 + y)
            assert max(abs(a - b) for a, b in zip(actual.getRgb(), expected.getRgb())) <= 2
        if cut:
            warning = QColor(theme.color("warning"))
            # 同时存在启用、选中或悬停状态时，警告虚线仍须显示正确颜色及间隙。
            dashes = sum(image.pixelColor(10 + x, 10) == warning for x in range(30, 170))
            assert 20 < dashes < 130
    finally:
        host.hide()
        qapp.setStyleSheet(previous_sheet)


def test_cut_state_survives_selection_and_invalidates_pixels_but_resets_on_rebind(qtbot):
    card = ModCard(Mod(mod_id="one"))
    qtbot.addWidget(card)
    key = card.scroll_render_key()
    card.set_cut_state(True)
    assert card.scroll_render_key() != key
    card.set_selected(True)
    card._mod.enable()
    card._update_status()
    assert card._cut_state
    card.set_mod_deferred(Mod(mod_id="two"))
    assert not card._cut_state and not card._selected


def test_status_updates_preserve_adult_preview_blur(qtbot):
    card = ModCard(Mod(mod_id="adult", info=ModInfo(age_rating=AgeRating.X)), adult_mode="blur")
    qtbot.addWidget(card)
    picture = QPixmap(32, 32)
    picture.fill(QColor("red"))
    assert card.set_preview_pixmap(picture)
    effect = card._preview_label.graphicsEffect()
    card.set_selected(True)
    card._mod.enable()
    card._update_status()
    assert card._preview_label.graphicsEffect() is effect
    assert effect.blurRadius() == 20


def test_painted_surface_cache_tracks_live_material_color_and_radius(qtbot, monkeypatch):
    card = ModCard(Mod(mod_id="one"))
    qtbot.addWidget(card)
    card.setProperty("_appearanceTheme", "light")
    card.setProperty("_backgroundSurface", False)
    key = card.scroll_render_key()
    card.setProperty("_appearanceTheme", "dark")
    dark_key = card.scroll_render_key()
    assert dark_key != key
    card.setProperty("_backgroundSurface", True)
    acrylic_key = card.scroll_render_key()
    assert acrylic_key != dark_key
    monkeypatch.setitem(theme.RADII, "card", theme.radius("card") + 1)
    assert card.scroll_render_key() != acrylic_key


def test_scroll_cache_checks_do_not_allocate_paint_colors(qtbot, monkeypatch):
    from src.ui.widgets import mod_card

    card = ModCard(Mod())
    qtbot.addWidget(card)
    expected = card.scroll_render_key()
    allocations = []
    original = mod_card.QColor

    def color(*args):
        allocations.append(args)
        return original(*args)

    monkeypatch.setattr(mod_card, "QColor", color)
    for _ in range(100):
        assert card.scroll_render_key() == expected
    assert allocations == [], "checking unchanged cached pixels must not build paint colors"


def test_scroll_cache_tracks_live_accent_and_opacity(qtbot, monkeypatch):
    card = ModCard(Mod())
    qtbot.addWidget(card)
    card.setProperty("_appearanceTheme", "light")
    card.setProperty("_backgroundSurface", True)
    card.set_selected(True)
    key = card.scroll_render_key()
    monkeypatch.setitem(theme._palette("light"), "accent.subtle", "#13579b")
    accent_key = card.scroll_render_key()
    assert accent_key != key
    assert card._chrome_style()[0].name() == "#13579b"
    monkeypatch.setitem(theme.ACRYLIC, "card", 0.123)
    assert card.scroll_render_key() != accent_key
    assert abs(card._chrome_style()[0].alphaF() - 0.123) < 0.001


@pytest.mark.parametrize("width,height", [(80, 100), (160, 200), (320, 400)])
def test_chrome_state_keeps_content_geometry_stable_at_supported_scales(qtbot, qapp, width, height):
    card = ModCard(
        Mod(mod_id="one", name="Long name for elision", info=ModInfo(tags=["First", "Second"])),
        card_width=width,
        card_height=height,
    )
    qtbot.addWidget(card)
    card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    card.show()
    qapp.processEvents()
    children = [card._preview_container, card._info_widget]
    before = [child.geometry() for child in children]
    assert all(card.rect().contains(rect) for rect in before)
    card._mod.enable()
    card.set_selected(True)
    card.set_cut_state(True)
    qapp.processEvents()
    assert [child.geometry() for child in children] == before
