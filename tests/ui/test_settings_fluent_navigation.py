import pytest
from PySide6.QtCore import Qt

from src.core.config import Config
from src.ui.pages.settings import SettingsPage


@pytest.fixture
def page(qtbot, temp_data_dir, monkeypatch):
    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    widget = SettingsPage()
    qtbot.addWidget(widget)
    widget.resize(1250, 850)
    widget.show()
    qtbot.waitUntil(widget.isVisible)
    return widget


def test_section_navigation_keeps_controls_and_remembers_location(page, qtbot):
    before = Config()._config_path.read_bytes()
    general = page._stack.widget(0)
    assert callable(getattr(general, "select_section", None)), "Settings need a page-local directory"
    general.select_section("cache")
    assert page._image_memory_settings.isVisible()
    assert page._img_cache_cap.isVisible()
    assert not page._delete_method_combo.isVisible()
    page._switch_settings_section(1)
    page._switch_settings_section(0)
    assert general.current_section() == "cache"
    assert page._image_memory_settings.isVisible()
    assert Config()._config_path.read_bytes() == before


@pytest.mark.parametrize("width", [1200, 900, 700])
def test_first_category_switch_presents_final_responsive_layout(qtbot, qapp, temp_data_dir, monkeypatch, width):
    from src.ui import anim, theme
    from src.ui.widgets.settings_components import SettingsRow

    monkeypatch.setattr(anim, "_enabled", True)
    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()
    theme.set_theme("light")
    theme.set_background_active(True)
    qapp.setStyleSheet(theme.render_qss("light") + theme.background_overlay_qss("light"))
    page = SettingsPage()
    qtbot.addWidget(page)
    try:
        page.resize(width, 800)
        page.show()
        qtbot.wait(60)
        page._switch_settings_section(1)
        target = page._stack.currentWidget()
        rows = [row for row in target.findChildren(SettingsRow) if row.isVisible()]

        def positions():
            return (
                target._topics.geometry().getRect(),
                [(row.geometry().getRect(), row._controls.geometry().getRect()) for row in rows],
            )

        # 合成器可能在排队布局请求执行前立即截图。
        page._stack.grab()
        first_frame = positions()
        qtbot.wait(60)
        assert rows
        assert positions() == first_frame, "The first frame must not expose provisional stacked controls"
    finally:
        overlay = getattr(page._stack, "_cross_fade_overlay", None)
        if overlay is not None:
            overlay.finish()
        page.close()
        mode, background, sheet = previous
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(sheet)


def test_search_opens_real_setting_in_another_category_without_saving(page, qtbot):
    before = Config()._config_path.read_bytes()
    search = getattr(page, "_settings_search", None)
    assert search is not None, "The approved layout includes settings search"
    qtbot.keyClicks(search, "font")
    from PySide6.QtWidgets import QPushButton

    qtbot.waitUntil(
        lambda: any(
            button.isVisible() and "字体" in button.accessibleName()
            for button in page._search_results.findChildren(QPushButton)
        )
    )
    results = [
        button
        for button in page._search_results.findChildren(QPushButton)
        if button.isVisible() and "字体" in button.accessibleName()
    ]
    assert results, "Search must index the real font setting"
    qtbot.mouseClick(results[0], Qt.MouseButton.LeftButton)
    assert page._stack.currentIndex() == 1
    assert page._font_combo.isVisible()
    assert page._settings_search.text() == ""
    assert Config()._config_path.read_bytes() == before


def test_theme_search_reveals_the_actual_theme_choices(page, qtbot):
    from PySide6.QtWidgets import QPushButton

    before = Config()._config_path.read_bytes()
    page._settings_search.setText("主题")
    qtbot.waitUntil(
        lambda: any("界面主题" in button.accessibleName() for button in page._search_results.findChildren(QPushButton))
    )
    result = next(
        button for button in page._search_results.findChildren(QPushButton) if "界面主题" in button.accessibleName()
    )
    qtbot.mouseClick(result, Qt.MouseButton.LeftButton)
    assert page._stack.currentIndex() == 1
    assert all(button.isVisible() for button in page._theme_choices.buttons.values())
    assert Config()._config_path.read_bytes() == before


def test_reload_refreshes_visible_mode_and_toggle_labels_without_saving(page, qtbot):
    from src.ui.widgets.settings_components import SettingsRow

    page._deploy_concurrency_input.setText("12")
    page._deploy_concurrency_input.editingFinished.emit()
    assert not page._deploy_concurrency_recommend_btn.isChecked()
    page._import_strict_format_check.setChecked(True)
    config = Config()
    config.set("mods.deploy_concurrency", 0)
    config.set("import.strict_format", False)
    page._switch_settings_section(3)
    before = config._config_path.read_bytes()
    page._load_settings()
    assert page._deploy_concurrency_recommend_btn.isChecked()
    row = next(row for row in page.findChildren(SettingsRow) if row.isAncestorOf(page._import_strict_format_check))
    assert row._state_label.text() == "关"
    assert config._config_path.read_bytes() == before


