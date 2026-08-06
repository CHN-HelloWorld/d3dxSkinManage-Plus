from threading import Event

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget

from src.ui.acrylic import apply_dialog_acrylic
from src.ui.operation_check import run_file_check
from tests.core.test_library_reconcile import mm as mm


def test_dialog_does_not_make_owner_children_native(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    layout = QVBoxLayout(owner)
    buttons = [QPushButton(str(i)) for i in range(100)]
    for button in buttons:
        layout.addWidget(button)
    owner.show()
    dialog = QDialog(buttons[0])
    apply_dialog_acrylic(dialog, animate=False)
    dialog.show()
    assert not any(b.testAttribute(Qt.WidgetAttribute.WA_NativeWindow) for b in buttons)


def test_file_check_keeps_owner_enabled_and_blocks_input(qtbot):
    from PySide6.QtTest import QTest

    owner = QWidget()
    qtbot.addWidget(owner)
    button = QPushButton("edit", owner)
    owner.show()
    clicked, states = [], []
    button.clicked.connect(lambda: clicked.append(True))
    release = Event()

    def inspect():
        states.append(owner.isEnabled())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        release.set()

    QTimer.singleShot(20, inspect)
    assert run_file_check(lambda: release.wait(2), owner)
    assert states == [True]
    assert clicked == []
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert clicked == [True]


def test_commit_duplicate_is_skipped_not_failed(qtbot, tmp_path):
    from src.core.mod_intake import DuplicateIntakeError
    from src.ui.dialogs.import_progress import ImportProgressDialog
    from src.ui.widgets.task_center import ST_ROLLED_BACK

    class Manager:
        def _get_character_library(self, *args, **kwargs):
            return tmp_path

    manager = Manager()
    dialog = ImportProgressDialog([("source",)], "node", manager)
    qtbot.addWidget(dialog)
    states = []
    dialog._mark_view_item = lambda *args: states.append(args)

    def duplicate(result):
        raise DuplicateIntakeError("content already exists: sample")

    dialog._intake_service.commit = duplicate
    # 通过已准备结果验证实际提交异常处理。
    from src.core.mod_intake import PreparedIntake

    prepared = object.__new__(PreparedIntake)
    dialog._on_file_prepared(0, prepared)
    assert dialog._skipped_duplicates == 1
    assert dialog._failed_indexes == set()
    assert states[0][1] == ST_ROLLED_BACK


def test_dialog_prepared_effects_are_not_reapplied_at_first_show(qtbot, monkeypatch):
    from src.ui import acrylic

    calls = []
    monkeypatch.setattr(acrylic, "_apply_window_acrylic_effects", lambda *a, **kw: calls.append(True))
    dialog = QDialog()
    qtbot.addWidget(dialog)
    apply_dialog_acrylic(dialog, animate=False)
    assert len(calls) == 1
    dialog.show()
    assert len(calls) == 1


def test_decorating_prepared_dialog_does_not_reset_native_flags(qtbot, monkeypatch):
    from src.ui.dialog_chrome import decorate_dialog

    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    dialog.winId()
    calls = []
    monkeypatch.setattr(dialog, "setWindowFlag", lambda *args: calls.append(args))
    decorate_dialog(dialog)
    assert calls == []


def test_explicit_acrylic_prepares_caption_before_realizing_window(qtbot):
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setFixedHeight(200)
    apply_dialog_acrylic(dialog, animate=False)
    assert dialog.property("dialogFrameInstalled")
    assert dialog.height() == 233


def test_two_prepared_copies_import_once_and_report_duplicate(qtbot, mm, tmp_path):
    from src.ui.dialogs.import_progress import ImportProgressDialog
    from src.ui.widgets.task_center import ST_DONE, ST_ROLLED_BACK

    node = mm.add_character("Target")
    source = tmp_path / "incoming"
    source.mkdir()
    (source / "mod.ini").write_text("[KeySwap]\nkey = p\n", encoding="utf8")
    dialog = ImportProgressDialog([(str(source), "A"), (str(source), "B")], node.id, mm)
    qtbot.addWidget(dialog)
    service = dialog._intake_service
    requests = [
        service.plan_request(
            source_path=source, game_id=mm.current_game_id, node_id=node.id, display_name=name, target_format="zip"
        )
        for name in ("A", "B")
    ]
    # 两个后台任务基于同一初始索引准备，第二项在提交时才发现重复。
    prepared = [service.prepare(request) for request in requests]
    states = []
    dialog._mark_view_item = lambda *args: states.append(args)
    for index, result in enumerate(prepared):
        dialog._on_file_prepared(index, result)
    assert [state[1] for state in states] == [ST_DONE, ST_ROLLED_BACK]
    assert dialog._failed_indexes == set()
    assert dialog._skipped_duplicates == 1
    assert len(mm.get_mods(node.id)) == 1
