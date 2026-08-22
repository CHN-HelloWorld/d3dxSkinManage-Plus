import json

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider

from src.core.config import Config
from src.ui.pages.settings import SettingsPage


def _background_on_disk(config):
    return json.loads(config._config_path.read_text(encoding="utf-8"))["appearance"]["background"]


@pytest.fixture
def background_page(qtbot, temp_data_dir):
    config = Config()
    config.set("general.theme", "dark")
    config.set("appearance.background.enabled", True)
    config.set("appearance.background.scrim_dark", 67)
    config.set("appearance.background.scrim_light", 22)
    page = SettingsPage()
    qtbot.addWidget(page)
    page._switch_settings_section(1)
    page.resize(1180, 1100)
    page.show()
    QApplication.processEvents()
    return page


@pytest.mark.parametrize("theme_name", ["dark", "light"])
@pytest.mark.parametrize("interaction", ["keyboard", "wheel", "groove"])
def test_scrim_non_drag_edits_autosave_only_current_theme(background_page, qtbot, theme_name, interaction):
    page = background_page
    config = page._config
    page._theme_combo.setCurrentIndex(page._theme_combo.findData(theme_name))
    before = _background_on_disk(config)
    slider = page._bg_scrim_slider
    edits = QSignalSpy(page.bg_scrim_live)
    # 遮罩修改不得提交其他控件仍待处理的变更。
    page._bg_blur_slider.setValue(73)

    if interaction == "keyboard":
        qtbot.keyClick(slider, Qt.Key.Key_Right)
    elif interaction == "wheel":
        local = slider.rect().center()
        event = QWheelEvent(
            QPointF(local),
            QPointF(slider.mapToGlobal(local)),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(slider, event)
    else:
        qtbot.mouseClick(
            slider,
            Qt.MouseButton.LeftButton,
            pos=QPoint(slider.width() - 4, slider.height() // 2),
        )

    value = slider.value()
    assert value != before[f"scrim_{theme_name}"]
    assert [edits.at(index) for index in range(edits.count())] == [[value]]
    expected = {**before, f"scrim_{theme_name}": value}
    assert _background_on_disk(config) == expected
    config.reload()
    assert config.get("appearance.background") == expected


@pytest.mark.parametrize("theme_name", ["dark", "light"])
def test_scrim_drag_previews_without_saving_until_release(background_page, qtbot, theme_name):
    page = background_page
    config = page._config
    page._theme_combo.setCurrentIndex(page._theme_combo.findData(theme_name))
    before = _background_on_disk(config)
    slider = page._bg_scrim_slider
    edits = QSignalSpy(page.bg_scrim_live)
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider,
        option,
        QStyle.SubControl.SC_SliderHandle,
        slider,
    )
    start = handle.center()

    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
    assert slider.isSliderDown()
    for offset in (10, 25):
        qtbot.mouseMove(slider, pos=start - QPoint(offset, 0))
        assert slider.value() < before[f"scrim_{theme_name}"]
        assert edits.at(edits.count() - 1)[0] == slider.value()
        assert _background_on_disk(config) == before

    value = slider.value()
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=start - QPoint(25, 0))
    expected = {**before, f"scrim_{theme_name}": value}
    assert _background_on_disk(config) == expected
    config.reload()
    assert config.get("appearance.background") == expected


def test_theme_switch_loads_its_own_scrim_without_emitting_live_edits(qtbot, temp_data_dir):
    config = Config()
    config.set("general.theme", "dark")
    config.set("appearance.background.scrim_dark", 67)
    config.set("appearance.background.scrim_light", 22)
    before = config._config_path.read_bytes()
    page = SettingsPage()
    qtbot.addWidget(page)
    assert config._config_path.read_bytes() == before
    edits = QSignalSpy(page.bg_scrim_live)

    page._theme_combo.setCurrentIndex(page._theme_combo.findData("light"))
    assert page._bg_scrim_slider.value() == 22
    assert page._bg_scrim_label.text() == "22%"
    assert edits.count() == 0
    assert _background_on_disk(config)["scrim_dark"] == 67
    assert _background_on_disk(config)["scrim_light"] == 22

    page._bg_scrim_slider.setValue(35)
    config.reload()
    assert config.get("appearance.background.scrim_light") == 35
    assert config.get("appearance.background.scrim_dark") == 67

    page._theme_combo.setCurrentIndex(page._theme_combo.findData("dark"))
    assert page._bg_scrim_slider.value() == 67
    assert page._bg_scrim_label.text() == "67%"
    assert edits.count() == 1
