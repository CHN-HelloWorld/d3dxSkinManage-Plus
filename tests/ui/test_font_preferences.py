from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QVBoxLayout

from src.app import App
from src.core.config import Config
from src.ui import theme
from src.ui.dialogs import mod_updates_style
from src.ui.pages.settings import SettingsPage


@pytest.fixture
def font_app(qapp, temp_data_dir, monkeypatch):
    from src.ui.background_style import install_background_style
    from src.ui.fonts import apply_font

    previous_controller = getattr(qapp, "_background_style", None)
    surfaces = install_background_style(qapp)
    previous_surfaces = surfaces.enabled
    previous_font = QFont(qapp.font())
    previous_sheet = qapp.styleSheet()
    previous_theme = theme.current_theme()
    previous_background = theme.is_background_active()
    monkeypatch.setattr(App, "_instance", None)
    app = App()
    app._qt_app = qapp
    yield app
    qapp.setStyleSheet("")
    apply_font(qapp, previous_font)
    theme.set_theme(previous_theme)
    theme.set_background_active(previous_background)
    surfaces.apply(previous_surfaces)
    qapp.setStyleSheet(previous_sheet)
    if previous_controller is None:
        # 清理外观控制器，防止后续绘制测试的 Polish 事件继续修改材质。
        surfaces.dispose()


@pytest.fixture
def installed_family(qapp):
    system_family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    families = QFontDatabase.families()
    for family in ("Times New Roman", "Arial", "DejaVu Serif", *families):
        if family in families and family != system_family and not family.startswith("@"):
            return family
    pytest.skip("This Qt platform backend does not expose installed fonts")


@pytest.mark.parametrize("appearance", [{}, {"font_family": None}, {"font_family": 42}])
def test_missing_or_invalid_preference_defaults_without_rewriting_other_settings(temp_data_dir, appearance):
    path = temp_data_dir / "config" / "settings.json"
    path.write_text(json.dumps({"appearance": appearance, "mods": {"delete_method": "recycle_bin"}}), encoding="utf-8")
    before = path.read_bytes()

    config = Config()

    assert config.get("appearance.font_family") == ""
    assert config.get("mods.delete_method") == "recycle_bin"
    assert path.read_bytes() == before


def test_opening_settings_loads_installed_font_without_saving(qtbot, font_app, installed_family):
    config = Config()
    config.set("appearance.font_family", installed_family)
    before = config._config_path.read_bytes()

    page = SettingsPage()
    qtbot.addWidget(page)

    assert page._font_combo.itemData(0) == ""
    assert page._font_combo.currentData() == installed_family
    offered = {page._font_combo.itemData(i) for i in range(1, page._font_combo.count())}
    assert installed_family in offered
    assert offered <= set(QFontDatabase.families())
    assert not any(family.startswith("@") for family in offered)
    assert config._config_path.read_bytes() == before


@pytest.mark.parametrize("background", [False, True])
def test_selection_updates_existing_styled_widgets_and_survives_theme_changes(
    qtbot,
    qapp,
    font_app,
    installed_family,
    background,
    tmp_path,
):
    config = Config()
    config.set("appearance.background.enabled", background)
    if background:
        from PySide6.QtGui import QColor, QImage

        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("#335577"))
        image_path = tmp_path / "background.png"
        assert image.save(str(image_path))
        config.set("appearance.background.path", str(image_path))
    font_app.apply_theme("dark")
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(mod_updates_style.stylesheet())
    label = QLabel("Example 示例 123", dialog)
    label.setStyleSheet("font-size: 18px; font-weight: bold;")
    QVBoxLayout(dialog).addWidget(label)
    dialog.ensurePolished()

    page._font_combo.setCurrentIndex(page._font_combo.findData(installed_family))
    qapp.processEvents()

    config.reload()
    assert config.get("appearance.font_family") == installed_family
    assert qapp.font().family() == installed_family
    assert label.font().family() == installed_family
    assert label.font().pixelSize() == 18
    assert label.font().bold()
    assert theme.is_background_active() is background
    for mode in ("light", "dark"):
        font_app.apply_theme(mode)
        assert label.font().family() == installed_family
        assert label.font().pixelSize() == 18
    later = QLabel("Created after switching")
    qtbot.addWidget(later)
    later.ensurePolished()
    assert later.font().family() == installed_family

    page._font_combo.setCurrentIndex(0)
    qapp.processEvents()
    config.reload()
    expected = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    assert config.get("appearance.font_family") == ""
    assert qapp.font().family() == expected
    assert label.font().family() == expected
    assert label.font().pixelSize() == 18


