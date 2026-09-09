from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme
from src.ui.main_window import MainWindow
from src.ui.pages.overview import BatchAddCharacterDialog, OverviewPage
from tests.core.test_library_reconcile import mm as mm


def button(dialog, text):
    return next(item for item in dialog.findChildren(QPushButton) if item.text() == text)


@pytest.mark.parametrize("finish", ["取消", "全部添加"])
def test_reopen_clears_rows_images_scroll_and_result_without_rebuilding_first_form(
    qtbot, temp_data_dir, monkeypatch, finish
):
    thumbnail = temp_data_dir / "thumbnail.png"
    pixmap = QPixmap(20, 30)
    pixmap.fill(Qt.GlobalColor.magenta)
    assert pixmap.save(str(thumbnail))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_a, **_k: (str(thumbnail), ""))
    dialog = BatchAddCharacterDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    first = dialog._entries[0]
    first["name_input"].setText("Old category")
    first["name_en_input"].setText("Old English")
    first["type_combo"].setCurrentIndex(1)
    qtbot.mouseClick(first["thumb_preview"], Qt.MouseButton.LeftButton)
    assert not first["thumb_preview"].pixmap().isNull()
    assert first["thumbnail_path"] == str(thumbnail)
    for index in range(8):
        button(dialog, "＋ 添加类别").click()
        dialog._entries[-1]["name_input"].setText(f"Extra {index}")
    scroll = dialog.findChild(QScrollArea).verticalScrollBar()
    qtbot.waitUntil(lambda: scroll.maximum() > 0)
    scroll.setValue(scroll.maximum())
    button(dialog, finish).click()
    assert not dialog.isVisible()
    previous_results = dialog.get_results()
    if finish == "全部添加":
        assert previous_results[0] == {
            "name": "Old category",
            "name_en": "Old English",
            "thumbnail": str(thumbnail),
            "node_type": "character",
        }
        assert len(previous_results) == 9

    dialog.prepare_for_open()

    assert len(dialog._entries) == 1 and dialog._entries[0] is first
    assert first["name_input"].text() == first["name_en_input"].text() == ""
    assert first["type_combo"].currentData() == "category"
    assert first["thumbnail_path"] == ""
    assert first["thumb_preview"].text() == "选择\n图片"
    assert first["thumb_preview"].pixmap().isNull()
    assert first["num_label"].text() == "#1"
    assert dialog._entry_count_label.text() == "共 1 个类别"
    assert scroll.value() == 0
    assert dialog.get_results() == []
    assert dialog.result() == QDialog.DialogCode.Rejected
    if finish == "全部添加":
        assert len(previous_results) == 9, "reset must not mutate already submitted results"
    dialog.show()
    assert dialog.focusWidget() is first["name_input"]
    qtbot.keyClicks(first["name_input"], "Fresh")
    button(dialog, "＋ 添加类别").click()
    assert dialog._entries[-1]["num_label"].text() == "#2"
    button(dialog, "全部添加").click()
    assert dialog.get_results() == [{"name": "Fresh", "name_en": "", "thumbnail": "", "node_type": "category"}]


def test_main_window_prepares_batch_form_and_submits_to_each_current_parent(qtbot, temp_data_dir):
    from src.core.config import Config
    from tests.ui.test_mod_updates_experience import make_host

    host = make_host(qtbot, Config())
    page = OverviewPage(host)
    qtbot.addWidget(page)
    host._overview_page = page
    MainWindow._prepare_interaction_dialogs(host)
    prepared = host.findChildren(BatchAddCharacterDialog)
    assert len(prepared) == 1 and not prepared[0].isVisible()
    dialog = prepared[0]
    first_field = dialog._entries[0]["name_input"]
    observed = []
    submitted = []
    page.character_batch_add_requested.connect(submitted.append)

    def fill_and_close(name, accept):
        active = QApplication.activeModalWidget()
        try:
            observed.append(
                (
                    active is dialog,
                    active._entries[0]["name_input"] is first_field,
                    active._entries[0]["name_input"].text(),
                )
            )
            active._entries[0]["name_input"].setText(name)
            button(active, "全部添加" if accept else "取消").click()
        finally:
            if active is not None and active.isVisible():
                active.reject()

    try:
        for parent, name, accept in (("one", "Cancelled", False), ("two", "Second", True), ("three", "Third", True)):
            page._current_parent_id = parent
            QTimer.singleShot(0, lambda n=name, a=accept: fill_and_close(n, a))
            page._on_batch_add()
        assert observed == [(True, True, "")] * 3
        assert submitted == [
            [{"name": "Second", "name_en": "", "thumbnail": "", "node_type": "category", "parent_id": "two"}],
            [{"name": "Third", "name_en": "", "thumbnail": "", "node_type": "category", "parent_id": "three"}],
        ]
    finally:
        page.close()


