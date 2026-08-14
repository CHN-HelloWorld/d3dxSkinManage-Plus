from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QInputMethodEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from src.core.mod_index import ModIndex
from src.core.mod_manager import ModManager
from src.core.mod_package import read_sidecar, write_container_marker
from src.core.tag_service import TagService
from src.models.mod import Mod, ModInfo
from src.ui.dialogs.mod_edit import ModEditDialog
from src.ui.main_window import MainWindow
from src.utils import fs
from tests.helpers import make_mod, register_games, reset_game_service
from tests.ui.tag_helpers import reveal_widget

FIRST_MOD_ID = "11111111111141118111111111111111"
SECOND_MOD_ID = "22222222222242228222222222222222"


@pytest.fixture
def editing(qtbot, temp_data_dir):
    register_games("game-a")
    index = ModIndex("game-a")
    original = make_mod(
        index,
        mod_id=FIRST_MOD_ID,
        name="Original",
        info=ModInfo(tags=["Shared"], author="Before"),
    )
    assert index.add_mod(original)
    assert index.add_mod(make_mod(index, "Two", mod_id=SECOND_MOD_ID, info=ModInfo(tags=["Shared"])))
    dialog = ModEditDialog(original)
    qtbot.addWidget(dialog)
    yield index, TagService(index), original, dialog
    # 测试保留未保存草稿，清理时跳过退出确认。
    dialog.done(QDialog.DialogCode.Rejected)
    index.close()
    reset_game_service()


def configure(dialog, service):
    dialog.set_tag_service(service)
    dialog.set_save_handler(lambda fields, draft: service.commit_edit(draft, fields))
    dialog.show()
    QApplication.processEvents()


@pytest.mark.parametrize("action", ["edit", "remove"])
@pytest.mark.parametrize("transient_focus", [False, True])
def test_candidate_actions_survive_native_pointer_focus(qtbot, qapp, request, editing, action, transient_focus):
    from src.ui import theme

    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(theme.render_qss("dark"))
    request.addfinalizer(lambda: qapp.setStyleSheet(previous_style))
    _, service, _, dialog = editing
    configure(dialog, service)
    qtbot.waitUntil(dialog.isActiveWindow)
    picker = dialog.tag_picker
    dialog._form_scroll.ensureWidgetVisible(picker)
    qtbot.mouseClick(
        dialog.windowHandle(), Qt.MouseButton.LeftButton, pos=picker.input.mapTo(dialog, picker.input.rect().center())
    )
    qtbot.waitUntil(picker.popup.isVisible)
    qtbot.wait(80)
    tag_id = picker.draft.selected_ids[0]
    button = getattr(picker._rows[tag_id], f"{action}_button")
    # 等待布局完成并出现实际可点击位置，再发送指针事件。
    qtbot.waitUntil(lambda: dialog.childAt(button.mapTo(dialog, button.rect().center())) is button)

    class PopupEvents(QObject):
        hidden = False

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Hide:
                self.hidden = True
            return False

    popup_events = PopupEvents(picker.popup)
    picker.popup.installEventFilter(popup_events)
    position = button.mapTo(dialog, button.rect().center())
    if transient_focus:
        # 焦点通知先于鼠标分发，最终目标才决定是否收起；按下后真实失焦会取消按钮操作。
        button.setFocus()
        dialog._desc_input.setFocus()
        button.setFocus()
    qtbot.mousePress(dialog.windowHandle(), Qt.MouseButton.LeftButton, pos=position)
    QApplication.processEvents()
    assert picker.popup.isVisible(), "The panel closed before the action received its release"
    qtbot.mouseRelease(dialog.windowHandle(), Qt.MouseButton.LeftButton, pos=position)
    QApplication.processEvents()
    assert not popup_events.hidden, "An internal action must not dismiss the panel mid-click"
    if action == "edit":
        assert picker.editor_card.isVisible()
        assert picker.name_input.text() == "Shared"
        assert picker.name_input.hasFocus()
    else:
        assert picker.removal_dialog is not None
        assert picker.removal_dialog.isVisible()
        picker.removal_dialog.reject()
    assert not picker.draft.is_dirty


