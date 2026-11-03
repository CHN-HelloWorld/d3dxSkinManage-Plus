"""分类表单复用控件，结束后释放当前数据，重开时重新绑定校验和预览。"""

from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from src.ui import theme
from src.ui.pages.overview import CharacterDialog, OverviewPage


@pytest.mark.parametrize("operation", ["add", "edit"])
@pytest.mark.parametrize("finish", ["save", "cancel", "close", "escape"])
def test_repeated_category_dialogs_reuse_widgets_and_preserve_submission(qtbot, qapp, temp_data_dir, operation, finish):
    thumbnail = temp_data_dir / "category.png"
    image = QImage(180, 240, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.magenta)
    assert image.save(str(thumbnail))
    host = QWidget()
    qtbot.addWidget(host)
    page = OverviewPage(host)
    QVBoxLayout(host).addWidget(page)
    category = {
        "id": "category",
        "name": "Category",
        "name_en": "Original",
        "parent_id": "",
        "node_type": "category",
        "thumbnail": str(thumbnail),
    }
    page.set_characters([category])
    submitted = []
    page.character_add_requested.connect(lambda data: submitted.append(("add", data)))
    page.character_edit_requested.connect(lambda item_id, data: submitted.append((item_id, data)))
    dialogs = []
    previews = []

    def finish_dialog():
        """在真实模态循环中编辑表单，并通过指定的用户关闭入口结束窗口。"""
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, CharacterDialog)
            assert dialog.parentWidget() is host
            dialogs.append(dialog)
            previews.append(dialog._thumb_preview)
            if operation == "edit":
                assert not dialog._thumb_preview.pixmap().isNull()
            dialog._name_input.setText("Updated")
            dialog._name_en_input.setText("English")
            if finish == "save":
                dialog._on_ok()
            elif finish == "cancel":
                dialog.reject()
            elif finish == "escape":
                qtbot.keyClick(dialog, Qt.Key.Key_Escape)
            else:
                dialog.close()
        finally:
            if dialog is not None and shiboken6.isValid(dialog) and dialog.isVisible():
                dialog.reject()

    try:
        for _ in range(4):
            QTimer.singleShot(0, finish_dialog)
            if operation == "add":
                page._on_add_character()
            else:
                page._on_edit_character("category")
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()

        if finish == "save":
            expected = {
                "name": "Updated",
                "name_en": "English",
                "node_type": "category",
                "thumbnail": str(thumbnail) if operation == "edit" else "",
            }
            if operation == "add":
                expected["parent_id"] = ""
            assert submitted == [("add" if operation == "add" else "category", expected)] * 4
        else:
            assert submitted == []
        assert len(dialogs) == 4
        assert len({id(dialog) for dialog in dialogs}) == 1, "repeated edits must reuse the same form"
        assert len(host.findChildren(CharacterDialog)) == 1
        assert all(shiboken6.isValid(widget) for widget in dialogs + previews)
        dialog = dialogs[-1]
        assert not dialog.isVisible()
        assert dialog._data is None and dialog.get_result() is None
        assert not dialog._existing_names and not dialog._all_character_names
        assert dialog._thumbnail_path == "" and dialog._thumb_preview.pixmap().isNull()
    finally:
        page.close()


