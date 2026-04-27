from __future__ import annotations

import json

from PySide6.QtWidgets import QPushButton

from src.core.config import Config
from src.ui.pages.settings import SettingsPage
from tests.core.test_deploy_concurrency import hardware as hardware


def test_recommended_import_workers_persist_after_restart(qtbot, temp_data_dir, hardware):
    hardware(16, 32)
    config = Config()
    config.set("import.parallel_workers", 3)
    config.set("mods.delete_method", "recycle_bin")
    before = config._config_path.read_bytes()
    page = SettingsPage()
    qtbot.addWidget(page)

    assert page._parallel_spin.value() == 3
    assert config._config_path.read_bytes() == before

    recommend_button = page._parallel_spin.parentWidget().findChild(QPushButton)
    assert recommend_button is not None
    with qtbot.waitSignal(page.settings_changed):
        recommend_button.click()

    assert page._parallel_spin.value() == 8
    saved = json.loads(config._config_path.read_text(encoding="utf-8"))
    assert saved["import"]["parallel_workers"] == 0
    assert config.get("import.parallel_workers") == 0
    assert saved["mods"]["delete_method"] == "recycle_bin"

    page.close()
    hardware(4, 8)
    Config._instance = None
    reopened = SettingsPage()
    qtbot.addWidget(reopened)
    assert reopened._parallel_spin.value() == 2


def test_manual_import_workers_persist_after_edit(qtbot, temp_data_dir):
    config = Config()
    page = SettingsPage()
    qtbot.addWidget(page)

    page._parallel_spin.setText("6")
    with qtbot.waitSignal(page.settings_changed):
        page._parallel_spin.editingFinished.emit()

    config.reload()
    assert config.get("import.parallel_workers") == 6
    reopened = SettingsPage()
    qtbot.addWidget(reopened)
    assert reopened._parallel_spin.value() == 6