def test_saved_font_is_reapplied_and_unavailable_font_falls_back(qtbot, qapp, font_app, installed_family):
    config = Config()
    config.set("appearance.font_family", installed_family)
    config.reload()
    font_app.apply_font(config.get("appearance.font_family"))
    font_app.apply_theme("light")
    label = QLabel("Saved preference")
    qtbot.addWidget(label)
    label.ensurePolished()
    assert label.font().family() == installed_family

    config.set("appearance.font_family", "Missing font 93e7c153")
    before = config._config_path.read_bytes()
    font_app.apply_font(config.get("appearance.font_family"))
    page = SettingsPage()
    qtbot.addWidget(page)
    expected = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    assert qapp.font().family() == label.font().family() == expected
    assert page._font_combo.currentData() == ""
    assert config._config_path.read_bytes() == before


def test_search_commits_only_an_installed_font(qtbot, font_app, installed_family):
    config = Config()
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    before = config._config_path.read_bytes()

    page._font_combo.setEditText("Missing font 93e7c153")
    page._font_combo.lineEdit().editingFinished.emit()

    qtbot.waitUntil(lambda: page._font_combo.currentText() == page._font_combo.itemText(0))
    assert config._config_path.read_bytes() == before
    page._font_combo.setEditText(installed_family.swapcase())
    page._font_combo.lineEdit().editingFinished.emit()
    qtbot.waitUntil(lambda: config.get("appearance.font_family") == installed_family)
    config.reload()
    assert config.get("appearance.font_family") == installed_family
    assert page._font_combo.currentText() == installed_family


def test_restore_defaults_applies_system_font_immediately(qtbot, qapp, font_app, installed_family, monkeypatch):
    config = Config()
    config.set("appearance.font_family", installed_family)
    font_app.apply_font(installed_family)
    font_app.apply_theme("dark")
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    page._reset_settings()

    config.reload()
    assert config.get("appearance.font_family") == ""
    assert page._font_combo.currentData() == ""
    assert qapp.font().family() == QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def test_navigation_indicator_tracks_new_font_metrics(qtbot, qapp, font_app, installed_family):
    font_app.apply_font("")
    font_app.apply_theme("dark")
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    page.resize(980, 640)
    page.show()
    page._switch_settings_section(1)
    qapp.processEvents()
    button = page._settings_nav_btns[1]
    indicator = page._settings_indicator
    left_inset = indicator.x() - button.x()
    right_inset = button.geometry().right() - indicator.geometry().right()
    family = "KaiTi" if "KaiTi" in QFontDatabase.families() else installed_family

    page._font_combo.setCurrentIndex(page._font_combo.findData(family))
    qapp.processEvents()

    # 字体度量变化时保留原有内边距。
    qtbot.waitUntil(
        lambda: (
            indicator.x() == button.x() + left_inset and indicator.width() == button.width() - left_inset - right_inset
        )
    )
    search = page._settings_search
    margins = search.textMargins()
    assert (
        search.fontMetrics().horizontalAdvance(search.placeholderText())
        <= search.contentsRect().width() - margins.left() - margins.right() - 4
    )


def test_shortcut_button_reflows_after_font_change(qtbot, qapp, font_app, installed_family):
    from src.ui.widgets.shortcut_controls import BindingButton

    font_app.apply_font("")
    font_app.apply_theme("dark")
    button = BindingButton("Example", "Ctrl+Shift+PageDown")
    qtbot.addWidget(button)
    button.set_column_width(210)
    button.ensurePolished()

    font_app.apply_font(installed_family)
    qapp.processEvents()

    assert button.height() == button.heightForWidth(button.width())


