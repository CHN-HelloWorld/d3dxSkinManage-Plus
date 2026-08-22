import pytest
from PySide6.QtTest import QSignalSpy

from src.ui.pages.settings import SettingsPage


@pytest.fixture
def page(qtbot, temp_data_dir):
    result = SettingsPage()
    qtbot.addWidget(result)
    return result


def select(page, source):
    page._bg_source_combo.setCurrentIndex(page._bg_source_combo.findData(source))


def test_source_buttons_show_the_selected_source_without_nested_engine_surface(page):
    assert page._bg_source_combo.isHidden()
    page._bg_source_buttons["scene"].click()
    assert page._config.get("appearance.background.source") == "scene"
    assert page._bg_source_buttons["scene"].isChecked()
    assert page._bg_engine_panel.parentWidget() is page._bg_we_panel
    assert page._bg_engine_panel.objectName() != "settingsGroup"
    assert page._bg_we_panel.objectName() != "settingsGroup"
    page._bg_source_buttons["image"].click()
    assert page._bg_source_buttons["image"].isChecked()
    assert not page._bg_source_buttons["scene"].isChecked()
    assert not page._bg_image_row.isHidden()


def test_scene_entry_remains_available_without_engine_and_other_sources_work(page):
    select(page, "scene")
    assert page._config.get("appearance.background.source") == "scene"
    assert not page._bg_engine_panel.isHidden()
    assert page._bg_scene_panel.isHidden()
    assert page._bg_sub.isHidden()
    select(page, "video")
    assert not page._bg_video_panel.isHidden()
    assert not page._bg_sub.isHidden()
    select(page, "off")
    assert page._config.get("appearance.background.enabled") is False
    assert page._bg_sub.isHidden()


def test_loading_controls_does_not_write_saved_configuration(page, monkeypatch):
    writes = []
    monkeypatch.setattr(page._config, "set", lambda *args: writes.append(args))
    page._load_background_settings()
    assert writes == []


def test_video_parameters_save_without_restarting_source(page):
    select(page, "video")
    changed = QSignalSpy(page.background_changed)
    refresh = QSignalSpy(page.bg_overlay_refresh)
    page._bg_fps_combo.setCurrentIndex(page._bg_fps_combo.findData(60))
    page._bg_volume_slider.setValue(25)
    assert page._config.get("appearance.background.fps") == 60
    assert page._config.get("appearance.background.volume") == 25
    assert changed.count() == 0
    assert refresh.count() == 2


def test_scene_volume_is_visible_and_saved_without_source_restart(page):
    select(page, "scene")
    page._bg_engine_validator.valid = True
    page._update_background_panels()
    assert not page._bg_volume_row.isHidden()
    changed = QSignalSpy(page.background_changed)
    refresh = QSignalSpy(page.bg_overlay_refresh)
    page._bg_volume_slider.setValue(37)
    assert page._config.get("appearance.background.volume") == 37
    assert page._bg_volume_label.text() == "37%"
    assert (changed.count() == 0) and refresh.count() == 1


def test_playback_controls_reflect_status_and_emit_intent(page):
    select(page, "video")
    requests = QSignalSpy(page.bg_playback_requested)
    page.set_background_status("paused", "已暂停")
    page._bg_pause_button.click()
    assert [requests.at(index) for index in range(requests.count())] == [["resume"]]
    page.set_background_status("error", "素材缺失")
    assert not page._bg_retry_button.isHidden()
    page._bg_retry_button.click()
    assert requests.at(requests.count() - 1) == ["retry"]


def test_engine_success_expands_controls_and_clear_keeps_repair_entry(page, qtbot, monkeypatch):
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (True, "可用"))
    select(page, "scene")
    page._bg_scene_path_edit.setText("saved/project.json")
    saved_scene = page._config.get("appearance.background.scene_path")
    page._bg_engine_path_edit.setText("wallpaper64.exe")
    assert page._bg_scene_panel.isHidden()
    qtbot.waitUntil(lambda: page._bg_engine_validator.valid, timeout=3000)
    assert not page._bg_scene_panel.isHidden()
    assert not page._bg_sub.isHidden()
    page._bg_engine_path_edit.clear()
    assert page._bg_scene_panel.isHidden()
    assert page._bg_sub.isHidden()
    assert not page._bg_engine_panel.isHidden()
    assert page._config.get("appearance.background.source") == "scene"
    assert page._config.get("appearance.background.scene_path") == saved_scene


