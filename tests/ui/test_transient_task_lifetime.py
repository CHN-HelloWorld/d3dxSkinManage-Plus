"""可复用编辑器的确认框有界，重建任务仅在结果结算和线程退场后释放窗口。"""

from __future__ import annotations

from threading import Event

import pytest
import shiboken6
from PySide6.QtWidgets import QDialog, QMessageBox

from src.core.rebuild_session import RebuildSession
from src.models.mod import Mod
from src.ui.dialogs.mod_edit import ModEditDialog, _DiscardDialog
from src.ui.dialogs.rebuild_progress import RebuildProgressDialog
from src.ui.main_window import MainWindow
from tests.core.test_rebuild_index import _make_deployable_mod
from tests.core.test_rebuild_index import mm as mm
from tests.qt_dialogs import interact
from tests.ui.test_transient_ui_reuse import _flush
from tests.ui.test_transient_ui_reuse import host as host


@pytest.mark.parametrize("finish", ["save", "discard"])
def test_mod_edit_reuses_discard_confirmation_until_next_mod(qtbot, qapp, host, finish):
    editor = ModEditDialog(Mod(name="original"), host)
    editor._name_input.setText("changed")
    confirmations = []
    finished = []
    editor.finished.connect(finished.append)

    def inspect(dialog):
        """继续编辑不得丢失字段，重复打开同一确认框只能连接一次放弃槽。"""
        for _ in range(4):

            def continue_editing(confirmation):
                """使用真实继续按钮关闭确认框，不改变编辑器的草稿。"""
                confirmations.append(confirmation)
                confirmation.continue_button.click()

            with interact(_DiscardDialog, continue_editing):
                dialog.reject()
                qtbot.waitUntil(lambda: not dialog.discard_dialog.isVisible())
            assert dialog.isVisible()
            assert dialog._name_input.text() == "changed"
        if finish == "save":
            dialog._on_save()
        else:
            with interact(_DiscardDialog, lambda confirmation: confirmation.discard_button.click()):
                dialog.reject()
                qtbot.waitUntil(lambda: not dialog.isVisible())

    with interact(ModEditDialog, inspect):
        result = editor.exec()
    assert result == (QDialog.DialogCode.Accepted if finish == "save" else QDialog.DialogCode.Rejected)
    assert finished == [result]
    assert len({id(confirmation) for confirmation in confirmations}) == 1
    assert len(editor.findChildren(_DiscardDialog)) == 1
    if finish == "save":
        assert editor.get_result()["name"] == "changed"
    editor.bind_mod(Mod(name="next"))
    _flush(qapp)
    assert not editor.findChildren(_DiscardDialog)
    assert editor.get_result() is None
    assert editor._name_input.text() == "next"


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_rebuild_entrypoint_releases_dialog_after_preserving_result(qtbot, qapp, host, mm, monkeypatch, outcome):
    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    host._mod_manager = mm
    host._invalid_metadata_summary = MainWindow._invalid_metadata_summary
    host._load_characters = lambda *_args, **_kwargs: None
    host._refresh_overview_mods = lambda: None
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    messages = []

    def report(_parent, _title, text):
        """消费复制出的结果时处理延迟销毁，检验报告不再依赖原生窗口。"""
        _flush(qapp)
        messages.append(text)

    monkeypatch.setattr(QMessageBox, "warning", report)
    monkeypatch.setattr(QMessageBox, "information", report)
    ready, release = Event(), Event()
    original_prepare = RebuildSession.prepare

    def prepare(session, progress):
        """仅注入受控错误或取消屏障，成功分支保留完整的隔离索引重建。"""
        if outcome == "failed":
            raise OSError("sample rebuild failure")
        original_prepare(session, progress)
        if outcome == "cancelled":
            ready.set()
            release.wait(5)
            session.check_cancel()

    monkeypatch.setattr(RebuildSession, "prepare", prepare)
    dialogs = []

    def inspect(dialog):
        """活动线程仍在时取消只能挂起关闭，线程结束后才允许模态循环退出。"""
        dialogs.append(dialog)
        if outcome == "cancelled":
            qtbot.waitUntil(ready.is_set)
            dialog.reject()
            assert dialog.isVisible()
            assert dialog._worker.isRunning()
            assert shiboken6.isValid(dialog)
            release.set()
        qtbot.waitUntil(lambda: dialog._finished)
        assert dialog._worker is None or not dialog._worker.isRunning()

    try:
        with interact(RebuildProgressDialog, inspect):
            MainWindow._on_rebuild_index(host)
    finally:
        release.set()
        for dialog in dialogs:
            if shiboken6.isValid(dialog) and dialog._worker is not None:
                dialog._worker.wait(5000)
    _flush(qapp)
    assert len(dialogs) == 1
    assert not shiboken6.isValid(dialogs[0])
    assert not host.findChildren(RebuildProgressDialog)
    assert len(messages) == 1
    if outcome == "completed":
        assert "登记 1 个 Mod" in messages[0]
        assert len(mm.get_mods()) == 1
    else:
        assert ("取消" if outcome == "cancelled" else "sample rebuild failure") in messages[0]
        assert mm.get_mods() == []
