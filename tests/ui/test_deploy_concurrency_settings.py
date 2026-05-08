from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from src.core.config import Config
from src.ui.pages.settings import SettingsPage


@pytest.fixture
def recommended_hardware(monkeypatch):
    from src.core import deploy_concurrency

    deploy_concurrency.recommended_deploy_concurrency.cache_clear()
    monkeypatch.setattr(deploy_concurrency.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(deploy_concurrency, "_total_memory_bytes", lambda: 16 * 1024**3)
    yield
    deploy_concurrency.recommended_deploy_concurrency.cache_clear()


def test_auto_default_and_manual_edit_persist_without_overwriting_other_settings(
    qtbot,
    temp_data_dir,
    recommended_hardware,
):
    config = Config()
    config.set("mods.delete_method", "recycle_bin")
    before = config._config_path.read_bytes()
    page = SettingsPage()
    qtbot.addWidget(page)

    assert page._deploy_concurrency_input.value() == 4
    assert page._deploy_concurrency_input.isEnabled()
    assert page._deploy_concurrency_recommend_btn.isChecked()
    assert config._config_path.read_bytes() == before, "opening settings must not save a hardware-derived number"

    page._deploy_concurrency_input.setText("12")
    with qtbot.waitSignal(page.settings_changed):
        page._deploy_concurrency_input.editingFinished.emit()
    config.reload()
    assert config.get("mods.deploy_concurrency") == 12
    assert config.get("mods.delete_method") == "recycle_bin"

    reopened = SettingsPage()
    qtbot.addWidget(reopened)
    assert not reopened._deploy_concurrency_recommend_btn.isChecked()
    assert reopened._deploy_concurrency_input.value() == 12

    reopened._deploy_concurrency_recommend_btn.click()
    config.reload()
    assert config.get("mods.deploy_concurrency") == 0
    assert reopened._deploy_concurrency_input.value() == 4
    assert reopened._deploy_concurrency_input.isEnabled()
    assert reopened._deploy_concurrency_recommend_btn.isChecked()


def test_reset_updates_controls_to_auto_without_resaving_manual_value(
    qtbot,
    temp_data_dir,
    recommended_hardware,
    monkeypatch,
):
    config = Config()
    config.set("mods.deploy_concurrency", 12)
    page = SettingsPage()
    qtbot.addWidget(page)

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page._reset_settings()
    config.reload()
    assert config.get("mods.deploy_concurrency") == 0
    assert page._deploy_concurrency_recommend_btn.isChecked()
    assert page._deploy_concurrency_input.value() == 4
    assert page._deploy_concurrency_input.isEnabled()


@pytest.mark.parametrize("retype", [False, True])
def test_deployment_recommendation_becomes_manual_only_after_edit(
    qtbot,
    temp_data_dir,
    recommended_hardware,
    retype,
):
    page = SettingsPage()
    qtbot.addWidget(page)
    page.show()
    page.activateWindow()
    control = page._deploy_concurrency_input
    control.setFocus()
    if control.isEnabled():
        qtbot.waitUntil(control.hasFocus)
    if retype:
        control.selectAll()
        qtbot.keyClicks(control, "4")
    qtbot.keyClick(control, Qt.Key.Key_Tab)
    assert Config().get("mods.deploy_concurrency") == (4 if retype else 0)
    assert page._deploy_concurrency_recommend_btn.isChecked() is not retype
