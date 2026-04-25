from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QPushButton


@pytest.mark.parametrize(
    ("name", "signal"),
    [
        ("categoryPresetImport", "category_preset_import_requested"),
        ("categoryPresetExport", "category_preset_export_requested"),
    ],
)
def test_tools_exposes_direct_category_preset_actions(qtbot, name, signal):
    from src.ui.pages.tools import ToolsPage

    page = ToolsPage()
    qtbot.addWidget(page)
    button = page.findChild(QPushButton, name)
    assert button is not None
    assert page.findChild(QPushButton, "categoryPresetManage") is None
    with qtbot.waitSignal(getattr(page, signal)):
        button.click()


@pytest.mark.parametrize(
    ("game_id", "nodes", "parent_id", "expected"),
    [
        ("", [], "", False),
        ("game_a", [], "", True),
        ("game_a", [{"id": "characters", "name": "角色", "parent_id": ""}], "", False),
        ("game_a", [{"id": "characters", "name": "角色", "parent_id": ""}], "characters", False),
    ],
)
@pytest.mark.parametrize("mode", ["grid", "list"])
def test_empty_game_guide_does_not_appear_for_an_empty_child(
    qtbot, temp_data_dir, game_id, nodes, parent_id, expected, mode
):
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    try:
        page.resize(900, 600)
        page.show()
        page.set_current_game(game_id)
        page.set_characters(nodes)
        page._current_parent_id = parent_id
        page._characters = page._children_map.get(parent_id, [])
        page._set_view(mode)
        page.set_current_mods([], [])
        qtbot.wait(10)
        links = [
            button
            for button in page.findChildren(QPushButton)
            if button.text() == "从分类预设开始" and button.isVisible()
        ]
        assert bool(links) is expected
        if expected:
            assert len(links) == 1
            with qtbot.waitSignal(page.category_preset_import_requested):
                links[0].click()
    finally:
        page.close()


@pytest.mark.parametrize("current_game", ["game_a", "game_b"])
def test_import_completion_schedules_only_its_target_game_once(current_game):
    from src.ui.main_window import MainWindow

    refreshed = []
    window = SimpleNamespace(
        _game_service=SimpleNamespace(current_game_id=current_game),
        _library_probe=None,
        _mod_manager=SimpleNamespace(invalidate_operation_trust=lambda: None),
        _reload_overview_level=lambda game_id: refreshed.append(game_id),
    )
    MainWindow._on_category_preset_imported(window, "game_a")
    assert refreshed == (["game_a"] if current_game == "game_a" else [])


def test_startup_recovery_notice_reports_retained_paths_once(qtbot, tmp_path):
    from PySide6.QtWidgets import QMainWindow, QMessageBox

    from src.ui.main_window import MainWindow

    window = QMainWindow()
    qtbot.addWidget(window)
    path = str(tmp_path / "pending-category")
    window._mod_manager = SimpleNamespace(category_preset_recovery_errors=(path,))
    MainWindow._show_category_preset_recovery_notice(window)
    notice = window.findChild(QMessageBox, "categoryPresetRecoveryNotice")
    assert notice is not None and notice.isVisible()
    assert path in notice.detailedText()
    assert "恢复" in notice.text() and "保护" in notice.informativeText()
    MainWindow._show_category_preset_recovery_notice(window)
    assert len(window.findChildren(QMessageBox, "categoryPresetRecoveryNotice")) == 1
    notice.close()
