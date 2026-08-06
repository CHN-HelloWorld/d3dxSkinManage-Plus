from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, QPropertyAnimation, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from src.models.mod import Mod, ModInfo
from src.models.tag import TagDraft
from src.ui import anim, theme
from src.ui.dialogs.mod_edit import ModEditDialog
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from src.ui.pages.overview import BatchAddCharacterDialog
from src.ui.widgets.tag_picker import TagPicker
from tests.ui.test_card_recycling import _prepare, _scroll


def test_edit_dialog_rebind_keeps_controls_and_clears_previous_draft(qtbot):
    first = Mod(mod_id="first", name="First", path="first.zip", info=ModInfo(tags=["Old"]))
    second = Mod(mod_id="second", name="Second", path="second.zip", info=ModInfo(author="New", tags=["Current"]))
    dialog = ModEditDialog(first)
    qtbot.addWidget(dialog)
    controls = (dialog._name_input, dialog._form_scroll, dialog.tag_picker, dialog._save_button)
    dialog.show()
    dialog._name_input.setText("Unsaved")
    dialog.tag_picker.input.setText("unfinished")
    dialog._info_toggle.setChecked(True)
    dialog._sync_folder_checkbox.setChecked(False)
    dialog._save_error.setText("Previous error")
    dialog._save_error.show()
    dialog.set_save_handler(lambda *_args: pytest.fail("old save handler was retained"))
    dialog.done(0)

    dialog.bind_mod(second)

    assert controls == (dialog._name_input, dialog._form_scroll, dialog.tag_picker, dialog._save_button)
    assert dialog._name_input.text() == "Second"
    assert dialog._author_input.text() == "New"
    assert dialog._path_label.text() == "second.zip"
    assert "Second" in dialog.windowTitle()
    assert dialog.tag_picker.draft.mod_id == "second"
    assert dialog.tag_picker.draft.names == ["Current"]
    assert not dialog.tag_picker.input.text()
    assert not dialog.tag_picker.is_dirty
    assert not dialog._info_toggle.isChecked()
    assert dialog._sync_folder_checkbox.isChecked()
    assert dialog._save_error.isHidden()
    assert dialog._save_handler is None
    assert dialog.get_result() is None
    assert dialog._collect_fields() == dialog._original_fields
    dialog.show()
    dialog.reject()
    assert not dialog.isVisible(), "a freshly rebound dialog has no unsaved old fields"


