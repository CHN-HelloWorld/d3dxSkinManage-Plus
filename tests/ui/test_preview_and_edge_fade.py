from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QScrollArea, QWidget

from src.ui.widgets.breadcrumb_bar import BreadcrumbBar
from src.ui.widgets.card_pool import CardPool
from src.ui.widgets.preview import PreviewWidget
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade


def _image(path, color: str) -> None:
    image = QImage(24, 24, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def test_preview_switches_between_duplicate_basenames(qtbot, tmp_path):
    first = tmp_path / "a" / "preview.png"
    second = tmp_path / "b" / "preview.png"
    first.parent.mkdir()
    second.parent.mkdir()
    _image(first, "#ff0000")
    _image(second, "#0000ff")

    widget = PreviewWidget()
    qtbot.addWidget(widget)
    widget.resize(360, 300)
    widget.show()
    widget.set_images([str(first), str(second)])
    first_color = widget._cached_pixmap.toImage().pixelColor(0, 0)

    widget._show_next()
    second_color = widget._cached_pixmap.toImage().pixelColor(0, 0)

    assert first_color != second_color
    assert second_color.blue() > second_color.red()


def test_delete_all_only_appears_for_multiple_images_and_tracks_mode_changes(qtbot, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _image(first, "#ff0000")
    _image(second, "#0000ff")
    widget = PreviewWidget()
    qtbot.addWidget(widget)
    widget.show()

    widget.set_single_image_mode(True)
    widget.set_images([str(first), str(second)])
    assert not widget._delete_all_btn.isVisible()

    widget.set_single_image_mode(False)
    assert widget._delete_all_btn.isVisible()

    widget.set_images([str(first)])
    assert widget._delete_btn.isVisible()
    assert not widget._delete_all_btn.isVisible()

    widget.set_images([])
    assert not widget._delete_btn.isVisible()
    assert not widget._delete_all_btn.isVisible()


def test_scroll_edge_fade_tracks_scroll_boundaries(qtbot):
    area = QScrollArea()
    area.resize(320, 220)
    content = QWidget()
    content.setMinimumHeight(900)
    area.setWidget(content)
    fade = ScrollEdgeFade(area)
    qtbot.addWidget(area)
    area.show()
    qtbot.wait(10)

    assert not fade.top_active
    assert fade.bottom_active
    area.verticalScrollBar().setValue(area.verticalScrollBar().maximum() // 2)
    assert fade.top_active and fade.bottom_active


def test_scroll_edge_fade_makes_card_pixels_transparent(qtbot):
    area = QScrollArea()
    area.resize(220, 220)
    area.viewport().setStyleSheet("background: #00aa00;")
    content = QWidget()
    content.setObjectName("fadeContent")
    content.setStyleSheet("#fadeContent { background: transparent; }")
    content.resize(200, 800)
    card = QWidget(content)
    card.setGeometry(10, 80, 170, 170)
    card.setStyleSheet("background: #ff0000;")
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card], fade_height=60)
    qtbot.addWidget(area)
    area.show()
    area.verticalScrollBar().setValue(100)
    fade.refresh()
    qtbot.wait(20)

    image = area.viewport().grab().toImage()
    edge = image.pixelColor(30, 1)
    inner = image.pixelColor(30, 58)

    assert edge.green() > edge.red(), "边缘应透出 viewport/Acrylic 背景，而不是覆盖阴影色"
    assert inner.red() > inner.green(), "离开渐隐带后卡片应恢复为不透明"


def test_breadcrumb_overflow_menu_is_prepared_for_acrylic(qtbot):
    bar = BreadcrumbBar()
    qtbot.addWidget(bar)
    menu = bar._overflow_menu

    assert menu.objectName() == "acrylicMenu"
    assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert "rgba" in menu.styleSheet()


def _fade_scene(qtbot, cache=False):
    area = QScrollArea()
    area.resize(220, 220)
    area.viewport().setStyleSheet("background: #00aa00;")
    content = QWidget()
    content.setObjectName("fadeContent")
    content.setStyleSheet("#fadeContent { background: transparent; }")
    content.resize(200, 1000)
    card = QWidget(content)
    card.setGeometry(10, 0, 170, 300)
    card.setStyleSheet("background: #ff0000;")
    if cache:
        card.scroll_render_key = lambda: card.styleSheet()
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card], fade_height=60)
    qtbot.addWidget(area)
    area.show()
    qtbot.wait(10)
    return area, card, fade


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("cache", [False, True])
def test_edge_alpha_at_scroll_limits_and_after_recycling(qtbot, dpr, cache):
    area, card, fade = _fade_scene(qtbot, cache)
    pool = CardPool()
    bar = area.verticalScrollBar()
    for position, top_fades, bottom_fades in (
        (0, False, True),
        (300, True, True),
        (bar.maximum(), True, False),
        (300, True, True),
    ):
        pool.release(card)
        assert pool.take(QWidget) is card
        card.move(10, position)
        bar.setValue(position)
        card.show()
        fade.refresh()
        viewport = area.viewport()
        pixmap = QPixmap(viewport.width() * dpr, viewport.height() * dpr)
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        viewport.render(pixmap)
        image = pixmap.toImage()
        top = image.pixelColor(30 * dpr, dpr)
        middle = image.pixelColor(30 * dpr, 100 * dpr)
        bottom = image.pixelColor(30 * dpr, (viewport.height() - 2) * dpr)
        assert (top.green() > top.red()) == top_fades
        assert middle.red() > middle.green()
        assert (bottom.green() > bottom.red()) == bottom_fades