@pytest.mark.parametrize("close_method", ["accept", "reject", "close"])
@pytest.mark.parametrize("action", ["edit", "remove"])
def test_color_dialog_close_preserves_actions_during_reactivation(qtbot, editing, close_method, action):
    _, service, _, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    dialog._form_scroll.ensureWidgetVisible(picker)
    tag_id = picker.draft.selected_ids[0]
    picker.open_editor(tag_id)
    original_color = picker.draft.definitions[tag_id].color
    reveal_widget(qtbot, picker.editor_card.custom_button)
    qtbot.mouseClick(picker.editor_card.custom_button, Qt.MouseButton.LeftButton)
    color_dialog = picker.editor_card.color_dialog
    assert color_dialog.isVisible()
    color_dialog.setCurrentColor(QColor("#123456"))
    getattr(color_dialog, close_method)()

    # Windows 可能先关闭颜色对话框再激活所有者，需处理这一暂时无焦点状态。
    QApplication.setActiveWindow(None)
    assert QApplication.focusWidget() is None
    QApplication.processEvents()
    remained_visible = picker.popup.isVisible()
    QApplication.setActiveWindow(dialog)
    assert remained_visible, "Closing custom color must not dismiss the tag panel during reactivation"
    assert not picker.has_active_modal
    expected_color = "#123456" if close_method == "accept" else original_color
    assert picker.draft.definitions[tag_id].color == expected_color

    button = getattr(picker._rows[tag_id], f"{action}_button")
    reveal_widget(qtbot, button)
    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    qtbot.mouseClick(dialog.windowHandle(), Qt.MouseButton.LeftButton, pos=button.mapTo(dialog, button.rect().center()))
    QApplication.processEvents()
    assert clicked, "The candidate action must receive the pointer click after custom color closes"
    if action == "edit":
        assert picker.name_input.hasFocus()
    else:
        assert picker.removal_dialog.isVisible()
        picker.removal_dialog.reject()


@pytest.mark.parametrize("close_method", ["accept", "reject", "close"])
def test_color_dialog_actions_with_administrator_window_hook(qtbot, qapp, editing, close_method):
    if qapp.platformName() != "windows":
        pytest.skip("Requires native Windows child-window hit testing")
    import ctypes
    from ctypes import wintypes

    from src.ui import theme, win_dnd_admin

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
    user32.ChildWindowFromPointEx.restype = wintypes.HWND
    user32.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    _, service, _, dialog = editing

    def native_click(widget):
        # 等待滚动和布局产生的原生几何消息到达窗口后，再选择指针命中位置。
        dialog._form_scroll.ensureWidgetVisible(widget)
        qtbot.wait(30)
        position = widget.mapTo(dialog, widget.rect().center())
        ratio = dialog.devicePixelRatioF()
        point = wintypes.POINT(round(position.x() * ratio), round(position.y() * ratio))
        hwnd = int(dialog.winId())
        while True:
            child = user32.ChildWindowFromPointEx(hwnd, point, 1 | 2 | 4)
            if not child or child == hwnd:
                break
            user32.MapWindowPoints(hwnd, child, ctypes.byref(point), 1)
            hwnd = child
        packed = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
        user32.SendMessageW(hwnd, 0x0200, 0, packed)
        user32.SendMessageW(hwnd, 0x0201, 1, packed)
        qtbot.wait(30)
        user32.SendMessageW(hwnd, 0x0202, 0, packed)
        qtbot.wait(50)

    # 直接验证提升权限后安装的应用回调，无需改变权限策略。
    window_filter = win_dnd_admin._ArmOnShow(qapp)
    qapp.installEventFilter(window_filter)
    previous_style = qapp.styleSheet()
    try:
        qapp.setStyleSheet(theme.render_qss("dark"))
        configure(dialog, service)
        QApplication.setActiveWindow(dialog)
        picker = dialog.tag_picker
        dialog._form_scroll.ensureWidgetVisible(picker)
        picker.input.setText("Second")
        qtbot.keyClick(picker.input, Qt.Key.Key_Return)
        first, second = picker.draft.selected_ids
        picker.open_editor(first)
        picker._editor_scroll.ensureWidgetVisible(picker.editor_card.custom_button)
        QApplication.processEvents()
        qtbot.mouseClick(picker.editor_card.custom_button, Qt.MouseButton.LeftButton)
        color_dialog = picker.editor_card.color_dialog
        assert color_dialog is not None and color_dialog.isVisible()
        getattr(color_dialog, close_method)()
        QApplication.processEvents()
        assert not color_dialog.isVisible()
        assert not picker.has_active_modal
        qtbot.waitUntil(dialog.isActiveWindow)

        picker._candidate_scroll.ensureWidgetVisible(picker._rows[second])
        native_click(picker._rows[second].edit_button)
        assert picker._active_tag_id == second, "Native pointer dispatch missed the edit action"
        picker._candidate_scroll.ensureWidgetVisible(picker._rows[first])
        native_click(picker._rows[first].remove_button)
        assert picker.removal_dialog is not None and picker.removal_dialog.isVisible()
        picker.removal_dialog.reject()
    finally:
        qapp.removeEventFilter(window_filter)
        window_filter.deleteLater()
        qapp.setStyleSheet(previous_style)