def test_failed_search_save_restores_previous_font_once(qtbot, qapp, font_app, installed_family, monkeypatch):
    from src.core import config as config_module

    font_app.apply_font("")
    config = Config()
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    before = config._config_path.read_bytes()
    warnings = []

    def fail_write(*_args):
        raise OSError("read-only preferences")

    monkeypatch.setattr(config_module, "write_json_atomic", fail_write)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    page._font_combo.setEditText(installed_family)
    page._font_combo.lineEdit().editingFinished.emit()
    qapp.processEvents()

    assert len(warnings) == 1
    assert config._config_path.read_bytes() == before
    assert config.get("appearance.font_family") == ""
    assert page._font_combo.currentData() == ""
    assert page._font_combo.currentText() == page._font_combo.itemText(0)
    assert qapp.font().family() == QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


@pytest.mark.parametrize("activation", ["keyboard", "mouse"])
def test_search_popup_selection_commits_the_highlighted_font(qtbot, qapp, font_app, activation):
    if not {"Arial", "Courier New"} <= set(QFontDatabase.families()):
        pytest.skip("This interaction regression uses the Windows system font names")
    config = Config()
    config.set("appearance.font_family", "Arial")
    font_app.apply_font("Arial")
    font_app.apply_theme("dark")
    page = SettingsPage()
    qtbot.addWidget(page)
    page.font_changed.connect(font_app.apply_font)
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    page.resize(980, 640)
    page.show()
    page._switch_settings_section(1)
    combo = page._font_combo
    popup = combo.completer().popup()
    popup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    combo.view().window().setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    combo.setFocus()
    combo.lineEdit().selectAll()

    qtbot.keyClicks(combo.lineEdit(), "New")
    qtbot.waitUntil(popup.isVisible)
    qtbot.keyClick(popup, Qt.Key.Key_Down)
    assert popup.currentIndex().data() == "Courier New"
    if activation == "keyboard":
        qtbot.keyClick(popup, Qt.Key.Key_Return)
    else:
        qtbot.mouseClick(
            popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(popup.currentIndex()).center()
        )

    qtbot.waitUntil(lambda: config.get("appearance.font_family") == "Courier New")
    config.reload()
    assert config.get("appearance.font_family") == combo.currentData() == combo.currentText() == "Courier New"
    assert qapp.font().family() == "Courier New"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_font_popup_surfaces_do_not_inherit_transparent_settings_background(
    qtbot,
    qapp,
    font_app,
    installed_family,
    mode,
):
    Config().set("general.theme", mode)
    font_app.apply_font("")
    font_app.apply_theme(mode)
    page = SettingsPage()
    qtbot.addWidget(page)
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    page.resize(980, 640)
    page.show()
    page._switch_settings_section(1)
    qapp.processEvents()
    combo = page._font_combo
    combo.view().window().setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    combo.showPopup()
    qapp.processEvents()

    surface = combo.view().window().grab().toImage()
    assert surface.pixelColor(surface.width() - 30, surface.height() - 14).alpha() == 255
    combo.hidePopup()

    popup = combo.completer().popup()
    popup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    combo.setFocus()
    combo.lineEdit().selectAll()
    qtbot.keyClicks(combo.lineEdit(), installed_family[:1])
    qtbot.waitUntil(popup.isVisible)
    surface = popup.grab().toImage()
    assert surface.pixelColor(surface.width() - 30, surface.height() - 14).alpha() == 255