def test_recycling_cancels_old_intro_without_losing_edge_mask(qtbot):
    area, card, fade = _fade_scene(qtbot)
    opacity = QGraphicsOpacityEffect(card)
    opacity.setOpacity(0.0)
    card.setGraphicsEffect(opacity)
    pool = CardPool()
    pool.release(card)
    assert pool.take(QWidget) is card
    card.move(10, 300)
    area.verticalScrollBar().setValue(300)
    card.show()
    fade.refresh()
    image = area.viewport().grab().toImage()
    assert image.pixelColor(30, 1).green() > image.pixelColor(30, 1).red()
    assert image.pixelColor(30, 100).red() > image.pixelColor(30, 100).green()


class _PaintCountingCard(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.color = QColor("red")
        self.paints = 0
        self.live = False

    def scroll_render_key(self):
        return None if self.live else self.color.name()

    def paintEvent(self, event):
        self.paints += 1
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)
        painter.end()


def test_cached_reveal_changes_alpha_without_repainting_static_card_children(qtbot):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(240, 240)
    area.viewport().setStyleSheet("background: #00aa00;")
    content = QWidget()
    content.resize(220, 800)
    card = _PaintCountingCard(content)
    card.setGeometry(10, 80, 170, 100)
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card])
    area.show()
    qtbot.wait(20)
    fade.refresh()
    effect = card.graphicsEffect()
    area.viewport().grab()
    painted = card.paints
    effect.set_reveal_opacity(0.3)
    translucent = area.viewport().grab().toImage().pixelColor(30, 100)
    effect.set_reveal_opacity(1)
    opaque = area.viewport().grab().toImage().pixelColor(30, 100)
    assert translucent.green() > opaque.green()
    assert opaque.red() > translucent.red()
    assert card.paints == painted
    pool = CardPool()
    effect.set_reveal_opacity(0.2)
    pool.release(card)
    assert effect.reveal_opacity == 1, "recycling must not carry an unfinished intro"


def test_static_card_reuses_pixels_while_edge_gradient_keeps_moving(qtbot):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(220, 220)
    area.viewport().setStyleSheet("background: #00aa00;")
    content = QWidget()
    content.resize(200, 1500)
    card = _PaintCountingCard(content)
    card.setGeometry(10, 300, 170, 400)
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card], fade_height=60)
    area.show()
    bar = area.verticalScrollBar()
    bar.setValue(310)
    qtbot.wait(10)
    area.viewport().grab()
    painted = card.paints

    for y in (320, 330, 340):
        bar.setValue(y)
        image = area.viewport().grab().toImage()
        assert image.pixelColor(30, 1).green() > image.pixelColor(30, 1).red()
        assert image.pixelColor(30, 100).red() > image.pixelColor(30, 100).green()
    assert card.paints == painted, "unchanged card children must not repaint on each scroll step"

    card.color = QColor("blue")
    card.update()
    image = area.viewport().grab().toImage()
    assert image.pixelColor(30, 100).blue() > image.pixelColor(30, 100).red()
    assert card.paints > painted

    card.live = True
    card.update()
    area.viewport().grab()
    painted = card.paints
    bar.setValue(350)
    area.viewport().grab()
    assert card.paints > painted, "animated content must remain live"
    assert fade.top_active


@pytest.mark.parametrize("kind", ["mod", "category", "gallery"])
def test_cached_local_card_displays_replaced_preview(qtbot, kind):
    from src.models.mod import Mod
    from src.ui.pages.gallery import GalleryItem
    from src.ui.pages.overview import CharacterCard
    from src.ui.widgets.mod_card import ModCard

    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(400, 320)
    content = QWidget()
    content.resize(380, 1800)
    if kind == "mod":
        card = ModCard(Mod(mod_id="picture"), parent=content)
        set_image = card.set_preview_pixmap
    elif kind == "category":
        card = CharacterCard({"id": "picture"}, parent=content)
        set_image = card.set_preview_pixmap
    else:
        card = GalleryItem(Mod(mod_id="picture"), parent=content)
        set_image = card.set_pixmap
    card.move(12, 400)
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card])
    area.show()
    area.verticalScrollBar().setValue(350)
    qtbot.wait(10)
    for color in ("red", "blue", "red"):
        preview = QPixmap(400, 400)
        preview.fill(QColor(color))
        set_image(preview)
        image = area.viewport().grab().toImage()
        assert image.pixelColor(60, 100) == QColor(color)
    assert fade.top_active
