from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPlainTextEdit, QPushButton, QWidget

from src.ui.dialogs.game_delete import GameDeleteDialog, GameDeleteReportDialog
from src.ui.pages.game import GamePage
from tests.qt_dialogs import interact
from tests.ui.test_transient_ui_reuse import _flush


@pytest.mark.parametrize("action", ["enter", "escape", "close"])
def test_delete_summary_never_confirms_by_default(qtbot, action):
    from src.ui.dialogs.game_delete import GameDeleteDialog

    dialog = GameDeleteDialog(
        "示例游戏甲",
        dict(
            mods=120,
            characters=25,
            presets=3,
            deployed=8,
            library_size=1536 * 1024 * 1024,
            library_dir="D:/ModLibrary/ExampleGameA",
        ),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(20)
    if action == "enter":
        qtbot.keyClick(dialog, Qt.Key.Key_Return)
    elif action == "escape":
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        dialog.findChild(QPushButton, "dialogClose").click()
    qtbot.waitUntil(lambda: not dialog.isVisible())
    assert dialog.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize(
    "second_answer, expected",
    [
        (QMessageBox.StandardButton.No, False),
        (QMessageBox.StandardButton.Yes, True),
    ],
)
def test_game_page_keeps_second_confirmation_after_summary(
    qtbot,
    temp_data_dir,
    monkeypatch,
    second_answer,
    expected,
):
    from src.core.mod_manager import ModManager
    from src.ui.dialogs.game_delete import GameDeleteDialog

    page = QWidget()
    qtbot.addWidget(page)
    page._svc = SimpleNamespace(get_resource_dir=lambda gid: "game_a")
    info = dict(
        mods=1, characters=1, presets=0, deployed=0, library_size=2048, library_dir="D:/ModLibrary/ExampleGameA"
    )
    monkeypatch.setattr(ModManager, "describe_game_data", lambda *args, **kwargs: info)

    # 显示真实删除摘要，只点击明确的删除按钮。
    def choose_delete():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, GameDeleteDialog)
        button = dialog.findChild(QPushButton, "confirmGameDelete")
        if button is None:
            QTimer.singleShot(10, choose_delete)
        else:
            button.click()

    answers = []

    def answer(parent, title, text, buttons, default):
        answers.append(default)
        return second_answer

    monkeypatch.setattr(QMessageBox, "question", answer)
    QTimer.singleShot(50, choose_delete)
    assert GamePage._confirm_delete(page, SimpleNamespace(id="game_a", name="示例游戏甲")) is expected
    assert answers == [QMessageBox.StandardButton.No]


def test_game_delete_confirmation_consumes_scan_result_then_releases_dialog(qapp, qtbot, temp_data_dir, monkeypatch):
    from src.core.mod_manager import ModManager

    info = dict(mods=0, characters=0, presets=0, deployed=0, library_size=0, library_dir="")
    monkeypatch.setattr(ModManager, "describe_game_data", lambda *_args, **_kwargs: info)
    host = QWidget()
    host._svc = SimpleNamespace(get_resource_dir=lambda _game_id: "game_a")
    dialogs = []

    def confirm(dialog):
        """等待真实扫描线程发布摘要，再点击明确删除按钮返回结果。"""
        dialogs.append(dialog)
        qtbot.waitUntil(lambda: dialog.findChild(QPushButton, "confirmGameDelete") is not None)
        dialog.findChild(QPushButton, "confirmGameDelete").click()

    try:
        for _ in range(4):
            with interact(GameDeleteDialog, confirm):
                assert GamePage._confirm_delete(host, SimpleNamespace(id="game_a", name="示例游戏甲"))
            _flush(qapp)

        assert not host.findChildren(GameDeleteDialog)
        assert all(not shiboken6.isValid(dialog) for dialog in dialogs)
    finally:
        host.deleteLater()
        _flush(qapp)


