import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget


@pytest.fixture
def shortcut_window(qtbot, temp_data_dir):
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    window = QWidget()
    layout = QVBoxLayout(window)
    edit = QLineEdit("selected text")
    listing = QListWidget()
    listing.addItems(["one", "two"])
    other = QWidget()
    other.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    layout.addWidget(edit)
    layout.addWidget(listing)
    layout.addWidget(other)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    calls = []
    dispatcher.register_context(
        listing,
        "overview",
        {
            "edit.select_all": lambda: calls.append("all"),
            "edit.cut": lambda: calls.append("cut"),
            "edit.paste": lambda: calls.append("paste"),
            "edit.delete": lambda: calls.append("delete"),
        },
    )
    dispatcher.register_window_action("view.refresh", lambda: calls.append("refresh"))
    dispatcher.register_window_action("mod.import", lambda: calls.append("import"))
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    edit.setFocus()
    QCoreApplication.processEvents()
    return window, edit, listing, other, settings, dispatcher, calls


def focus(widget):
    widget.window().activateWindow()
    widget.setFocus()
    QCoreApplication.processEvents()


def test_dispatch_only_observes_current_focus(shortcut_window):
    from PySide6.QtCore import QEvent

    from src.ui.shortcut_manager import ShortcutDispatcher

    window, edit, listing, other, settings, _dispatcher, _calls = shortcut_window
    seen = []

    class ObservedDispatcher(ShortcutDispatcher):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.User:
                seen.append(watched)
            return super().eventFilter(watched, event)

    # 在 Qt 首次分发前安装虚方法覆盖，PySide 会缓存已有包装对象的覆盖方法。
    _observer = ObservedDispatcher(settings, window)

    focus(edit)
    for widget in (edit, listing, other):
        QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.User))
    assert seen == [edit]
    seen.clear()
    focus(listing)
    for widget in (edit, listing, other):
        QCoreApplication.sendEvent(widget, QEvent(QEvent.Type.User))
    assert seen == [listing]


