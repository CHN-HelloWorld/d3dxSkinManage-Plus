from __future__ import annotations

from src.core.rebuild_session import RebuildSession
from src.ui.dialogs.rebuild_progress import RebuildProgressDialog
from src.ui.main_window import MainWindow
from tests.core.test_rebuild_index import mm as rebuild_mm

mm = rebuild_mm


def test_rebuild_dialog_preserves_invalid_metadata_in_report(mm, qtbot, monkeypatch):
    prepare = RebuildSession.prepare

    def with_unreadable(session, progress):
        prepare(session, progress)
        session.invalid.append("C:/mods/Broken")

    monkeypatch.setattr(RebuildSession, "prepare", with_unreadable)
    dialog = RebuildProgressDialog(mm, "game_a")
    qtbot.addWidget(dialog)

    dialog._start()
    qtbot.waitUntil(lambda: dialog._finished)
    assert dialog.report.invalid_metadata == ["C:/mods/Broken"]


def test_rebuild_summary_lists_unique_invalid_paths_and_overflow():
    paths = [f"C:/mods/Broken-{index}" for index in range(6)]
    paths.append(paths[0])

    summary = MainWindow._invalid_metadata_summary(paths)

    assert "未恢复的损坏 Mod 容器（6 个）" in summary
    assert summary.count(paths[0]) == 1
    assert "另有 1 个，详见日志" in summary