def test_update_dialog_reopens_with_same_static_controls_and_fresh_input(qtbot, temp_data_dir):
    from src.core.config import Config
    from tests.ui.test_mod_updates_experience import make_host

    host = make_host(qtbot, Config())
    host._open_mod_updates_center()
    dialog = host._mod_updates_dialog
    search = dialog._search
    search.setText("previous query")
    dialog.show_message("Previous undo", undo=lambda: pytest.fail("stale undo"))
    dialog.reject()
    assert host._mod_updates_dialog is None
    host._open_mod_updates_center()

    assert host._mod_updates_dialog is dialog
    assert dialog._search is search
    assert search.text() == ""
    assert dialog._message.isHidden()
    assert dialog._undo is None
    dialog._recheck_btn.click()
    assert host._mod_update_service.requests == 1
    host._mod_update_service.finish()
    dialog.force_close()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_update_close_icon_recovers_when_hidden_without_a_leave_event(qtbot, qapp, temp_data_dir, mode):
    from src.core.config import Config
    from tests.ui.test_mod_updates_experience import make_host

    previous = theme.current_theme()
    theme.set_theme(mode)
    host = make_host(qtbot, Config())
    host._open_mod_updates_center()
    dialog = host._mod_updates_dialog
    close = dialog._close_btn

    def icon_image():
        return close.icon().pixmap(close.iconSize()).toImage()

    try:
        normal = icon_image()
        assert any(normal.pixelColor(x, y).alpha() > 200 for x in range(normal.width()) for y in range(normal.height()))
        for _ in range(2):
            # Windows 隐藏被点击的标题按钮时可能不发送 Leave。
            qapp.sendEvent(close, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
            assert icon_image() != normal
            close.click()
            assert not dialog.isVisible()
            host._open_mod_updates_center()
            assert host._mod_updates_dialog is dialog
            assert icon_image() == normal, "a reopened X must contrast with the normal titlebar"
            qapp.sendEvent(close, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
            qapp.sendEvent(close, QEvent(QEvent.Type.Leave))
            assert icon_image() == normal
    finally:
        dialog.force_close()
        theme.set_theme(previous)


def test_directory_refresh_reuses_cards_and_rebinds_their_actions(qtbot, temp_data_dir, request):
    page = _prepare(qtbot, "overview", request)
    cards = set(page._vs_card_cache.values())
    for idle in page._vs_card_pool._idle.values():
        cards.update(idle)
    old_ids = set(page._vs_card_cache)
    replacements = [Mod(mod_id=f"replacement-{i:04}", name=f"Next {i:04}") for i in range(800)]

    page.set_current_mods(replacements, replacements)
    _scroll(page, 20)

    assert set(page._vs_card_cache).isdisjoint(old_ids)
    assert set(page._vs_card_cache.values()) <= cards, "directory refresh must reuse the viewport pool"
    assert all(shiboken6.isValid(card) and card.parentWidget() is page._scroll_content for card in cards)
    current_id, card = next(iter(page._vs_card_cache.items()))
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert page._selected_ids == {current_id}
    assert page._selected_mod.mod_id == current_id


def test_hidden_tag_candidates_wait_for_the_first_open(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(520, 600)
    draft = TagDraft.from_mod(Mod(name="Test", info=ModInfo(tags=["First", "Second"])))
    picker = TagPicker(draft, host)
    QVBoxLayout(host).addWidget(picker)
    assert picker.findChildren(QFrame, "tag-candidate-row") == []
    picker.set_draft(TagDraft.from_mod(Mod(info=ModInfo(tags=["Current"]))))
    assert host.findChildren(QFrame, "tag-candidate-row") == []

    host.show()
    picker.input.setFocus()
    picker.input.setText("Added")
    picker.show_popup()
    assert picker.popup.isVisible()
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Current", "Added"]


def test_search_waits_for_the_requested_subtree(qtbot, temp_data_dir):
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    requested = []
    page.set_mod_loader(lambda: requested.append(page._search_input.text()))
    page._search_input.setText("needle")
    page._search_timer.stop()
    page._execute_search_refresh(force_text="needle")
    assert requested == ["needle"]
    assert page._status_label.text() == "正在检索中..."
    direct = Mod(mod_id="direct", name="Direct")
    nested = Mod(mod_id="nested", name="needle")
    page.set_current_mods([direct], [direct, nested])
    assert [item["id"] for item in page._display_items] == ["nested"]
    page.close()


def test_opaque_update_dialog_keeps_its_native_surface_during_polish(qtbot, qapp):
    from src.ui.dialog_chrome import DialogFrameManager

    manager = DialogFrameManager(qapp)
    qapp.installEventFilter(manager)
    dialog = ModUpdatesDialog([])
    qtbot.addWidget(dialog)
    try:
        dialog.ensurePolished()
        assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        dialog.show()
        assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert not dialog.property("_dialog_acrylic")
    finally:
        dialog.done(0)
        qapp.removeEventFilter(manager)


@pytest.mark.parametrize("kind", ["edit", "updates", "batch"])
def test_dialog_intro_keeps_input_ready_and_settles(qtbot, kind):
    previous = anim.is_enabled()
    anim.set_enabled(True)
    dialog = None
    try:
        if kind == "edit":
            dialog = ModEditDialog(Mod(name="Ready"))
            field = dialog._name_input
        elif kind == "updates":
            dialog = ModUpdatesDialog([])
            field = dialog._search
        else:
            dialog = BatchAddCharacterDialog()
            field = dialog._entries[0]["name_input"]
        qtbot.addWidget(dialog)
        dialog.show()
        shown_size = dialog.size()
        assert dialog.windowOpacity() == 1.0
        motions = [
            animation
            for animation in dialog.findChildren(QPropertyAnimation)
            if animation.targetObject() is dialog
            and bytes(animation.propertyName()) in (b"pos", b"geometry", b"windowOpacity")
            and animation.state() == QAbstractAnimation.State.Running
        ]
        # 对话框以十六像素滑入，待动画稳定后检查最终状态。
        assert len(motions) == 1
        motion = motions[0]
        assert bytes(motion.propertyName()) == b"geometry"
        start, target = motion.startValue(), motion.endValue()
        assert start.size() == target.size()
        assert target.topLeft() - start.topLeft() == QPoint(0, 16)
        assert motion.duration() == 150
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, "Ready")
        assert field.text() == "Ready"
        qtbot.waitUntil(
            lambda: not shiboken6.isValid(motion) or motion.state() == QAbstractAnimation.State.Stopped, timeout=1000
        )
        assert dialog.windowOpacity() == 1.0
        # 显示时按屏幕范围限制尺寸，可能缩小请求宽度。
        assert dialog.pos() == target.topLeft()
        assert dialog.size() == shown_size
    finally:
        anim.set_enabled(False)
        if dialog is not None and shiboken6.isValid(dialog):
            # 断言失败也会触发延迟销毁，须先停止已启动动画；全局开关不影响正在执行的任务。
            for motion in dialog.findChildren(QAbstractAnimation):
                if shiboken6.isValid(motion):
                    motion.stop()
            dialog.done(0)
        anim.set_enabled(previous)
