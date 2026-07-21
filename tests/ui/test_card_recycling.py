from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QMenu, QWidget

from src.models.mod import Mod, ModInfo
from src.ui.pages.gallery import GalleryItem, GalleryPage
from src.ui.pages.overview import CharacterCard, OverviewPage
from src.ui.widgets.gb_mod_card import _FillCropLabel
from src.ui.widgets.mod_card import ModCard
from src.ui.widgets.spinner import ShimmerLabel
from tests.qt_dialogs import interact


def _prepare(qtbot, kind, request):
    page = OverviewPage() if kind == "overview" else GalleryPage()
    qtbot.addWidget(page)
    # qtbot 只保存控件弱引用，需保持线程所有者存活至关闭完成。
    request.addfinalizer(page.close)
    page.resize(900, 600)
    page.show()
    qtbot.wait(20)
    mods = [Mod(mod_id=f"mod-{i:04}", name=f"Mod {i:04}") for i in range(800)]
    if kind == "overview":
        page.set_current_mods(mods, mods)
    else:
        page.set_mods(mods)
    page._fresh_population = False
    page._vs_population_timer.stop()
    _scroll(page, 20)
    return page


def _scroll(page, row):
    page._scroll_area.verticalScrollBar().setValue(row * page._vs_row_h)
    page._vs_update_visible()
    # 完成控件分批创建，暂不分发后台预览回调。
    for _ in range(100):
        if not getattr(page, "_vs_population_pending", False):
            break
        page._vs_update_visible()
    assert not getattr(page, "_vs_population_pending", False)
    page._vs_scroll_timer.stop()


@pytest.mark.parametrize("kind", ["overview", "gallery"])
def test_distant_scroll_reuses_existing_card_widgets(qtbot, temp_data_dir, kind, request):
    page = _prepare(qtbot, kind, request)
    original = set(page._vs_card_cache.values())
    original_ids = set(page._vs_card_cache)

    _scroll(page, 60)

    assert set(page._vs_card_cache).isdisjoint(original_ids)
    assert set(page._vs_card_cache.values()) <= original, "scrolling must rebind existing card widgets"
    for mod_id, card in page._vs_card_cache.items():
        assert card._mod.mod_id == mod_id
        assert card.parentWidget() is page._scroll_content

    _scroll(page, 20)
    assert set(page._vs_card_cache) == original_ids
    assert set(page._vs_card_cache.values()) <= original