def test_parent_delivered_click_uses_popup_bounds_for_dismissal(qtbot, editing):
    _, service, _, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    picker.input.setFocus()
    picker.show_popup()
    button = picker._rows[picker.draft.selected_ids[0]].edit_button
    reveal_widget(qtbot, button)

    def press_at(global_position):
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(dialog.mapFromGlobal(global_position)),
            QPointF(global_position),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(dialog, event)

    press_at(button.mapToGlobal(button.rect().center()))
    assert picker.popup.isVisible()
    press_at(dialog.mapToGlobal(QPoint(1, 1)))
    assert not picker.popup.isVisible()
    picker.show_popup()
    qtbot.keyClick(picker.input, Qt.Key.Key_Escape)
    assert not picker.popup.isVisible()


def test_save_commits_metadata_and_shared_tags_before_accepting(qtbot, editing):
    index, service, original, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    dialog._author_input.setText("After")
    picker.open_editor(picker.draft.selected_ids[0])
    picker.name_input.setText("Renamed")
    dialog._on_save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert index.get_mod(FIRST_MOD_ID).info.author == "After"
    assert index.get_mod(SECOND_MOD_ID).info.tags == ["Renamed"]
    assert original.info.tags == ["Shared"]
    committed = dialog.get_result()["tag_commit_result"]
    assert {mod.mod_id for mod in committed.mods} == {FIRST_MOD_ID, SECOND_MOD_ID}


def test_db_failure_keeps_dialog_draft_and_original_mod(editing, caplog):
    index, service, original, dialog = editing
    configure(dialog, service)
    dialog._author_input.setText("After")
    dialog.tag_picker.draft.add_names(["New"])
    index._db().execute("""CREATE TRIGGER reject_edit BEFORE UPDATE ON mods
        BEGIN SELECT RAISE(ABORT, 'blocked for test'); END""")
    dialog._on_save()
    assert dialog.isVisible()
    assert dialog.get_result() is None
    assert dialog.tag_picker.draft.names == ["Shared", "New"]
    assert "blocked for test" in dialog._save_error.text()
    assert any(
        record.exc_info and "blocked for test" in str(record.exc_info[1]) and FIRST_MOD_ID in record.getMessage()
        for record in caplog.records
    )
    assert index.get_mod(FIRST_MOD_ID).info.author == "Before"
    assert original.info.author == "Before"
    index._db().execute("DROP TRIGGER reject_edit")
    dialog._on_save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared", "New"]


