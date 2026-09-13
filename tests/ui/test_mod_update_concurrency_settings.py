from __future__ import annotations

import json

from PySide6.QtWidgets import QMessageBox, QWidget

from src.core.config import Config
from src.ui.main_window import MainWindow
from src.ui.pages.settings import SettingsPage
from tests.core.test_deploy_concurrency import hardware as hardware
from tests.core.test_mod_update_concurrency import controlled_updates as controlled_updates
from tests.core.test_mod_update_pipeline import controlled_pipeline as controlled_pipeline
from tests.core.test_mod_update_pipeline import several_updates as several_updates
from tests.core.test_mod_update_pipeline import update_case as update_case
from tests.core.test_mod_update_pipeline import update_service as update_service


def test_default_update_limit_is_recommended_without_rewriting_preferences(qtbot, temp_data_dir, hardware):
    hardware(8, 16)
    path = temp_data_dir / "config" / "settings.json"
    original = '{"import": {"parallel_workers": 6}}'
    path.write_text(original, encoding="utf-8")
    page = SettingsPage()
    qtbot.addWidget(page)

    assert hasattr(page, "_upd_concurrency_input"), "Mod update concurrency needs a settings control"
    assert page._upd_concurrency_input.value() == 4
    assert path.read_text(encoding="utf-8") == original


def test_manual_and_recommended_update_limits_survive_restart(qtbot, temp_data_dir, hardware):
    hardware(8, 16)
    config = Config()
    config.set("gamebanana.update_check.concurrency", 1)
    config.set("import.parallel_workers", 6)
    page = SettingsPage()
    qtbot.addWidget(page)
    assert hasattr(page, "_upd_concurrency_input"), "Mod update concurrency needs a settings control"
    assert page._upd_concurrency_input.value() == 1

    page._upd_concurrency_input.setText("7")
    with qtbot.waitSignal(page.settings_changed):
        page._upd_concurrency_input.editingFinished.emit()
    saved = json.loads(config._config_path.read_text(encoding="utf-8"))
    assert saved["gamebanana"]["update_check"]["concurrency"] == 7
    assert saved["import"]["parallel_workers"] == 6

    page.close()
    Config._instance = None
    reopened = SettingsPage()
    qtbot.addWidget(reopened)
    assert reopened._upd_concurrency_input.value() == 7
    with qtbot.waitSignal(reopened.settings_changed):
        reopened._upd_concurrency_recommend_btn.click()
    assert reopened._upd_concurrency_input.value() == 4
    reopened.close()

    hardware(4, 8)
    Config._instance = None
    automatic = SettingsPage()
    qtbot.addWidget(automatic)
    assert automatic._upd_concurrency_input.value() == 2, "automatic selection must survive a device change"
    assert Config().get("gamebanana.update_check.concurrency") == 0
    assert Config().get("import.parallel_workers") == 6


def test_reset_restores_recommended_update_limit(qtbot, temp_data_dir, hardware, monkeypatch):
    hardware(4, 8)
    config = Config()
    config.set("gamebanana.update_check.concurrency", 7)
    page = SettingsPage()
    qtbot.addWidget(page)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page._reset_settings()
    config.reload()
    assert config.get("gamebanana.update_check.concurrency") == 0
    assert page._upd_concurrency_input.value() == 2


class _SettingsHost(QWidget):
    _on_settings_changed = MainWindow._on_settings_changed

    def __init__(self, service):
        super().__init__()
        self._mod_update_service = service

    def _start_pending_deploys(self):
        pass

    def _apply_settings_reconcile(self):
        pass


def test_settings_adjust_running_update_queue_without_interrupting_downloads(qtbot, controlled_updates, hardware):
    hardware(8, 16)
    case = controlled_updates
    case.config.set("gamebanana.update_check.concurrency", 1)
    page = SettingsPage()
    host = _SettingsHost(case.service)
    qtbot.addWidget(page)
    qtbot.addWidget(host)
    page.settings_changed.connect(host._on_settings_changed)
    case.start(8)
    assert [mid for mid, state in case.service.update_task_snapshots().items() if state["phase"] == "downloading"] == [
        "0"
    ]

    page._upd_concurrency_input.setText("3")
    page._upd_concurrency_input.editingFinished.emit()
    assert [mid for mid, state in case.service.update_task_snapshots().items() if state["phase"] == "downloading"] == [
        "0",
        "1",
        "2",
    ]

    page._upd_concurrency_input.setText("1")
    page._upd_concurrency_input.editingFinished.emit()
    assert all(case.downloads.tasks[fid].state == "downloading" for fid in (1, 2, 3))
    for fid in (1, 2):
        case.downloads.fail(fid)
        assert case.service.update_task_snapshots()["3"]["phase"] == "queued"
    case.downloads.fail(3)
    assert case.service.update_task_snapshots()["3"]["phase"] == "downloading"

    page._upd_concurrency_recommend_btn.click()
    assert [mid for mid, state in case.service.update_task_snapshots().items() if state["phase"] == "downloading"] == [
        "3",
        "4",
        "5",
        "6",
    ]
    assert case.service.update_task_snapshots()["7"]["phase"] == "queued"


def test_import_worker_setting_resizes_only_import_queue(qtbot, controlled_pipeline):
    case = controlled_pipeline
    case.downloads.cached = True
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    page = SettingsPage()
    host = _SettingsHost(case.service)
    qtbot.addWidget(page)
    qtbot.addWidget(host)
    page.settings_changed.connect(host._on_settings_changed)
    case.start()
    assert case.phases() == ["extracting", "queued", "queued", "queued"]

    page._upd_concurrency_input.setText("4")
    page._upd_concurrency_input.editingFinished.emit()
    assert case.phases() == ["extracting", "queued", "queued", "queued"]
    page._parallel_spin.setText("3")
    page._parallel_spin.editingFinished.emit()
    assert case.phases() == ["extracting", "extracting", "extracting", "queued"]
    assert case.config.get("gamebanana.update_check.concurrency") == 4

    page._parallel_spin.setText("1")
    page._parallel_spin.editingFinished.emit()
    assert case.phases() == ["extracting", "extracting", "extracting", "queued"]
    for index in (0, 1):
        case.processes[case.mods[index].mod_id].finish()
        assert case.phases()[3] == "queued"
    case.processes[case.mods[2].mod_id].finish()
    assert case.phases() == ["done", "done", "done", "extracting"]


def test_automatic_import_workers_use_recommendation_for_update_imports(controlled_pipeline, hardware):
    hardware(4, 8)
    case = controlled_pipeline
    case.downloads.cached = True
    case.config.set("import.parallel_workers", 0)
    case.start()
    assert case.phases() == ["extracting", "extracting", "queued", "queued"]