def test_category_form_rebinds_add_edit_and_validation_without_rebuilding(qtbot, qapp, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_characters(
        [{"id": "category", "name": "Category", "name_en": "Original", "parent_id": "", "node_type": "character"}]
    )
    dialogs = []
    submitted = []
    page.character_add_requested.connect(submitted.append)

    def edit_existing():
        """在编辑模式留下未提交草稿，验证下次新建不会沿用。"""
        dialog = QApplication.activeModalWidget()
        try:
            dialogs.append(dialog)
            assert dialog._name_input.text() == "Category"
            assert dialog._name_en_input.text() == "Original"
            assert dialog._type_combo.currentData() == "character"
            assert dialog._type_section.isHidden()
            dialog._name_input.setText("Discarded")
        finally:
            dialog.reject()

    def invalid_new():
        """新建模式使用新类型和空字段；校验失败不影响下一次打开。"""
        dialog = QApplication.activeModalWidget()
        try:
            dialogs.append(dialog)
            assert dialog._name_input.text() == dialog._name_en_input.text() == ""
            assert dialog._type_combo.currentData() == "category"
            assert not dialog._type_section.isHidden()
            assert dialog.get_result() is None
            dialog._on_ok()
            assert dialog.isVisible() and dialog._name_input.styleSheet()
        finally:
            dialog.reject()

    def save_fresh():
        """刷新目录后按当前名称集合校验并提交，不保留之前的错误样式。"""
        dialog = QApplication.activeModalWidget()
        try:
            dialogs.append(dialog)
            assert not dialog._name_input.styleSheet()
            assert "new sibling" in dialog._existing_names
            assert "category" not in dialog._existing_names
            dialog._name_input.setText("Fresh")
            dialog._on_ok()
        finally:
            if dialog.isVisible():
                dialog.reject()

    try:
        QTimer.singleShot(0, edit_existing)
        page._on_edit_character("category")
        QTimer.singleShot(0, invalid_new)
        page._on_add_character()
        page.set_characters([{"id": "sibling", "name": "New sibling", "parent_id": ""}])
        QTimer.singleShot(0, save_fresh)
        page._on_add_character()
        assert len({id(dialog) for dialog in dialogs}) == 1
        assert submitted == [
            {"name": "Fresh", "name_en": "", "node_type": "category", "thumbnail": "", "parent_id": ""}
        ]
    finally:
        page.close()


def test_visible_category_form_is_not_reset_by_a_reentrant_open(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    entered = []

    def dismiss_current():
        """旧实现若进入第二层循环，也能退出并保留可读的失败结果。"""
        active = QApplication.activeModalWidget()
        if active is not None:
            active.reject()

    def reopen():
        """模拟回调重入同一入口，只激活已有表单，不打开第二层模态循环。"""
        dialog = QApplication.activeModalWidget()
        entered.append(dialog)
        dialog._name_input.setText("Draft")
        QTimer.singleShot(0, dismiss_current)
        page._on_add_character()
        assert QApplication.activeModalWidget() is dialog
        assert dialog._name_input.text() == "Draft"
        assert len(page.findChildren(CharacterDialog)) == 1
        dialog.reject()

    try:
        QTimer.singleShot(0, reopen)
        page._on_add_character()
        assert len(entered) == 1
    finally:
        page.close()


def test_destroying_page_releases_its_reusable_category_form(qtbot, qapp, temp_data_dir):
    host = QWidget()
    qtbot.addWidget(host)
    page = OverviewPage(host)
    dialog = None

    def cancel():
        """保留表单包装引用，供宿主页面销毁后核对原生资源。"""
        nonlocal dialog
        dialog = QApplication.activeModalWidget()
        dialog.reject()

    QTimer.singleShot(0, cancel)
    try:
        page._on_add_character()
        assert shiboken6.isValid(dialog)
    finally:
        page.close()
        page.deleteLater()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
    assert shiboken6.isValid(host)
    assert not shiboken6.isValid(dialog)


def test_closed_category_form_updates_theme_and_retires_old_widgets(qtbot, qapp, temp_data_dir):
    previous = theme.current_theme()
    page = None
    try:
        theme.set_theme("light")
        page = OverviewPage()
        qtbot.addWidget(page)
        first = page.prepare_character_dialog()
        assert theme.color("bg.card") in first._thumb_preview.styleSheet()
        theme.set_theme("dark")
        page.retheme()
        current = page.prepare_character_dialog()
        assert theme.color("bg.card") in current._thumb_preview.styleSheet()
        assert not current.isVisible()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
        assert not shiboken6.isValid(first)
        assert page.findChildren(CharacterDialog) == [current]
    finally:
        if page is not None:
            page.close()
        theme.set_theme(previous)