def test_game_delete_receipt_is_released_after_user_closes_it(qapp, temp_data_dir, monkeypatch):
    from src.core.mod_manager import ModManager

    monkeypatch.setattr(
        ModManager,
        "purge_game_data",
        lambda *_args, **_kwargs: {"errors": ["locked"], "deployed_removed": 0, "presets": 0},
    )
    host = QWidget()
    game = SimpleNamespace(id="game_a", name="示例游戏甲")
    host._selected_id = ""
    host._svc = SimpleNamespace(
        get_game=lambda _game_id: game,
        current_game_id="other",
        get_resource_dir=lambda _game_id: "game_a",
        delete_game=lambda _game_id: None,
    )
    host._confirm_delete = lambda _game: True
    host.reload = lambda: None
    host.games_changed = SimpleNamespace(emit=lambda: None)
    host.game_changed = SimpleNamespace(emit=lambda _game_id: None)
    dialogs = []

    def close(dialog):
        """记录真实清理报告并通过关闭路径结束模态循环。"""
        dialogs.append(dialog)
        dialog.reject()

    try:
        for _ in range(4):
            with interact(GameDeleteReportDialog, close):
                GamePage._on_delete(host, "game_a")
            _flush(qapp)

        assert not host.findChildren(GameDeleteReportDialog)
        assert all(not shiboken6.isValid(dialog) for dialog in dialogs)
    finally:
        host.deleteLater()
        _flush(qapp)


def test_cleanup_receipt_keeps_all_remaining_paths_copyable(qtbot):
    from src.ui.dialogs.game_delete import GameDeleteReportDialog

    paths = [f"D:/ModLibrary/角色{i}/未能清理" for i in range(12)]
    dialog = GameDeleteReportDialog("示例游戏甲", dict(errors=paths, deployed_removed=8, presets=3))
    qtbot.addWidget(dialog)
    details = dialog.findChild(QPlainTextEdit)
    assert details.isReadOnly()
    details.selectAll()
    assert details.textCursor().selectedText().split("\u2029\u2029") == paths


@pytest.mark.parametrize("action", ["cancel", "escape", "close"])
def test_game_scan_cancel_never_reaches_destructive_confirmation(qtbot, action):
    from threading import Event

    from src.core.game_data_summary import GameDataScanCancelled
    from src.ui.dialogs.game_delete import GameDeleteDialog

    started, finish = Event(), Event()

    def scan(*, cancelled, progress):
        progress("统计容量", 20, 100, 1024)
        started.set()
        finish.wait(5)
        if cancelled():
            raise GameDataScanCancelled()
        return {}

    dialog = GameDeleteDialog("示例游戏甲", None, scan=scan)
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        qtbot.waitUntil(started.is_set)
        qtbot.waitUntil(lambda: dialog._scan_progress.value() == 20)
        assert dialog.findChild(QPushButton, "confirmGameDelete") is None
        if action == "cancel":
            dialog.cancel_button.click()
        elif action == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            dialog.findChild(QPushButton, "dialogClose").click()
        assert dialog._scan.cancelled.is_set()
        finish.set()
        qtbot.waitUntil(lambda: not dialog.isVisible())
        assert dialog.info is None
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert dialog.findChild(QPushButton, "confirmGameDelete") is None
    finally:
        finish.set()
        qtbot.waitUntil(lambda: dialog._scan is None)


def test_destroying_scan_dialog_cancels_and_retires_its_worker(qtbot):
    from threading import Event

    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    from src.core.game_data_summary import GameDataScanCancelled
    from src.ui.dialogs.game_delete import GameDeleteDialog

    started, stopped = Event(), Event()

    def scan(*, cancelled, progress):
        started.set()
        while not cancelled():
            Event().wait(0.01)
        stopped.set()
        raise GameDataScanCancelled()

    dialog = GameDeleteDialog("示例游戏甲", None, scan=scan)
    dialog.show()
    qtbot.waitUntil(started.is_set)
    worker = dialog._scan
    try:
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qtbot.waitUntil(stopped.is_set)
    finally:
        if shiboken6.isValid(worker):
            worker.stop()
            worker.wait(5000)
