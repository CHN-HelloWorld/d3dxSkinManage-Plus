from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox

from src.core.config import Config
from src.ui.pages.settings import SettingsPage
from tests.core.test_deploy_concurrency import hardware as hardware


@pytest.mark.parametrize(
    ("kind", "input_name", "button_name"),
    [
        ("import", "_parallel_spin", "_parallel_recommend_btn"),
        ("export", "_export_parallel_spin", "_export_parallel_recommend_btn"),
    ],
)
def test_worker_controls_keep_auto_mode_when_opened_and_recommended(
    qtbot,
    temp_data_dir,
    hardware,
    kind,
    input_name,
    button_name,
):
    hardware(8, 16)
    config = Config()
    config.set("mods.delete_method", "recycle_bin")
    before = config._config_path.read_bytes()
    page = SettingsPage()
    qtbot.addWidget(page)

    control = getattr(page, input_name, None)
    assert control is not None, f"{kind} worker count must be configurable in settings"
    assert control.value() == 4
    assert getattr(page, button_name).isChecked()
    assert config._config_path.read_bytes() == before

    with qtbot.waitSignal(page.settings_changed):
        getattr(page, button_name).click()
    config.reload()
    assert config.get(f"{kind}.parallel_workers") == 0
    assert config.get("mods.delete_method") == "recycle_bin"


def test_export_worker_control_is_in_export_section_and_survives_restart(qtbot, temp_data_dir, hardware):
    hardware(8, 16)
    config = Config()
    config.set("import.parallel_workers", 7)
    page = SettingsPage()
    qtbot.addWidget(page)
    control = getattr(page, "_export_parallel_spin", None)
    assert control is not None, "Export settings need their own worker count input"

    page.resize(1120, 900)
    page.show()
    page._switch_settings_section(3)
    page._stack.currentWidget().select_section("export")
    assert control.isVisible()
    labels = [label.text() for label in page._stack.currentWidget().findChildren(QLabel)]
    assert "导出并发" in labels

    control.setText("6")
    with qtbot.waitSignal(page.settings_changed):
        control.editingFinished.emit()
    config.reload()
    assert config.get("export.parallel_workers") == 6
    assert config.get("import.parallel_workers") == 7
    assert not page._export_parallel_recommend_btn.isChecked()

    page.close()
    Config._instance = None
    reopened = SettingsPage()
    qtbot.addWidget(reopened)
    assert reopened._export_parallel_spin.value() == 6
    reopened._export_parallel_recommend_btn.click()
    assert reopened._export_parallel_spin.value() == 4
    reopened.close()

    hardware(4, 8)
    Config._instance = None
    automatic = SettingsPage()
    qtbot.addWidget(automatic)
    assert automatic._export_parallel_spin.value() == 2
    assert automatic._export_parallel_recommend_btn.isChecked()
    assert Config().get("export.parallel_workers") == 0
    assert Config().get("import.parallel_workers") == 7


@pytest.mark.parametrize(
    ("kind", "input_name", "button_name"),
    [
        ("import", "_parallel_spin", "_parallel_recommend_btn"),
        ("export", "_export_parallel_spin", "_export_parallel_recommend_btn"),
    ],
)
def test_retyping_recommendation_is_manual_but_unedited_focus_keeps_auto(
    qtbot,
    temp_data_dir,
    hardware,
    kind,
    input_name,
    button_name,
):
    hardware(8, 16)
    page = SettingsPage()
    qtbot.addWidget(page)
    page._switch_settings_section(3)
    page._stack.currentWidget().select_section(kind)
    page.show()
    control = getattr(page, input_name)
    button = getattr(page, button_name)
    control.setFocus()
    qtbot.keyClick(control, Qt.Key.Key_Tab)
    assert Config().get(f"{kind}.parallel_workers") == 0
    assert button.isChecked()

    control.setFocus()
    control.selectAll()
    qtbot.keyClicks(control, "4")
    qtbot.keyClick(control, Qt.Key.Key_Return)
    assert Config().get(f"{kind}.parallel_workers") == 4
    assert not button.isChecked()


def test_reset_restores_automatic_policies_before_publishing_changes(qtbot, temp_data_dir, hardware, monkeypatch):
    hardware(4, 8)
    config = Config()
    for key in (
        "import.parallel_workers",
        "export.parallel_workers",
        "mods.deploy_concurrency",
        "gamebanana.update_check.concurrency",
    ):
        config.set(key, 7)
    config.set("images.memory_cache_mode", "manual")
    config.set("images.memory_cache_mb", 1024)
    config.set("general.theme", "light")
    config.set(
        "gamebanana.proxy_config",
        {
            "active": True,
            "enabled": True,
            "mode": "manual",
            "type": "socks5",
            "host": "custom.proxy",
            "port": "8888",
        },
    )
    page = SettingsPage()
    qtbot.addWidget(page)
    published = []
    page.settings_changed.connect(
        lambda: published.append(
            (
                page._parallel_spin.value(),
                getattr(page, "_export_parallel_spin", page._parallel_spin).value(),
                config.get("gamebanana.proxy_config.host"),
            )
        )
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    page._reset_settings()
    config.reload()
    for key in (
        "import.parallel_workers",
        "export.parallel_workers",
        "mods.deploy_concurrency",
        "gamebanana.update_check.concurrency",
    ):
        assert config.get(key) == 0
    assert page._parallel_spin.value() == page._export_parallel_spin.value() == 2
    assert page._parallel_recommend_btn.isChecked()
    assert page._export_parallel_recommend_btn.isChecked()
    assert config.get("images.memory_cache_mode") == "auto"
    assert page._image_memory_settings.auto_button.isChecked()
    assert config.get("gamebanana.proxy_config") == {
        "active": False,
        "enabled": False,
        "mode": "system",
        "type": "http",
        "host": "127.0.0.1",
        "port": "7890",
    }
    assert published and all(snapshot == (2, 2, "127.0.0.1") for snapshot in published)
