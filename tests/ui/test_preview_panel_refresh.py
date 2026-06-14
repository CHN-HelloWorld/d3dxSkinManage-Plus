import threading
from dataclasses import replace

import pytest

from src.ui.pages.overview import OverviewPage
from src.ui.widgets.key_capture import KeyCaptureField
from tests.core.test_library_reconcile import mm as mm


@pytest.fixture
def selected_page(qtbot, mm, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "mod.ini").write_text("[KeySwap]\nkey = x\nback = y\n", encoding="utf-8")
    node = mm.add_character("Category")
    mod = mm.import_mod(source_path=str(source), name="Original", node_id=node.id)
    assert mod is not None
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(mm)
    page.resize(1200, 850)
    page.set_characters(mm.get_characters_for_ui("game_a"), refresh=False)
    page._current_parent_id = node.id
    page.set_current_mods([mod])
    page._selected_ids = {mod.mod_id}
    page.show()
    page._show_selection_panel()
    page._detail_tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: len(page._panel_keys_container.findChildren(KeyCaptureField)) == 2)
    qtbot.wait(20)
    yield page, mod
    page.close()


def test_key_refresh_keeps_existing_layout_until_result(selected_page, mm, qtbot, monkeypatch):
    page, mod = selected_page
    fields = page._panel_keys_container.findChildren(KeyCaptureField)
    keys_height = page._panel_keys_container.height()
    entered, release = threading.Event(), threading.Event()
    original = mm.parse_mod_key_bindings

    def held(mod):
        entered.set()
        assert release.wait(3)
        return original(mod)

    monkeypatch.setattr(mm, "parse_mod_key_bindings", held)
    try:
        page.refresh_keys_if_selected(mod.mod_id)
        qtbot.waitUntil(entered.is_set)
        qtbot.wait(20)
        assert page._panel_keys_container.isVisible()
        assert page._panel_keys_container.height() == keys_height
        assert page._panel_keys_container.findChildren(KeyCaptureField) == fields
    finally:
        release.set()
    qtbot.waitUntil(lambda: page._panel_keys_container.isEnabled())
    qtbot.wait(30)
    assert page._panel_keys_container.height() == keys_height
    # 内容未变的 INI 通知也应保留已有控件。
    assert page._panel_keys_container.findChildren(KeyCaptureField) == fields


def test_metadata_edit_updates_details_without_reloading_keys(selected_page, qtbot, monkeypatch):
    page, mod = selected_page
    fields = page._panel_keys_container.findChildren(KeyCaptureField)

    def unexpected():
        pytest.fail("metadata-only edit reloaded INI bindings")

    monkeypatch.setattr(page, "_build_keys_panel", unexpected)
    edited = replace(mod, name="Renamed", info=replace(mod.info, author="Author", description="Description"))
    page.update_mod_in_view(edited)
    assert page._panel_name_label.text() == "Renamed"
    assert page._panel_author_label.text() == "Author"
    assert page._panel_desc_label.text() == "Description"
    assert page._panel_keys_container.findChildren(KeyCaptureField) == fields
    assert page._detail_tabs.currentIndex() == 1


def test_source_link_follows_theme_without_reselecting_mod(selected_page, qapp):
    from PySide6.QtGui import QColor, QTextDocument

    from src.ui import theme

    page, mod = selected_page
    linked = replace(mod, info=replace(mod.info, url="https://gamebanana.com/mods/12345"))
    page.update_mod_in_view(linked)
    page._detail_tabs.setCurrentIndex(0)
    label = page._panel_source_label
    document = QTextDocument()
    document.setHtml(label.text())
    source_text = document.toPlainText()
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()
    try:
        for mode, link_color in [("dark", "#60cdff"), ("light", "#0067c0"), ("dark", "#60cdff")]:
            theme.set_theme(mode)
            theme.set_background_active(False)
            qapp.setStyleSheet(theme.render_qss(mode))
            page.retheme()
            qapp.processEvents()
            expected = QColor(link_color)
            document.setHtml(label.text())
            assert document.toPlainText() == source_text
            assert f'href="{linked.info.url}"' in label.text()
            assert label.openExternalLinks()
            image = label.grab().toImage()
            assert any(image.pixelColor(x, y) == expected for y in range(image.height()) for x in range(image.width()))
    finally:
        mode, background, sheet = previous
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(sheet)


def test_unchanged_watcher_delta_does_not_refresh_selected_panel(selected_page, monkeypatch):
    page, mod = selected_page

    def unexpected(*args, **kwargs):
        pytest.fail("unchanged watcher data refreshed the view")

    with monkeypatch.context() as patch:
        patch.setattr(page, "_refresh_panel_mod_status", unexpected)
        patch.setattr(page, "_vs_invalidate_prefetch", unexpected)
        assert page.apply_reconcile_delta([replace(mod)], ())