def test_pending_text_and_name_composition_block_save(qtbot, editing):
    index, service, _, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    picker.input.setText("unfinished")
    dialog._on_save()
    assert dialog.isVisible()
    assert picker.input.hasFocus()
    picker.input.clear()
    picker.open_editor(picker.draft.selected_ids[0])
    QApplication.sendEvent(picker.name_input, QInputMethodEvent("高清", []))
    qtbot.keyClick(picker.name_input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    dialog._on_save()
    assert dialog.isVisible()
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared"]


def test_removal_modal_blocks_outer_save_shortcut(qtbot, editing):
    index, service, _, dialog = editing
    configure(dialog, service)
    dialog._author_input.setText("After")
    dialog.tag_picker.open_removal(dialog.tag_picker.draft.selected_ids[0])
    confirmation = dialog.tag_picker.removal_dialog
    dialog._on_save()
    qtbot.keyClick(dialog, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dialog.isVisible()
    assert confirmation.isVisible()
    assert index.get_mod(FIRST_MOD_ID).info.author == "Before"
    confirmation.reject()


def test_cancel_requires_explicit_discard_and_writes_nothing(qtbot, editing):
    index, service, _, dialog = editing
    configure(dialog, service)
    dialog.tag_picker.draft.add_names(["New"])
    dialog.reject()
    confirmation = dialog.discard_dialog
    assert dialog.isVisible()
    assert confirmation.isVisible()
    qtbot.mouseClick(confirmation.continue_button, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()
    dialog.reject()
    qtbot.mouseClick(dialog.discard_dialog.discard_button, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible()
    assert dialog.get_result() is None
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared"]
    assert [tag.name for tag in service.list_tags()] == ["Shared"]


def test_discarding_confirmed_candidate_removal_preserves_all_references(qtbot, editing):
    index, service, original, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    tag_id = picker.draft.selected_ids[0]
    picker.open_removal(tag_id)
    qtbot.mouseClick(picker.removal_dialog.confirm_button, Qt.MouseButton.LeftButton)
    assert picker.draft.deleted_ids == {tag_id}

    dialog.reject()
    qtbot.mouseClick(dialog.discard_dialog.discard_button, Qt.MouseButton.LeftButton)

    assert not dialog.isVisible()
    assert dialog.get_result() is None
    assert [(tag.id, tag.usage_count) for tag in service.list_tags()] == [(tag_id, 2)]
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared"]
    assert index.get_mod(SECOND_MOD_ID).info.tags == ["Shared"]
    assert original.info.tags == ["Shared"]


def test_candidate_removal_save_failure_rolls_back_definition_and_references(qtbot, editing):
    index, service, _, dialog = editing
    configure(dialog, service)
    picker = dialog.tag_picker
    tag_id = picker.draft.selected_ids[0]
    picker.open_removal(tag_id)
    qtbot.mouseClick(picker.removal_dialog.confirm_button, Qt.MouseButton.LeftButton)
    index._db().execute("""CREATE TRIGGER reject_tag_removal BEFORE UPDATE ON mods
        BEGIN SELECT RAISE(ABORT, 'blocked deletion'); END""")
    index._db().commit()

    dialog._on_save()

    assert dialog.isVisible()
    assert dialog.get_result() is None
    assert "blocked deletion" in dialog._save_error.text()
    assert [(tag.id, tag.usage_count) for tag in service.list_tags()] == [(tag_id, 2)]
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared"]
    assert index.get_mod(SECOND_MOD_ID).info.tags == ["Shared"]
    index._db().execute("DROP TRIGGER reject_tag_removal")
    index._db().commit()
    dialog._on_save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert service.list_tags() == []
    assert index.get_mod(FIRST_MOD_ID).info.tags == []
    assert index.get_mod(SECOND_MOD_ID).info.tags == []


class _EditWindow(QWidget):
    _on_mod_edit = MainWindow._on_mod_edit
    _suppress_watcher = MainWindow._suppress_watcher

    def __init__(self, manager):
        super().__init__()
        self._mod_manager = manager
        self.updated_mods = []
        self.refresh_count = 0
        self.tag_refresh_count = 0
        self._watcher_suppress_until = 0.0
        self._overview_page = SimpleNamespace(
            update_mod_in_view=self.updated_mods.append,
            update_mods_in_view=self.updated_mods.extend,
            refresh_tags=self._refresh_tags,
        )

    def _refresh_tags(self, *, refresh_view=True):
        self.tag_refresh_count += 1

    def _refresh_overview_mods(self):
        self.refresh_count += 1


@pytest.fixture
def main_edit(qtbot, temp_data_dir, monkeypatch):
    register_games("game-a")
    manager = ModManager()
    manager.set_game("game-a")
    index = manager._index
    for mod_id, name in ((FIRST_MOD_ID, "Original"), (SECOND_MOD_ID, "Two")):
        container = index.paths.roots["game-a"] / name
        path = container / name
        path.mkdir(parents=True)
        (path / "mod.ini").write_text("; fixture", encoding="utf-8")
        content_hash = Mod.compute_sha1(path)
        imported_at = "2026-10-05T10:00:00"
        assert write_container_marker(container, name, content_hash, mod_id=mod_id, imported_at=imported_at)
        assert index.add_mod(
            Mod(
                game_id="game-a",
                mod_id=mod_id,
                imported_at=imported_at,
                name=name,
                folder_name=name,
                path=str(path),
                content_hash=content_hash,
                info=ModInfo(tags=["Shared"], author="Before"),
            )
        )
    for mod_id in (FIRST_MOD_ID, SECOND_MOD_ID):
        assert manager._write_mod_sidecar(index.get_mod(mod_id))
    window = _EditWindow(manager)
    qtbot.addWidget(window)
    warnings = []
    monkeypatch.setattr("src.ui.main_window.QMessageBox.warning", lambda parent, title, text: warnings.append(text))

    def drive(edit):
        class Dialog(ModEditDialog):
            def exec(self):
                qtbot.addWidget(self)
                self.show()
                QApplication.processEvents()
                edit(self)
                self._on_save()
                result = self.result()
                self.done(result)
                return result

        monkeypatch.setattr("src.ui.main_window.ModEditDialog", Dialog)
        window._on_mod_edit(FIRST_MOD_ID)

    yield window, manager, index, warnings, drive
    index.close()
    reset_game_service()


def test_main_window_refreshes_every_shared_mod_without_second_save(main_edit, monkeypatch):
    window, manager, index, warnings, drive = main_edit
    original_update = manager.update_mod
    updates = []

    def update(mod):
        updates.append(mod.mod_id)
        return original_update(mod)

    monkeypatch.setattr(manager, "update_mod", update)

    def edit(dialog):
        picker = dialog.tag_picker
        picker.open_editor(picker.draft.selected_ids[0])
        picker.name_input.setText("Renamed")
        dialog._author_input.setText("After")

    drive(edit)
    assert index.get_mod(SECOND_MOD_ID).info.tags == ["Renamed"]
    assert {mod.mod_id for mod in window.updated_mods} == {FIRST_MOD_ID, SECOND_MOD_ID}
    assert window.tag_refresh_count == 1
    assert window.refresh_count == 0
    assert updates == []
    assert warnings == []


@pytest.mark.parametrize("view_mode", ["grid", "list"])
@pytest.mark.parametrize("action", ["rename", "color"])
def test_shared_tag_save_publishes_all_updated_mods_in_one_delta(main_edit, qtbot, monkeypatch, view_mode, action):
    from src.ui.pages.overview import OverviewPage
    from src.ui.widgets.tag_chip import TagChip

    window, manager, index, warnings, drive = main_edit
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(manager)
    page.resize(1200, 780)
    page.set_current_mods(manager.get_mods())
    page._set_view(view_mode)
    page.show()
    tag = manager.get_tag_service().list_tags()[0]
    page.select_tag_filter(tag.id)
    page._on_mod_card_clicked(FIRST_MOD_ID)
    cards = dict(page._vs_card_cache)
    window._overview_page = page
    publications = []
    refreshes = []
    original_publish = page.apply_reconcile_delta
    original_refresh = page.refresh_tags

    def publish(mods, removed_ids):
        publications.append({mod.mod_id for mod in mods})
        return original_publish(mods, removed_ids)

    def refresh(**kwargs):
        refreshes.append(True)
        return original_refresh(**kwargs)

    monkeypatch.setattr(page, "apply_reconcile_delta", publish)
    monkeypatch.setattr(page, "refresh_tags", refresh)

    def edit(dialog):
        draft = dialog.tag_picker.draft
        if action == "rename":
            draft.rename(tag.id, "Renamed")
        else:
            draft.set_color(tag.id, "mint")

    drive(edit)

    updated_ids = {FIRST_MOD_ID, SECOND_MOD_ID} if action == "rename" else {FIRST_MOD_ID}
    assert publications == [updated_ids]
    assert len(refreshes) <= 2
    expected_name = "Renamed" if action == "rename" else "Shared"
    assert {entry["id"] for entry in page._display_items} == {FIRST_MOD_ID, SECOND_MOD_ID}
    assert all(entry["data"].info.tags == [expected_name] for entry in page._display_items)
    assert page._tag_filter.selected_ids == [tag.id]
    assert page._tag_filter.catalog[tag.id].name == expected_name
    if action == "color":
        assert page._tag_filter.catalog[tag.id].color == "mint"
        if view_mode == "grid":
            qtbot.waitUntil(lambda: all(mid in page._vs_card_cache for mid in (FIRST_MOD_ID, SECOND_MOD_ID)))
            for mid in (FIRST_MOD_ID, SECOND_MOD_ID):
                chips = page._vs_card_cache[mid]._tag_row.findChildren(TagChip)
                assert [(chip.tag.id, chip.tag.color) for chip in chips] == [(tag.id, "mint")]
        else:
            page._table._sync_visible_rows()
            for mid in (FIRST_MOD_ID, SECOND_MOD_ID):
                row = page._display_idx_map[mid]
                assert [(item.id, item.color) for item in page._table._name_data[row][1]] == [(tag.id, "mint")]
        assert [(chip.tag.id, chip.tag.color) for chip in page._panel_tags.findChildren(TagChip)] == [(tag.id, "mint")]
    assert page._selected_mod.mod_id == FIRST_MOD_ID
    assert page._selected_mod.info.tags == [expected_name]
    assert all(page._vs_card_cache.get(mid) is card for mid, card in cards.items())
    assert index.get_mod(SECOND_MOD_ID).info.tags == [expected_name]
    assert window.refresh_count == 0
    assert warnings == []
    page.close()


def test_candidate_removal_updates_all_mods_and_sidecars_only_on_save(qtbot, main_edit):
    window, manager, index, warnings, drive = main_edit

    def edit(dialog):
        picker = dialog.tag_picker
        tag_id = picker.draft.selected_ids[0]
        picker.show_popup()
        qtbot.mouseClick(picker._rows[tag_id].remove_button, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(picker.removal_dialog.confirm_button, Qt.MouseButton.LeftButton)
        assert [(tag.id, tag.usage_count) for tag in manager.get_tag_service().list_tags()] == [(tag_id, 2)]
        for mod_id in (FIRST_MOD_ID, SECOND_MOD_ID):
            saved = index.get_mod(mod_id)
            assert saved.info.tags == ["Shared"]
            assert read_sidecar(saved.path)["info"]["tags"] == ["Shared"]

    drive(edit)

    assert manager.get_tag_service().list_tags() == []
    assert index._db().execute("SELECT COUNT(*) FROM mod_tags").fetchone()[0] == 0
    for mod_id in (FIRST_MOD_ID, SECOND_MOD_ID):
        saved = index.get_mod(mod_id)
        assert saved.info.tags == []
        assert read_sidecar(saved.path)["info"]["tags"] == []
    assert {mod.mod_id for mod in window.updated_mods} == {FIRST_MOD_ID, SECOND_MOD_ID}
    assert window.tag_refresh_count == 1
    assert warnings == []


def test_shared_deletion_can_retry_in_open_dialog_after_file_batch_failure(qtbot, main_edit, monkeypatch):
    from src.core import tag_file_batch
    from src.ui.operation_check import run_file_check

    window, manager, index, warnings, _drive = main_edit
    dialog = ModEditDialog(index.get_mod(FIRST_MOD_ID), window)
    qtbot.addWidget(dialog)
    dialog.set_tag_service(manager.get_tag_service())
    dialog.set_save_handler(lambda fields, draft: manager.commit_tag_edit(draft, fields))
    manager.operation_check_runner = lambda work: run_file_check(work, window)
    dialog.show()
    dialog._author_input.setText("Retained author")
    picker = dialog.tag_picker
    tag_id = picker.draft.selected_ids[0]
    picker.open_removal(tag_id)
    picker.removal_dialog.accept()
    original_write = tag_file_batch.write_sidecar
    blocked_path = index.get_mod(SECOND_MOD_ID).path

    def write(path, manifest):
        if path == blocked_path:
            raise PermissionError("temporary metadata lock")
        return original_write(path, manifest)

    with monkeypatch.context() as patch:
        patch.setattr(tag_file_batch, "write_sidecar", write)
        dialog._on_save()

    assert dialog.isVisible()
    assert dialog.get_result() is None
    assert dialog._save_error.isVisible()
    assert dialog._save_button.isEnabled()
    assert picker.draft.deleted_ids == {tag_id}
    assert index.get_mod(FIRST_MOD_ID).info.tags == ["Shared"]
    assert read_sidecar(index.get_mod(FIRST_MOD_ID).path)["info"]["tags"] == ["Shared"]

    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert index.get_mod(FIRST_MOD_ID).info.author == "Retained author"
    assert manager.get_tag_service().list_tags() == []
    for mod_id in (FIRST_MOD_ID, SECOND_MOD_ID):
        mod = index.get_mod(mod_id)
        assert mod.info.tags == []
        assert read_sidecar(mod.path)["info"]["tags"] == []


def test_physical_rename_runs_after_transaction_without_full_rebuild(main_edit, monkeypatch):
    window, manager, index, warnings, drive = main_edit
    original_rename = manager.rename_mod
    observations = []

    def rename(mod_id, name, sync_folder=True):
        saved = index.get_mod(mod_id)
        observations.append((saved.name, saved.info.author))
        return original_rename(mod_id, name, sync_folder=sync_folder)

    monkeypatch.setattr(manager, "rename_mod", rename)

    def edit(dialog):
        dialog._name_input.setText("Renamed Mod")
        dialog._author_input.setText("After")

    drive(edit)
    assert observations == [("Renamed Mod", "After")]
    saved = index.get_mod(FIRST_MOD_ID)
    assert saved.folder_name == "Renamed Mod"
    assert window.refresh_count == 0
    assert [mod.mod_id for mod in window.updated_mods] == [FIRST_MOD_ID]
    assert window._watcher_suppress_until == 0
    assert warnings == []


def test_removing_active_tag_reprojects_once_to_reveal_excluded_mods(main_edit, qtbot, tmp_path):
    from src.ui.pages.overview import OverviewPage

    window, manager, index, warnings, drive = main_edit
    source = tmp_path / "untagged"
    source.mkdir()
    (source / "mod.ini").write_text("; untagged", encoding="utf-8")
    extra = manager.import_mod(source_path=str(source), name="Untagged")
    assert extra is not None
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(manager)
    page.set_current_mods(manager.get_mods())
    page.show()
    tag = manager.get_tag_service().list_tags()[0]
    page.select_tag_filter(tag.id)
    assert extra.mod_id not in page._display_idx_map
    window._overview_page = page

    def reproject():
        window.refresh_count += 1
        page.set_current_mods(manager.get_mods())

    window._refresh_overview_mods = reproject
    drive(lambda dialog: dialog.tag_picker.draft.remove_candidate(tag.id))
    assert page._tag_filter.selected_ids == []
    assert extra.mod_id in page._display_idx_map
    assert window.refresh_count == 1
    assert warnings == []
    page.close()


def test_post_commit_physical_rename_failure_is_reported_as_saved(main_edit, monkeypatch):
    window, manager, index, warnings, drive = main_edit
    monkeypatch.setattr(manager, "rename_mod", lambda *args, **kwargs: None)

    def edit(dialog):
        dialog._name_input.setText("Saved Name")
        dialog._author_input.setText("After")

    drive(edit)
    assert index.get_mod(FIRST_MOD_ID).name == "Saved Name"
    assert index.get_mod(FIRST_MOD_ID).info.author == "After"
    assert window.updated_mods[0].name == "Saved Name"
    assert window.refresh_count == 0
    assert warnings and "已保存" in warnings[0]
    assert "重命名" in warnings[0]


def test_failed_shared_sidecar_save_keeps_draft_and_does_not_publish(main_edit, monkeypatch):
    from src.core import tag_file_batch

    window, manager, index, warnings, drive = main_edit
    mods = [index.get_mod(mid) for mid in (FIRST_MOD_ID, SECOND_MOD_ID)]
    before = {mod.mod_id: read_sidecar(mod.path) for mod in mods}
    original_write = tag_file_batch.write_sidecar
    writes = []

    def fail_second(path, manifest):
        writes.append(path)
        return False if len(writes) == 2 else original_write(path, manifest)

    monkeypatch.setattr(tag_file_batch, "write_sidecar", fail_second)

    def edit(dialog):
        dialog._name_input.setText("Saved Name")
        draft = dialog.tag_picker.draft
        draft.rename(draft.selected_ids[0], "Renamed")

    drive(edit)
    dialog = window._mod_edit_dialog
    assert len(writes) == 2
    assert dialog.get_result() is None
    assert dialog._save_error.text()
    assert dialog._name_input.text() == "Saved Name"
    assert dialog.tag_picker.draft.names == ["Renamed"]
    assert index.get_mod(FIRST_MOD_ID).name == "Original"
    for mod in mods:
        assert index.get_mod(mod.mod_id).info.tags == ["Shared"]
        assert read_sidecar(mod.path) == before[mod.mod_id]
    assert window.updated_mods == []
    assert window.refresh_count == 0
    assert warnings == []


def test_small_screen_scrolls_form_and_keeps_save_visible(qtbot, editing, monkeypatch):
    _, service, _, dialog = editing
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 640, 480)))
    configure(dialog, service)
    qtbot.waitUntil(lambda: dialog.height() <= 400)
    assert dialog.height() <= 400
    assert dialog._form_scroll.verticalScrollBar().maximum() > 0
    position = dialog._save_button.mapTo(dialog, QPoint())
    assert 0 <= position.y() < dialog.height() - dialog._save_button.height()
    assert dialog._name_input.hasFocus()


def test_main_window_passes_existing_long_path_to_rename(main_edit, temp_data_dir, monkeypatch):
    window, manager, index, warnings, drive = main_edit
    parent_id = ""
    for number in range(3):
        node = manager.add_character(f"nested-{number}-" + "x" * 65, parent_id=parent_id)
        assert node is not None
        parent_id = node.id
    parent = index.paths.node_path(parent_id)
    body = parent / "Original" / "Original"
    fs.ensure_dir(body)
    fs.write_text(body / "mod.ini", "; long-path fixture", encoding="utf-8")
    assert len(str(body)) > 260
    mod = index.get_mod(FIRST_MOD_ID)
    mod.node_id = parent_id
    mod.path = str(body)
    mod.content_hash = Mod.compute_sha1(body)
    assert write_container_marker(
        body.parent, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
    )
    assert index.update_mod(mod)
    assert manager._write_mod_sidecar(mod)
    original_rename = manager.rename_mod
    called = []

    def rename(mod_id, name, sync_folder=True):
        called.append(mod_id)
        return original_rename(mod_id, name, sync_folder=sync_folder)

    monkeypatch.setattr(manager, "rename_mod", rename)
    drive(lambda dialog: dialog._name_input.setText("Renamed Long Mod"))
    assert called == [FIRST_MOD_ID], "A valid long path must reach the physical rename operation"
    assert index.get_mod(FIRST_MOD_ID).name == "Renamed Long Mod"
    assert not any("物理路径不存在" in message for message in warnings)