def test_batch_window_does_not_make_the_page_and_its_ancestors_native(qtbot, qapp, temp_data_dir):
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(900, 700)
    root = QWidget()
    window.setCentralWidget(root)
    splitter = QSplitter()
    QVBoxLayout(root).addWidget(splitter)
    stack = QStackedWidget()
    splitter.addWidget(stack)
    page = OverviewPage()
    stack.addWidget(page)
    # 打开对话框时不得将显示器已有的原生前景层次扩展到页面布局内。
    root.winId()
    window.show()
    qapp.processEvents()
    content = (splitter, stack, page, page._search_input)
    assert all(widget.windowHandle() is None for widget in content)
    try:
        dialog = page.prepare_batch_add_dialog()
        dialog.show()
        qapp.processEvents()
        assert dialog.parentWidget() is window
        assert dialog.windowHandle().transientParent() is window.windowHandle()
        assert all(widget.windowHandle() is None for widget in content)
        assert all(not widget.testAttribute(Qt.WidgetAttribute.WA_NativeWindow) for widget in content)
        dialog.reject()
    finally:
        page.close()
        window.close()


def test_theme_change_prepares_a_form_with_current_colors(qtbot, temp_data_dir):
    previous = theme.current_theme()
    page = None
    try:
        theme.set_theme("light")
        page = OverviewPage()
        qtbot.addWidget(page)
        first = page.prepare_batch_add_dialog()
        assert theme.color("bg.card") in first._entries[0]["frame"].styleSheet()
        theme.set_theme("dark")
        page.retheme()
        prepared = page.prepare_batch_add_dialog()
        assert theme.color("bg.card") in prepared._entries[0]["frame"].styleSheet()
        assert theme.color("bg.subtle") in prepared._entries[0]["thumb_preview"].styleSheet()
        assert not prepared.isVisible()
    finally:
        if page is not None:
            page.close()
        theme.set_theme(previous)


def test_batch_add_persists_each_selected_node_type(qtbot, mm):
    parent = mm.add_character("Parent")
    host = SimpleNamespace(
        _mod_manager=mm,
        _game_service=mm._game_service,
        _load_characters=lambda _game_id: None,
        _suppress_watcher=lambda: None,
    )
    page = OverviewPage()
    qtbot.addWidget(page)
    page._current_parent_id = parent.id
    page.character_batch_add_requested.connect(lambda items: MainWindow._on_character_batch_add(host, items))

    def submit_categories():
        """通过同一批表单提交不同节点类型，核对宿主接收后的实际落盘结果。"""
        dialog = QApplication.activeModalWidget()
        first = dialog._entries[0]
        first["name_input"].setText("Role")
        first["name_en_input"].setText("Role English")
        first["type_combo"].setCurrentIndex(1)
        button(dialog, "＋ 添加类别").click()
        dialog._entries[1]["name_input"].setText("Folder")
        button(dialog, "全部添加").click()

    try:
        QTimer.singleShot(0, submit_categories)
        page._on_batch_add()
        nodes = {node.name: node for node in mm.get_characters() if node.parent_id == parent.id}
        assert {name: node.node_type for name, node in nodes.items()} == {"Role": "character", "Folder": "category"}
        assert nodes["Role"].name_en == "Role English"
        for name, expected_type in (("Role", "character"), ("Folder", "category")):
            marker_path = mm._index.paths.node_path(nodes[name].id) / ".d3dxcategory"
            assert json.loads(marker_path.read_text(encoding="utf-8"))["node_type"] == expected_type
    finally:
        page.close()