def test_theme_reload_does_not_invalidate_validated_engine(page, qtbot, monkeypatch):
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (True, "可用"))
    select(page, "scene")
    page._bg_engine_path_edit.setText("wallpaper64.exe")
    qtbot.waitUntil(lambda: page._bg_engine_validator.valid, timeout=3000)
    status_changes = QSignalSpy(page._bg_engine_validator.status_changed)
    page._load_background_settings()
    assert page._bg_engine_validator.valid
    assert status_changes.count() == 0


def test_dynamic_fit_excludes_tile_and_preserves_image_preference(page):
    select(page, "image")
    page._bg_fit_combo.setCurrentIndex(page._bg_fit_combo.findData("tile"))
    select(page, "video")
    assert page._bg_fit_combo.findData("tile") == -1
    select(page, "image")
    assert page._bg_fit_combo.currentData() == "tile"


def test_auto_discovery_persists_path_and_opens_scene_controls(page, qtbot, monkeypatch, tmp_path):
    engine_path = str(tmp_path / "wallpaper64.exe")
    monkeypatch.setattr("src.ui.backgrounds.engine.discover_engine_path", lambda: engine_path)
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (True, "可用"))
    select(page, "scene")
    page._bg_engine_validator.discover()
    qtbot.waitUntil(lambda: page._bg_engine_validator.valid, timeout=3000)
    assert page._bg_engine_path_edit.text() == engine_path
    assert page._config.get("appearance.background.engine_path") == engine_path
    assert not page._bg_scene_panel.isHidden()


def test_failed_engine_validation_does_not_hide_its_repair_panel(page, qtbot, monkeypatch):
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (False, "安装资源缺失"))
    select(page, "scene")
    page._bg_engine_path_edit.setText("broken/wallpaper64.exe")
    qtbot.waitUntil(lambda: page._bg_engine_validator.status == "invalid", timeout=3000)
    assert not page._bg_engine_panel.isHidden()
    assert page._bg_scene_panel.isHidden()
    assert page._bg_playback_panel.isHidden()
    assert page._bg_engine_status.text() == "安装资源缺失"


def test_restore_saved_scene_checks_engine_without_writing_configuration(page, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (True, "可用"))
    page._config.set("appearance.background.source", "scene")
    page._config.set("appearance.background.enabled", True)
    page._config.set("appearance.background.engine_path", str(tmp_path / "wallpaper64.exe"))
    before = page._config._config_path.read_bytes()
    page._load_background_settings()
    assert page._bg_scene_panel.isHidden()
    qtbot.waitUntil(lambda: page._bg_engine_validator.valid, timeout=3000)
    assert not page._bg_scene_panel.isHidden()
    assert page._config._config_path.read_bytes() == before


def test_image_error_is_visible_and_retry_does_not_expose_video_controls(page):
    select(page, "image")
    page.set_background_status("error", "图片无法加载，请重新选择或重试。")
    assert not page._bg_image_error_row.isHidden()
    assert page._bg_image_status.text() == "图片无法加载，请重新选择或重试。"
    assert page._bg_playback_panel.isHidden()
    requests = QSignalSpy(page.bg_playback_requested)
    page._bg_image_retry_button.click()
    assert [requests.at(index) for index in range(requests.count())] == [["retry"]]
    page.set_background_status("playing", "图片已加载")
    assert page._bg_image_error_row.isHidden()


def test_scene_configuration_stays_in_one_container_and_shared_controls_return(page, qtbot, monkeypatch):
    monkeypatch.setattr("src.ui.backgrounds.engine.validate_engine_path", lambda path: (True, "可用"))
    select(page, "scene")
    page._bg_engine_path_edit.setText("wallpaper64.exe")
    qtbot.waitUntil(lambda: page._bg_engine_validator.valid, timeout=3000)
    for widget in (page._bg_engine_panel, page._bg_scene_panel, page._bg_playback_panel, page._bg_sub):
        assert page._bg_we_panel.isAncestorOf(widget)
    select(page, "video")
    assert page._bg_we_panel.isHidden()
    for widget in (page._bg_playback_panel, page._bg_sub):
        assert not page._bg_we_panel.isAncestorOf(widget)
        assert not widget.isHidden()
    select(page, "image")
    assert not page._bg_sub.isHidden()
    assert page._bg_playback_panel.isHidden()