@pytest.mark.parametrize("kind", ["overview", "gallery"])
@pytest.mark.parametrize("size", [(900, 1400), (1400, 600)])
def test_resize_fills_new_space_and_reuses_widgets(qtbot, temp_data_dir, kind, size, request):
    page = _prepare(qtbot, kind, request)
    original = set(page._vs_card_cache.values())

    page.resize(*size)

    def viewport_is_filled():
        top = page._scroll_area.verticalScrollBar().value()
        bottom = top + page._scroll_area.viewport().height()
        records = (
            [item["id"] for item in page._display_items] if kind == "overview" else [mod.mod_id for mod in page._mods]
        )
        expected = {
            record_id
            for i, record_id in enumerate(records)
            if (
                6 + (i // page._vs_cols) * page._vs_row_h < bottom
                and 6 + (i // page._vs_cols + 1) * page._vs_row_h > top
            )
        }
        return expected <= page._vs_card_cache.keys()

    qtbot.waitUntil(viewport_is_filled, timeout=1500)
    assert original <= set(page._vs_card_cache.values()), "resizing must retain reusable widgets"


def test_reused_mod_card_context_menu_uses_current_record(qtbot, temp_data_dir, monkeypatch, request):
    page = _prepare(qtbot, "overview", request)
    original = set(page._vs_card_cache.values())
    old_id = next(iter(page._vs_card_cache))
    page._selected_ids = {old_id}
    page._update_card_selection()
    calls = []
    monkeypatch.setattr(page, "_show_mod_card_context_menu", lambda card, mid, pos: calls.append(mid))

    _scroll(page, 60)
    reused = [(mid, card) for mid, card in page._vs_card_cache.items() if card in original]

    assert reused, "the same widget must serve another record"
    for mid, card in reused:
        assert not card._selected
        card.customContextMenuRequested.emit(QPoint(2, 2))
        assert calls[-1] == mid


@pytest.mark.parametrize("rebind_stage", ["selection", "menu"])
def test_category_menu_keeps_the_opening_record(qtbot, monkeypatch, rebind_stage):
    from src.ui import plugin_menu
    from src.utils.i18n import t

    card = CharacterCard({"id": "first", "name": "First"})
    qtbot.addWidget(card)
    card.show()
    deleted = []
    card.delete_requested.connect(deleted.append)
    if rebind_stage == "selection":
        card.context_select_requested.connect(lambda _: card.set_category({"id": "second"}))
    plugin_ids = []
    monkeypatch.setattr(plugin_menu, "menu_actions", lambda _, cid: plugin_ids.append(cid) or [])

    def choose_delete(menu):
        if rebind_stage == "menu":
            card.set_category({"id": "second"})
        return next(action for action in menu.actions() if action.text() == t("删除"))

    with interact(QMenu, choose_delete):
        card._show_context_menu(QPoint(3, 3))

    assert deleted == ["first"]
    assert plugin_ids == ["first"]


def test_edit_refresh_does_not_overwrite_a_card_recycled_by_panel_reflow(
    qtbot,
    temp_data_dir,
    monkeypatch,
    request,
):
    page = _prepare(qtbot, "overview", request)
    mod_id, card = next(iter(page._vs_card_cache.items()))
    page._selected_mod = card._mod
    # 展开预览面板可能同步改变可见绑定。
    monkeypatch.setattr(page, "_show_mod_panel", lambda mod: _scroll(page, 60))

    page.update_mod_in_view(Mod(mod_id=mod_id, name="Edited"))

    assert all(mid == item.mod_id for mid, item in page._vs_card_cache.items())


def test_category_menu_survives_owner_deleted_during_exec(qapp):
    card = CharacterCard({"id": "first", "name": "First"})

    def delete_owner(menu):
        shiboken6.delete(card)
        assert not shiboken6.isValid(menu)
        return None

    try:
        with interact(QMenu, delete_owner):
            card._show_context_menu(QPoint(3, 3))
    finally:
        if shiboken6.isValid(card):
            shiboken6.delete(card)


def test_native_menu_interaction_reraises_callback_failure_after_closing(qapp):
    menu = QMenu()
    menu.addAction("Example")

    def fail_inside_menu(_menu):
        raise AssertionError("callback failure")

    try:
        with pytest.raises(AssertionError, match="callback failure"):
            with interact(QMenu, fail_inside_menu):
                menu.exec(QPoint(3, 3))
        assert not menu.isVisible()
    finally:
        shiboken6.delete(menu)


def test_native_interaction_timeout_closes_unexpected_modal(qapp):
    dialog = QDialog()
    try:
        with pytest.raises(AssertionError, match="did not finish"):
            with interact(QMenu, lambda menu: None, timeout=100):
                dialog.exec()
        assert not dialog.isVisible()
    finally:
        shiboken6.delete(dialog)


def test_reused_gallery_card_click_uses_current_record(qtbot, temp_data_dir, request):
    page = _prepare(qtbot, "gallery", request)
    original = set(page._vs_card_cache.values())
    selected = []
    page.mod_selected.connect(selected.append)
    _scroll(page, 60)
    current_id, card = next(iter(page._vs_card_cache.items()))

    assert card in original
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert selected == [current_id]


def test_gallery_loaded_preview_remains_on_label(qtbot):
    item = GalleryItem(Mod(mod_id="picture", preview_images=["preview.png"]), defer_pixmap=True)
    qtbot.addWidget(item)
    picture = QPixmap(32, 24)
    picture.fill(QColor("red"))

    item.set_pixmap(picture)

    assert not item._image.pixmap().isNull()
    assert item._image.pixmap().toImage().pixelColor(16, 12) == QColor("red")


@pytest.mark.parametrize("tags", [[], ["Shared tag"]])
def test_rebinding_same_tags_preserves_tag_widgets(qtbot, tags):
    card = ModCard(Mod(mod_id="first", info=ModInfo(tags=tags)))
    qtbot.addWidget(card)
    original = set(card._tag_row.findChildren(QWidget))

    card.set_mod_deferred(Mod(mod_id="second", info=ModInfo(tags=tags)))

    assert set(card._tag_row.findChildren(QWidget)) == original


@pytest.mark.parametrize("kind", ["mod", "category", "gallery"])
def test_rebinding_without_preview_stops_hidden_shimmer(qtbot, kind):
    first = Mod(mod_id="first", preview_images=["pending.png"])
    if kind == "mod":
        card = ModCard()
        card.set_mod_deferred(first)
        image = card._preview_label

        def rebind():
            card.set_mod_deferred(Mod(mod_id="second"))
    elif kind == "category":
        card = CharacterCard({"id": "first", "thumbnail": "pending.png"}, defer_pixmap=True)
        image = card._thumb

        def rebind():
            card.set_category({"id": "second"})
    else:
        card = GalleryItem(first, defer_pixmap=True)
        image = card._image

        def rebind():
            card.set_mod(Mod(mod_id="second"))

    qtbot.addWidget(card)
    card.show()
    card.hide()

    rebind()
    card.show()

    assert image._shimmer.isHidden(), "old loading state must not cover a rebound placeholder"


@pytest.mark.parametrize("online", [False, True])
def test_new_thumbnail_loading_survives_old_crossfade(qtbot, monkeypatch, online):
    from src.ui import anim

    monkeypatch.setattr(anim, "_enabled", True)
    label = _FillCropLabel(QSize(120, 100)) if online else ShimmerLabel()
    qtbot.addWidget(label)
    label.resize(120, 100)
    label.show()
    label.start_loading()
    pixmap = QPixmap(120, 100)
    pixmap.fill(QColor("red"))
    label.setPixmap(pixmap)

    label.start_loading()
    qtbot.wait(anim.DURATION["fast"] + 50)

    assert label._shimmer.isVisible(), "the previous fade must not hide the new loading state"


def test_category_scroll_does_not_decode_thumbnails_on_gui_thread(qtbot, temp_data_dir, tmp_path, monkeypatch):
    from src.ui.pages import overview as overview_module

    path = tmp_path / "category.png"
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(path))
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.show()
    qtbot.wait(20)

    def forbid_gui_decode(*args):
        raise AssertionError("category thumbnails must use the background image worker")

    monkeypatch.setattr(overview_module, "load_pixmap_file", forbid_gui_decode)
    page.set_characters([{"id": f"cat-{i:04}", "name": f"Category {i:04}", "thumbnail": str(path)} for i in range(800)])
    page._fresh_population = False
    page._vs_population_timer.stop()
    _scroll(page, 20)
    original = set(page._vs_card_cache.values())
    _scroll(page, 60)

    assert set(page._vs_card_cache.values()) <= original
    assert all(isinstance(card, CharacterCard) for card in page._vs_card_cache.values())