def test_window_rebind_is_immediate_and_old_key_stops(qtbot, shortcut_window):
    _window, edit, _listing, _other, settings, _dispatcher, calls = shortcut_window
    assert settings.save_binding("view.refresh", "Ctrl+Shift+R")
    qtbot.keyClick(edit, Qt.Key.Key_F5)
    qtbot.keyClick(edit, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert calls == ["refresh"]


def test_shared_binding_selects_text_or_focused_list(qtbot, shortcut_window):
    _window, edit, listing, _other, settings, _dispatcher, calls = shortcut_window
    assert settings.save_binding("edit.select_all", "Ctrl+Shift+A")
    focus(edit)
    qtbot.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert edit.selectedText() == ""
    qtbot.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert edit.selectedText() == edit.text()
    assert calls == []
    focus(listing)
    qtbot.keyClick(listing, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert calls == []
    qtbot.keyClick(listing, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert calls == ["all"]


def test_unsupported_focus_never_acts_on_background_selection(qtbot, shortcut_window):
    _window, _edit, _listing, other, _settings, _dispatcher, calls = shortcut_window
    focus(other)
    qtbot.keyClick(other, Qt.Key.Key_Delete)
    qtbot.keyClick(other, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert calls == []


def test_replacement_overrides_native_text_binding_without_pasting(qtbot, shortcut_window):
    _window, edit, _listing, _other, settings, _dispatcher, calls = shortcut_window
    QApplication.clipboard().setText("clipboard")
    assert settings.save_binding("mod.import", "Ctrl+V", replace=True)
    focus(edit)
    before = edit.text()
    qtbot.keyClick(edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert calls == ["import"]
    assert edit.text() == before


def test_plain_delete_and_space_keep_text_semantics_after_list_rebind(qtbot, shortcut_window):
    _window, edit, listing, _other, settings, _dispatcher, calls = shortcut_window
    assert settings.save_binding("edit.paste", "Delete", replace=True)
    edit.setText("abc")
    edit.setCursorPosition(1)
    focus(edit)
    qtbot.keyClick(edit, Qt.Key.Key_Delete)
    qtbot.keyClick(edit, Qt.Key.Key_Space)
    assert edit.text() == "a c"
    assert calls == []
    focus(listing)
    qtbot.keyClick(listing, Qt.Key.Key_Delete)
    assert calls == ["paste"]


def test_modal_text_uses_binding_but_window_actions_stay_blocked(qtbot, shortcut_window):
    window, _edit, listing, _other, settings, _dispatcher, calls = shortcut_window
    assert settings.save_binding("edit.select_all", "Ctrl+Shift+A")
    dialog = QDialog(window)
    dialog.setModal(True)
    layout = QVBoxLayout(dialog)
    text = QLineEdit("modal text")
    layout.addWidget(text)
    qtbot.addWidget(dialog)
    dialog.show()
    focus(text)
    qtbot.keyClick(text, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert text.selectedText() == "modal text"
    qtbot.keyClick(text, Qt.Key.Key_F5)
    qtbot.keyClick(text, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(listing, Qt.Key.Key_Delete)
    assert calls == []
    dialog.close()


def test_composition_and_background_window_do_not_dispatch(qtbot, shortcut_window):
    window, edit, _listing, _other, _settings, _dispatcher, calls = shortcut_window
    focus(edit)
    QCoreApplication.sendEvent(edit, QInputMethodEvent("拼", []))
    qtbot.keyClick(edit, Qt.Key.Key_F5)
    assert calls == []
    QCoreApplication.sendEvent(edit, QInputMethodEvent("", []))
    qtbot.keyClick(edit, Qt.Key.Key_F5)
    assert calls == ["refresh"]
    unrelated = QWidget()
    unrelated.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    qtbot.addWidget(unrelated)
    unrelated.show()
    focus(unrelated)
    qtbot.keyClick(edit, Qt.Key.Key_F5)
    assert calls == ["refresh"]
    window.showMinimized()
    qtbot.keyClick(edit, Qt.Key.Key_F5)
    assert calls == ["refresh"]


def test_ini_key_capture_takes_priority(qtbot, shortcut_window):
    from src.ui.widgets.key_capture import KeyCaptureField

    window, _edit, _listing, _other, _settings, _dispatcher, calls = shortcut_window
    field = KeyCaptureField("a", window)
    window.layout().addWidget(field)
    field.show()
    focus(field)
    field.enter_capture()
    qtbot.keyClick(field, Qt.Key.Key_F5)
    assert field.token() == "VK_F5"
    assert calls == []


def test_saved_text_copy_cut_paste_and_delete(qtbot, shortcut_window):
    _window, edit, _listing, _other, settings, _dispatcher, calls = shortcut_window
    for command, key in (
        ("edit.copy", "Ctrl+Shift+C"),
        ("edit.cut", "Ctrl+Shift+X"),
        ("edit.paste", "Ctrl+Shift+V"),
        ("edit.delete", "Ctrl+D"),
    ):
        assert settings.save_binding(command, key)
    focus(edit)
    edit.selectAll()
    modified = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    qtbot.keyClick(edit, Qt.Key.Key_C, modified)
    assert QApplication.clipboard().text() == "selected text"
    QCoreApplication.processEvents()  # 完成延迟的 Windows OLE 剪贴板操作。
    qtbot.keyClick(edit, Qt.Key.Key_X, modified)
    assert edit.text() == ""
    QCoreApplication.processEvents()
    qtbot.keyClick(edit, Qt.Key.Key_V, modified)
    assert edit.text() == "selected text"
    edit.selectAll()
    qtbot.keyClick(edit, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert edit.text() == ""
    assert calls == []


def test_real_overview_and_preset_handlers_share_configuration(qtbot, temp_data_dir):
    from src.models.preset import Preset
    from src.ui.pages.overview import OverviewPage
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    window = QWidget()
    layout = QVBoxLayout(window)
    overview = OverviewPage()
    qtbot.addWidget(window)
    qtbot.addWidget(overview)
    layout.addWidget(overview)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    assert hasattr(overview, "install_shortcuts"), "总览与预设应接入统一分派器"
    overview.install_shortcuts(dispatcher)
    window.show()
    window.activateWindow()
    overview._display_items = [{"id": "one", "type": "category"}]
    assert settings.save_binding("edit.select_all", "Ctrl+Shift+A")
    focus(overview)
    qtbot.keyClick(overview, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert overview._selected_ids == set()
    qtbot.keyClick(overview, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert overview._selected_ids == {"one"}
    popup = overview._preset_selector._popup
    popup.set_presets([Preset(id="preset1", name="preset", game_id="test")], "")
    popup.show()
    focus(popup._list)
    popup._list.item(0).setSelected(True)
    pasted = []
    popup.duplicate_requested.connect(pasted.append)
    assert settings.save_binding("edit.copy", "Ctrl+Shift+C")
    assert settings.save_binding("edit.paste", "Ctrl+Shift+V")
    qtbot.keyClick(popup._list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(popup._list, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert pasted == []
    qtbot.keyClick(popup._list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    qtbot.keyClick(popup._list, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert pasted == [["preset1"]]
    popup.close()


@pytest.fixture
def overview_table(qtbot, temp_data_dir):
    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    overview = OverviewPage()
    qtbot.addWidget(overview)
    layout.addWidget(overview)
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    overview.install_shortcuts(dispatcher)
    overview._set_view("list")
    overview.set_current_mods([Mod(mod_id=name, name=name, folder_name=name) for name in ("one", "two", "three")])
    window.resize(1100, 700)
    window.show()
    focus(overview._table)
    yield overview, settings  # 在整个用例期间保持所有者窗口存活。


@pytest.mark.parametrize("action", ["cut", "delete"])
def test_table_select_all_then_keyboard_navigation_uses_visible_selection(qtbot, overview_table, action):
    overview, settings = overview_table
    table = overview._table
    assert settings.save_binding("edit.select_all", "Ctrl+Shift+A")
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(table.item(0, 1)).center())
    qtbot.keyClick(table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert {index.row() for index in table.selectionModel().selectedRows()} == {0, 1, 2}
    assert overview._selected_ids == {"one", "two", "three"}
    qtbot.keyClick(table, Qt.Key.Key_Down)
    target = table.item(1, 0).data(Qt.ItemDataRole.UserRole)
    assert {index.row() for index in table.selectionModel().selectedRows()} == {1}
    assert overview._selected_ids == {target}
    if action == "cut":
        qtbot.keyClick(table, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
        assert overview._clipboard_mod_ids == [target]
    else:
        deleted = []
        overview.mod_delete_requested.connect(deleted.append)
        qtbot.keyClick(table, Qt.Key.Key_Delete)
        assert deleted == [target]


def test_table_ctrl_click_and_refresh_preserve_selection_by_id(qtbot, overview_table):
    overview, _settings = overview_table
    table = overview._table
    for row, modifiers in ((0, Qt.KeyboardModifier.NoModifier), (2, Qt.KeyboardModifier.ControlModifier)):
        qtbot.mouseClick(
            table.viewport(),
            Qt.MouseButton.LeftButton,
            modifiers,
            pos=table.visualItemRect(table.item(row, 1)).center(),
        )
    selected = {table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in (0, 2)}
    assert overview._selected_ids == selected
    overview._sort_key = "name_desc"
    overview._refresh_table()
    assert overview._selected_ids == selected
    assert {index.data(Qt.ItemDataRole.UserRole) for index in table.selectionModel().selectedRows()} == selected


def test_cancelled_table_delete_keeps_selection_available(qtbot, overview_table):
    overview, _settings = overview_table
    table = overview._table
    first = table.item(0, 1)
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(first).center())
    target = table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    requested = []
    # 与用户取消主窗口的确认一致：收到请求后不删除、不重建表格。
    overview.mod_delete_requested.connect(requested.append)
    qtbot.keyClick(table, Qt.Key.Key_Delete)
    assert requested == [target]
    assert overview._selected_ids == {target}
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(first).center())
    qtbot.keyClick(table, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    assert overview._clipboard_mod_ids == [target]


def test_table_refresh_does_not_expand_collapsed_preview(qtbot, overview_table):
    overview, _settings = overview_table
    table = overview._table
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=table.visualItemRect(table.item(0, 1)).center())
    assert overview._main_splitter.sizes()[1] > 0
    overview._main_splitter.setSizes([overview._main_splitter.width(), 0])
    overview._refresh_table()
    assert overview._main_splitter.sizes()[1] == 0


@pytest.mark.parametrize("modal", [False, True])
def test_child_dialog_does_not_inherit_background_list_actions(qtbot, shortcut_window, modal):
    _window, _edit, listing, _other, _settings, _dispatcher, calls = shortcut_window
    dialog = QDialog(listing)
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    button = QPushButton("dialog action")
    layout.addWidget(button)
    dialog.setModal(modal)
    dialog.show()
    focus(button)
    qtbot.keyClick(button, Qt.Key.Key_Delete)
    qtbot.keyClick(button, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    assert calls == []
    dialog.close()
