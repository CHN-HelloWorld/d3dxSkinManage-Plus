import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QScrollArea, QWidget

from src.models.mod import Mod
from src.ui.pages import overview
from src.ui.widgets import mod_card
from src.ui.widgets.card_pool import CardPool
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade


class PaintCard(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.color = QColor("red")
        self.paints = 0

    def scroll_render_key(self):
        return self.color.name()

    def paintEvent(self, event):
        self.paints += 1
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)


def viewport_image(area, dpr):
    pixmap = QPixmap(area.viewport().size() * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    area.viewport().render(pixmap)
    return pixmap.toImage()


@pytest.mark.parametrize("dpr", [1, 2])
def test_resize_reuses_card_surface_and_preserves_current_edge_mask(qtbot, dpr):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(350, 400)
    area.viewport().setStyleSheet("background: #00aa00;")
    content = QWidget()
    content.resize(320, 1000)
    area.setWidget(content)
    card = PaintCard(content)
    card.setGeometry(10, 100, 150, 200)
    fade = ScrollEdgeFade(area, lambda: [card])
    area.show()
    qtbot.wait(20)
    area.viewport().grab()
    initial_paints = card.paints
    # 布局请求可能在绘制后使渲染键失效，开始缩放时仍应复用现有像素。
    QCoreApplication.sendEvent(card, QEvent(QEvent.Type.LayoutRequest))
    fade.set_resizing(True)
    for width in (160, 175, 190):
        card.resize(width, 250)
        area.verticalScrollBar().setValue(110)
        qtbot.wait(10)
        image = viewport_image(area, dpr)
        assert image.pixelColor((width - 10) * dpr, 100 * dpr).red() > 200
        edge = image.pixelColor(30 * dpr, dpr)
        assert edge.green() > edge.red()
    assert card.paints == initial_paints
    card.color = QColor("blue")
    fade.set_resizing(False)
    image = viewport_image(area, dpr)
    assert image.pixelColor(30 * dpr, 100 * dpr).blue() > 200
    assert card.paints > initial_paints


def test_recycled_card_does_not_keep_the_previous_resize_snapshot(qtbot):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(350, 400)
    content = QWidget()
    content.resize(320, 1000)
    area.setWidget(content)
    card = PaintCard(content)
    card.setGeometry(10, 80, 150, 200)
    fade = ScrollEdgeFade(area, lambda: [card])
    area.show()
    qtbot.wait(20)
    area.viewport().grab()
    fade.set_resizing(True)
    pool = CardPool()
    pool.release(card)
    assert pool.take(PaintCard) is card
    card.color = QColor("blue")
    card.show()
    fade.refresh()
    image = area.viewport().grab().toImage()
    assert image.pixelColor(30, 100).blue() > 200


@pytest.mark.parametrize("restart", [False, True])
def test_finishing_zoom_spreads_repaints_and_a_new_drag_cancels_the_old_finish(qtbot, restart):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(380, 430)
    content = QWidget()
    content.resize(360, 400)
    area.setWidget(content)
    cards = [PaintCard(content) for _ in range(30)]
    for i, card in enumerate(cards):
        card.setGeometry(10 + i % 5 * 65, 10 + i // 5 * 60, 50, 50)
    fade = ScrollEdgeFade(area, lambda: cards)
    area.show()
    qtbot.wait(20)
    area.viewport().grab()
    painted = sum(card.paints for card in cards)
    fade.set_resizing(True)
    for card in cards:
        card.color = QColor("blue")
        card.resize(55, 55)
    area.viewport().grab()
    fade.set_resizing(False, deferred=True)
    fade._resize_finish_timer.stop()  # 显式驱动帧更新，避免断言依赖调度时机。
    fade.refresh()
    area.viewport().grab()
    assert sum(card.paints for card in cards) == painted
    fade._finish_resize_batch()
    area.viewport().grab()
    repainted = sum(card.paints for card in cards) - painted
    assert 0 < repainted < len(cards), "release must not repaint the entire viewport in one frame"
    if restart:
        fade.set_resizing(True)
        before = sum(card.paints for card in cards)
        for card in cards:
            card.color = QColor("green")
            card.resize(57, 57)
        fade._finish_resize_batch()
        area.viewport().grab()
        assert sum(card.paints for card in cards) == before
        fade.set_resizing(False)
    else:
        while fade._resize_finish_queue:
            fade._finish_resize_batch()
            area.viewport().grab()
    final = area.viewport().grab().toImage()
    expected = QColor("green" if restart else "blue")
    assert all(final.pixelColor(card.x() + 20, card.y() + 20) == expected for card in cards)


@pytest.fixture
def scale_page(qtbot, temp_data_dir):
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 750)
    page.show()
    image = QPixmap(80, 120)
    image.fill(QColor("red"))
    path = temp_data_dir / "scale-preview.png"
    assert image.save(str(path))
    mod = Mod(mod_id="scale", name="A long name for the card scaling test", preview_images=[str(path)])
    page.set_current_mods([mod], [mod])
    qtbot.waitUntil(lambda: "scale" in page._vs_card_cache and page._vs_card_cache["scale"]._cached_pixmap is not None)
    page._scroll_area.viewport().grab()
    yield page
    page.close()


def test_drag_defers_expensive_preview_composition_until_release(scale_page, qtbot, monkeypatch):
    page = scale_page
    compositions = []
    compose = mod_card.fill_blur_rounded

    def tracked(*args, **kwargs):
        compositions.append(True)
        return compose(*args, **kwargs)

    monkeypatch.setattr(mod_card, "fill_blur_rounded", tracked)
    slider = page._card_size_slider
    slider.setSliderDown(True)
    slider.setValue(150)
    qtbot.wait(250)
    assert not compositions, "holding the slider must not trigger main-thread blur/crop work"
    slider.setSliderDown(False)
    qtbot.waitUntil(lambda: page._vs_card_cache["scale"]._preview_resolved, timeout=3000)
    qtbot.wait(200)
    card = page._vs_card_cache["scale"]
    assert card.width() == 240
    assert card._preview_label.pixmap().width() == round((card.width() - 8) * card.devicePixelRatioF())
    assert card._preview_label.pixmap().height() == round(card._preview_height * card.devicePixelRatioF())
    assert not compositions, "final high-quality previews must use the background queue"


def test_drag_displays_size_changes_without_repainting_preview_widgets(scale_page, qtbot):
    page = scale_page
    card = page._vs_card_cache["scale"]
    paints = []

    class Counter(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint:
                paints.append(True)
            return False

    counter = Counter(card)
    card._preview_label.installEventFilter(counter)
    slider = page._card_size_slider
    slider.setSliderDown(True)
    for value in (110, 120, 130, 140, 150):
        slider.setValue(value)
        qtbot.wait(25)
        page._scroll_area.viewport().grab()
    assert card.width() == 240
    assert len(paints) <= 1, "drag frames must reuse the existing card surface"
    slider.setSliderDown(False)


def test_directory_change_during_drag_resumes_loading_new_previews(scale_page, qtbot, temp_data_dir):
    page = scale_page
    page._card_size_slider.setSliderDown(True)
    page._card_size_slider.setValue(130)
    qtbot.wait(30)
    image = QPixmap(80, 120)
    image.fill(QColor("blue"))
    path = temp_data_dir / "next-preview.png"
    assert image.save(str(path))
    mod = Mod(mod_id="next", name="Next directory", preview_images=[str(path)])
    page.set_current_mods([mod], [mod])
    qtbot.waitUntil(
        lambda: "next" in page._vs_card_cache and page._vs_card_cache["next"]._cached_pixmap is not None, timeout=3000
    )
    card = page._vs_card_cache["next"]
    assert card._cached_pixmap.toImage().pixelColor(20, 20) == QColor("blue")
    page._card_size_slider.setSliderDown(False)


def test_keyboard_scaling_finishes_without_a_mouse_release(scale_page, qtbot):
    page = scale_page
    slider = page._card_size_slider
    slider.setFocus()
    qtbot.keyClick(slider, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: not page._card_scale_active, timeout=1500)
    card = page._vs_card_cache["scale"]
    qtbot.waitUntil(lambda: card._preview_resolved, timeout=3000)
    assert slider.value() == 101
    assert card.width() == 161
    assert card._preview_label.pixmap().width() == 153


def test_hiding_during_drag_restores_live_cards_and_final_previews(scale_page, qtbot):
    page = scale_page
    slider = page._card_size_slider
    slider.setSliderDown(True)
    slider.setValue(150)
    qtbot.wait(25)
    page.hide()
    page.show()
    qtbot.waitUntil(lambda: page._vs_card_cache["scale"]._preview_resolved, timeout=3000)
    card = page._vs_card_cache["scale"]
    assert not page._card_scale_active
    assert not card.graphicsEffect()._resizing
    assert card._preview_label.pixmap().width() == 232
    slider.setSliderDown(False)


def test_loading_failure_applies_a_size_changed_while_waiting(scale_page, qtbot):
    page = scale_page
    page.begin_loading()
    page._card_size_slider.setValue(150)
    page.loading_failed()
    card = page._vs_card_cache["scale"]
    qtbot.waitUntil(lambda: card._preview_resolved, timeout=3000)
    assert card.width() == 240
    assert card._preview_label.pixmap().width() == 232


@pytest.mark.parametrize("kind", ["mod", "category"])
def test_zooming_out_restores_recycled_card_outer_bounds(scale_page, qtbot, kind):
    page = scale_page
    path = page._vs_card_cache["scale"]._mod.primary_preview
    mods = [Mod(mod_id=f"scale-{i:03}", name=f"Mod {i:03}", preview_images=[path]) for i in range(100)]
    if kind == "category":
        page.set_current_mods([], [])
        page.set_characters([dict(id=mod.mod_id, name=mod.name, thumbnail=path) for mod in mods])
        card_type = overview.CharacterCard
    else:
        page.set_current_mods(mods, mods)
        card_type = overview.ModCard
    qtbot.waitUntil(
        lambda: not page._vs_population_pending and all(c._preview_resolved for c in page._vs_card_cache.values()),
        timeout=5000,
    )
    slider = page._card_size_slider
    slider.setSliderDown(True)
    slider.setValue(200)
    qtbot.wait(30)
    assert page._vs_card_pool.count(card_type) > 0
    slider.setValue(100)
    qtbot.waitUntil(lambda: not page._vs_population_pending, timeout=3000)
    slider.setSliderDown(False)
    qtbot.waitUntil(lambda: not page._scroll_edge_fade._resize_finish_queue, timeout=3000)
    assert all(card.width() == 160 and card.height() == 200 for card in page._vs_card_cache.values()), (
        "a pooled card must restore its outer bounds even when its deferred child layout already has the final size"
    )


@pytest.mark.parametrize("scale", [50, 160])
def test_category_zoom_resolves_final_crop_in_background(scale_page, qtbot, monkeypatch, scale):
    page = scale_page
    path = page._vs_card_cache["scale"]._mod.primary_preview
    page.set_current_mods([], [])
    page.set_characters([dict(id="category", name="Category", thumbnail=path, mod_count=5000, enabled_count=2500)])
    qtbot.waitUntil(
        lambda: "category" in page._vs_card_cache and page._vs_card_cache["category"]._cached_pixmap is not None
    )
    compositions = []
    compose = overview.fill_crop_rounded

    def tracked(*args, **kwargs):
        compositions.append(True)
        return compose(*args, **kwargs)

    monkeypatch.setattr(overview, "fill_crop_rounded", tracked)
    slider = page._card_size_slider
    slider.setSliderDown(True)
    slider.setValue(scale)
    qtbot.wait(200)
    slider.setSliderDown(False)
    card = page._vs_card_cache["category"]
    qtbot.waitUntil(lambda: card._preview_resolved, timeout=3000)
    assert card.width() == int(160 * scale / 100)
    preview = card._thumb.pixmap().toImage()
    assert preview.width() == card.width() - 8
    assert preview.height() == card._preview_h
    assert preview.pixelColor(preview.width() // 2, preview.height() // 2).red() > 240
    assert preview.pixelColor(0, 0).alpha() == 0
    assert not compositions