def test_mutating_existing_mod_still_updates_its_presented_name(selected_page):
    page, mod = selected_page
    mod.name = "Edited in place"
    page.update_mod_in_view(mod)
    assert page._panel_name_label.text() == "Edited in place"
    assert page._display_items[page._display_idx_map[mod.mod_id]]["name"] == "Edited in place"


def test_same_path_update_reloads_changed_ini_content(selected_page, mm, qtbot):
    import zipfile

    from src.models.mod import Mod

    page, mod = selected_page
    with zipfile.ZipFile(mod.path, "w") as archive:
        archive.writestr("mod.ini", "[KeyNew]\nkey = z\n")
    # 现有覆盖文件为有效来源，模拟更新事务同时替换这些文件。
    from src.core.mod_package import write_override

    assert write_override(mod.path, "mod.ini", "[KeyNew]\nkey = z\n")
    changed = replace(mod, content_hash=Mod.compute_sha1(mod.path))
    page.update_mod_in_view(changed)
    qtbot.waitUntil(
        lambda: [field.token() for field in page._panel_keys_container.findChildren(KeyCaptureField)] == ["z"]
    )


def test_rating_edit_reapplies_hidden_preview_policy(selected_page, qtbot, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    from src.models.mod import AgeRating

    page, mod = selected_page
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.red)
    path = str(tmp_path / "preview.png")
    image.save(path)
    shown = replace(mod, preview_images=[path])
    page.update_mod_in_view(shown)
    page.set_adult_mode("hide")
    restricted = replace(shown, info=replace(shown.info, age_rating=AgeRating.X))
    page.update_mod_in_view(restricted)
    assert page._panel_preview._image_label.text() == "18+ 内容已隐藏"
    assert page._vs_card_cache[mod.mod_id]._preview_label.text() == "已隐藏"


def test_edited_key_keeps_populated_tab_during_async_replacement(selected_page, mm, qtbot):
    from PySide6.QtCore import QEvent, QObject

    page, mod = selected_page
    frames = []

    class PaintProbe(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint:
                frames.append(
                    tuple(
                        sorted(
                            field.token()
                            for field in page._panel_keys_container.findChildren(KeyCaptureField)
                            if field.isVisible()
                        )
                    )
                )
            return False

    probe = PaintProbe(page)
    page._panel_keys_scroll.viewport().installEventFilter(probe)
    binding = mm.parse_mod_key_bindings(mod)[0]
    page._on_key_changed(binding, "key", "ctrl alt k")
    qtbot.waitUntil(
        lambda: any(field.token() == "ctrl alt k" for field in page._panel_keys_container.findChildren(KeyCaptureField))
    )
    page._panel_keys_scroll.viewport().update()
    qtbot.wait(30)
    assert frames and set(frames) <= {("x", "y"), ("ctrl alt k", "y")}


def test_new_tag_uses_shared_identity_on_existing_card(selected_page, mm, qtbot):
    from src.ui.widgets.tag_chip import TagChip

    page, mod = selected_page
    tagged = replace(mod, info=replace(mod.info, tags=["Shared"]))
    assert mm.update_mod(tagged)
    page.update_mod_in_view(tagged)
    tag = mm.get_tag_service().list_tags()[0]
    card = page._vs_card_cache[mod.mod_id]
    chips = card._tag_row.findChildren(TagChip)
    assert [chip.tag.id for chip in chips] == [tag.id]
    draft = mm.get_tag_service().begin_edit(mm.get_mod(mod.mod_id))
    draft.rename(tag.id, "Renamed tag")
    result = mm.commit_tag_edit(draft, {})
    page.refresh_tags(refresh_view=False)
    for saved in result.mods:
        page.update_mod_in_view(saved)
    chips = card._tag_row.findChildren(TagChip)
    assert [(chip.tag.id, chip.tag.name) for chip in chips] == [(tag.id, "Renamed tag")]
    draft = mm.get_tag_service().begin_edit(mm.get_mod(mod.mod_id))
    draft.set_color(tag.id, "mint")
    result = mm.commit_tag_edit(draft, {})
    page.refresh_tags(refresh_view=False)
    for saved in result.mods:
        page.update_mod_in_view(saved)
    assert card._tag_row.findChildren(TagChip)[0].tag.color == "mint"


def test_content_update_refreshes_same_path_preview_pixels(selected_page, qtbot, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    page, mod = selected_page
    path = str(tmp_path / "updated.png")
    red, blue = QPixmap(1500, 1500), QPixmap(1500, 1500)
    red.fill(Qt.GlobalColor.red)
    blue.fill(Qt.GlobalColor.blue)
    red.save(path)
    shown = replace(mod, preview_images=[path])
    page.update_mod_in_view(shown)
    page._panel_preview._cached_pixmap = red
    page._panel_preview._cached_path = path
    blue.save(path)
    page.update_mod_in_view(replace(shown, content_hash="new-content"))
    qtbot.waitUntil(
        lambda: (
            page._panel_preview._cached_pixmap is not None
            and page._panel_preview._cached_pixmap.toImage().pixelColor(0, 0).blue() > 200
        )
    )