def test_rejected_proxy_toggle_keeps_the_visible_state_in_sync(page, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from src.ui.widgets.settings_components import SettingsRow

    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    page._switch_settings_section(4)
    row = next(row for row in page.findChildren(SettingsRow) if row.isAncestorOf(page._proxy_enable_check))
    qtbot.mouseClick(page._proxy_enable_check, Qt.MouseButton.LeftButton)
    assert not page._proxy_enable_check.isChecked()
    assert row._state_label.text() == "关"


def test_update_recommendation_reflects_automatic_and_manual_preferences(page, qtbot):
    config = Config()
    button = page._upd_concurrency_recommend_btn
    assert button.isChecked()
    page._upd_concurrency_input.setText("5")
    page._upd_concurrency_input.editingFinished.emit()
    assert config.get("gamebanana.update_check.concurrency") == 5
    assert not button.isChecked()
    button.click()
    assert config.get("gamebanana.update_check.concurrency") == 0
    assert button.isChecked()


def test_proxy_link_opens_network_topic_without_writing_config(page, qtbot):
    before = Config()._config_path.read_bytes()
    page._switch_settings_section(4)
    qtbot.mouseClick(page._proxy_config_button, Qt.MouseButton.LeftButton)
    assert page._stack.currentIndex() == 0
    assert page._stack.currentWidget().current_section() == "network"
    assert page._proxy_active_check.isVisible()
    assert Config()._config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("query", "title", "category", "section", "control"),
    [
        ("滑块", "磁盘", 0, "cache", "_img_cache_cap"),
        ("在线图片缓存上限", "磁盘", 0, "cache", "_img_cache_cap"),
        ("清理磁盘缓存", "磁盘", 0, "cache", "_cache_clean_btn"),
        ("浅色", "界面主题", 1, "look", "_theme_choices"),
        ("WE 场景", "背景素材", 1, "background", "_bg_source_segments"),
        ("手动额度", "额度", 0, "cache", "_image_memory_settings"),
    ],
)
def test_search_finds_control_labels_options_and_actions_without_saving(
    page,
    qtbot,
    query,
    title,
    category,
    section,
    control,
):
    from PySide6.QtWidgets import QPushButton

    before = Config()._config_path.read_bytes()
    page._settings_search.setText(query)
    results = [
        button
        for button in page._search_results.findChildren(QPushButton)
        if button.isVisible() and title in button.accessibleName()
    ]
    assert results, f"The real setting for {query!r} must be searchable"
    qtbot.mouseClick(results[0], Qt.MouseButton.LeftButton)
    assert page._stack.currentIndex() == category
    assert page._stack.currentWidget().current_section() == section
    assert getattr(page, control).isVisible()
    assert Config()._config_path.read_bytes() == before


def test_search_finds_merged_memory_quota_after_manual_edit(page, qtbot):
    from PySide6.QtWidgets import QPushButton

    page._stack.widget(0).select_section("cache")
    memory = page._image_memory_settings
    memory.memory_input.setText("1024")
    memory.memory_input.editingFinished.emit()
    before = Config()._config_path.read_bytes()
    page._settings_search.setText("手动额度")
    result = next(
        (
            button
            for button in page._search_results.findChildren(QPushButton)
            if button.isVisible() and "缓存额度" in button.accessibleName()
        ),
        None,
    )
    assert result is not None, "Search must reveal the merged quota control through its manual quota keyword"
    qtbot.mouseClick(result, Qt.MouseButton.LeftButton)
    assert memory.memory_input.isVisible() and memory.memory_input.isEnabled()
    assert Config()._config_path.read_bytes() == before


@pytest.mark.parametrize("retype", [False, True])
def test_update_concurrency_commits_same_value_only_after_user_edit(page, qtbot, retype):
    config = Config()
    page._switch_settings_section(4)
    page._stack.currentWidget().select_section("mod-updates")
    count = page._upd_concurrency_input
    value = count.value()
    before = config._config_path.read_bytes()
    changes = []
    page.settings_changed.connect(lambda: changes.append(True))
    count.setFocus()
    if retype:
        count.selectAll()
        qtbot.keyClicks(count, str(value))
    qtbot.keyClick(count, Qt.Key.Key_Tab)
    config.reload()
    assert config.get("gamebanana.update_check.concurrency") == (value if retype else 0)
    assert page._upd_concurrency_recommend_btn.isChecked() is not retype
    assert page._upd_concurrency_auto is not retype
    assert changes == ([True] if retype else [])
    if not retype:
        assert config._config_path.read_bytes() == before