def test_font_popup_preserves_acrylic_and_selected_font_after_retheme(qtbot, qapp, font_app, installed_family):
    from src.ui import acrylic

    font_app.apply_font(installed_family)
    font_app.apply_theme("light")
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    try:
        page = SettingsPage()
        qtbot.addWidget(page)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        page.resize(980, 640)
        page.show()
        page._switch_settings_section(1)
        qapp.processEvents()
        combo = page._font_combo
        combo.view().window().setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        for mode in ("light", "dark"):
            font_app.apply_theme(mode)
            acrylic.reset_for_theme_change()
            page.retheme()
            combo.showPopup()
            qapp.processEvents()

            surface = combo.view().window().grab().toImage()
            alpha = surface.pixelColor(surface.width() - 30, surface.height() - 14).alpha()
            assert 0 < alpha < 255, "The font selector must preserve the app's translucent popup surface"
            assert combo.view().font().family() == installed_family
            combo.hidePopup()
    finally:
        qapp.removeEventFilter(popup_filter)
        popup_filter.deleteLater()


def test_font_change_preserves_stylesheet_and_explicit_font_attributes(
    qtbot,
    qapp,
    font_app,
    installed_family,
    monkeypatch,
):
    font_app.apply_theme("dark")
    label = QLabel("Styled label")
    label.setStyleSheet("font-size: 18px; font-weight: bold;")
    explicit = QLabel("Custom painted typography")
    custom = QFont(qapp.font())
    custom.setPixelSize(21)
    custom.setItalic(True)
    explicit.setFont(custom)
    for widget in (label, explicit):
        qtbot.addWidget(widget)
        widget.ensurePolished()
    before = [(w.font().pixelSize(), w.font().bold(), w.font().italic()) for w in (label, explicit)]
    previous_sheet = qapp.styleSheet()
    theme_calls = []
    monkeypatch.setattr(font_app, "apply_theme", lambda *args: theme_calls.append(args))

    font_app.apply_font(installed_family)

    assert theme_calls == [], "Font selection must not repolish the entire application"
    assert qapp.styleSheet() == previous_sheet
    for widget, attributes in zip((label, explicit), before):
        assert widget.font().family() == installed_family
        assert (widget.font().pixelSize(), widget.font().bold(), widget.font().italic()) == attributes


def test_live_font_requests_yield_and_apply_the_last_family(qtbot, qapp, font_app, installed_family):
    font_app.apply_theme("dark")
    labels = [QLabel("Live font 示例") for _ in range(40)]
    for label in labels:
        qtbot.addWidget(label)
        label.ensurePolished()
    callbacks = []

    font_app.apply_font(installed_family, on_finished=lambda: callbacks.append("superseded"))
    font_app.apply_font("", on_finished=lambda: callbacks.append("latest"))

    assert callbacks == [], "Live application must yield to paint the busy notification"
    qtbot.waitUntil(lambda: callbacks == ["latest"], timeout=8000)
    expected = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    assert all(label.font().family() == expected for label in labels)
    assert qapp.font().family() == expected


def test_font_preview_uses_each_family_without_resolving_fonts_for_sizing(
    qtbot,
    font_app,
    installed_family,
    monkeypatch,
):
    from PySide6.QtWidgets import QStyleOptionViewItem

    from src.ui.fonts import font_catalog

    page = SettingsPage()
    qtbot.addWidget(page)
    catalog = font_catalog()
    qtbot.waitUntil(lambda: catalog.is_ready, timeout=8000)
    combo = page._font_combo
    for view in (combo.view(), combo.completer().popup()):
        delegate = view.itemDelegate()
        index = combo.model().index(combo.findData(installed_family), 0)
        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, index)
        assert option.font.family() == installed_family, (type(delegate).__name__, index.data(Qt.ItemDataRole.UserRole))
        delegate.initStyleOption(option, combo.model().index(0, 0))
        assert option.font.family() == QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
        font_resolutions = []
        monkeypatch.setattr(delegate, "initStyleOption", lambda *args: font_resolutions.append(args))
        for row in range(combo.count()):
            size = delegate.sizeHint(QStyleOptionViewItem(), combo.model().index(row, 0))
            assert size.height() >= 28
        assert font_resolutions == [], "Popup layout must not instantiate every preview font"
