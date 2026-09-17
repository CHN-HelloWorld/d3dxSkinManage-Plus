from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


@pytest.fixture
def custom_language(tmp_path, monkeypatch):
    from src.utils import i18n

    root = tmp_path / "config/languages"
    folder = root / "de-DE"
    folder.mkdir(parents=True)
    (folder / "strings.json").write_text(
        json.dumps(
            {
                "_meta": {"name": "Deutsch"},
                "messages": {
                    "语言与字体": "Sprache und Schrift",
                    "显示语言": "Anzeigesprache",
                    "快捷键": "Tastenkürzel",
                    "自定义": "Benutzerdefiniert",
                    "简洁": "Einfach",
                    "标准": "Standard",
                    "详细": "Ausführlich",
                    "打开语言包文件夹": "Open language packs folder",
                    "刷新列表": "Refresh list",
                    "此 Mod 没有可保存的持久化数值变量": "Keine speicherbaren Variablen",
                },
                "gamebanana": {"Skins": "Aussehen", "Characters": "Figuren"},
                "qt": {"Cancel": "Zurück"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    instance = i18n.I18n(directory=root)
    instance.set_language("de-DE")
    monkeypatch.setattr(i18n, "_i18n", instance)
    return instance


def test_settings_discovers_pack_and_translates_newer_sections(qtbot, temp_data_dir, monkeypatch, custom_language):
    from src.ui.pages.settings import SettingsPage

    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    page = SettingsPage()
    qtbot.addWidget(page)
    index = page._language_combo.findData("de-DE")
    assert index >= 0
    assert page._language_combo.itemText(index) == "Deutsch"
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "Sprache und Schrift" in labels
    assert "Anzeigesprache" in labels


def test_open_language_folder_uses_the_active_config_directory_without_copying_files(
    qtbot, temp_data_dir, monkeypatch, custom_language
):
    from PySide6.QtGui import QDesktopServices

    from src.ui.pages.settings import SettingsPage

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    before = {
        path.relative_to(custom_language.directory): path.read_bytes()
        for path in custom_language.directory.rglob("*")
        if path.is_file()
    }
    page = SettingsPage()
    qtbot.addWidget(page)
    page._open_language_folder()
    from pathlib import Path

    assert [Path(path) for path in opened] == [custom_language.directory]
    after = {
        path.relative_to(custom_language.directory): path.read_bytes()
        for path in custom_language.directory.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_gamebanana_breadcrumb_and_picker_translate_without_mutating_api_categories(qtbot, custom_language):
    from src.services.gamebanana.models import GbCategory
    from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar

    def category(name, cid):
        return GbCategory(id=cid, name=name, parent_id=None, item_count=0, icon_url=None)

    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    root, skins, chars = category("My Game", 0), category("Skins", 5000), category("Characters", 5100)
    bar.set_path([root, skins])
    assert bar.segment_texts() == ["My Game", "Aussehen"]
    bar._populate_menu(1)
    bar.set_children(5000, [chars])
    picker = bar._pickers[1]
    item = picker._list.item(0)
    assert "Figuren" in item.text()
    assert item.data(Qt.ItemDataRole.UserRole).name == "Characters"
    assert (root.name, skins.name, chars.name) == ("My Game", "Skins", "Characters")


def test_qt_standard_button_translations_can_be_overridden_by_the_json_pack(qapp, custom_language):
    from PySide6.QtWidgets import QDialogButtonBox

    from src.ui.i18n import LanguagePackTranslator

    translator = LanguagePackTranslator(custom_language, qapp)
    qapp.installTranslator(translator)
    try:
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        assert box.button(QDialogButtonBox.StandardButton.Cancel).text() == "Zurück"
    finally:
        qapp.removeTranslator(translator)


def test_qt_keeps_source_text_when_no_translator_has_an_entry(qapp, custom_language):
    from PySide6.QtCore import QCoreApplication

    from src.ui.i18n import LanguagePackTranslator

    translator = LanguagePackTranslator(custom_language, qapp)
    qapp.installTranslator(translator)
    try:
        assert QCoreApplication.translate("custom-plugin", "Unregistered UI sentence") == "Unregistered UI sentence"
    finally:
        qapp.removeTranslator(translator)


def test_json_overlay_falls_through_to_existing_qt_translations(qapp, custom_language):
    from PySide6.QtCore import QCoreApplication, QTranslator

    from src.ui.i18n import LanguagePackTranslator

    class BaseTranslator(QTranslator):
        def isEmpty(self):
            return False

        def translate(self, context, source, disambiguation=None, n=-1):
            return "Base translation" if source == "Provided by Qt" else None

    base = BaseTranslator(qapp)
    overlay = LanguagePackTranslator(custom_language, qapp)
    qapp.installTranslator(base)
    qapp.installTranslator(overlay)
    try:
        assert QCoreApplication.translate("QWidget", "Provided by Qt") == "Base translation"
    finally:
        qapp.removeTranslator(overlay)
        qapp.removeTranslator(base)


def test_shortcut_command_descriptions_are_translated_when_the_page_is_built(qtbot, temp_data_dir, custom_language):
    from src.ui.pages.shortcuts import ShortcutsPage
    from src.ui.shortcut_manager import ShortcutSettings

    page = ShortcutsPage(ShortcutSettings())
    qtbot.addWidget(page)
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "Tastenkürzel" in labels
    assert "Benutzerdefiniert" in labels


def test_unsupported_variant_state_keeps_its_behavior_after_translation(qtbot, custom_language):
    from src.ui.widgets.mod_variant_panel import ModVariantPanel

    panel = ModVariantPanel()
    qtbot.addWidget(panel)
    panel.set_state(
        {
            "mod_id": "test",
            "enabled": True,
            "variable_count": 0,
            "reason": custom_language.t("此 Mod 没有可保存的持久化数值变量"),
        },
        [],
    )
    assert panel.save_button.isHidden()


def test_replacing_a_visible_preset_page_publishes_its_controls_synchronously(qtbot):
    from types import SimpleNamespace

    from src.ui.dialogs.category_preset import CategoryPresetDialog

    manager = SimpleNamespace(_game_service=SimpleNamespace(get_game=lambda _id: None))
    dialog = CategoryPresetDialog(manager, "")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    dialog._new_page("Translated result", "error")
    assert dialog._page.isVisible()
    assert dialog._content.isVisible()


def test_english_preset_names_translate_only_the_builtin_default(qtbot, monkeypatch):
    from pathlib import Path

    from src.models.preset import Preset
    from src.ui.widgets.preset_selector import PresetSelector
    from src.utils import i18n

    catalog = i18n.I18n(directory=Path(__file__).resolve().parents[2] / "config/languages")
    assert catalog.set_language("en-US")
    monkeypatch.setattr(i18n, "_i18n", catalog)
    presets = [
        Preset(id="default", name="默认", is_default=True),
        Preset(id="custom", name="默认"),
        Preset(id="renamed", name="自定义名称", is_default=True),
    ]
    selector = PresetSelector()
    qtbot.addWidget(selector)
    selector.set_presets(presets, "default")

    assert "Default" in selector._btn.text()
    assert "默认" not in selector._btn.text()
    rows = [selector._popup._list.itemWidget(selector._popup._list.item(i)) for i in range(3)]
    assert [row._name_label.toolTip() for row in rows] == ["Default", "默认", "自定义名称"]
    assert [preset.name for preset in presets] == ["默认", "默认", "自定义名称"]


def test_language_pack_actions_fit_their_translated_labels_at_compact_width(
    qtbot, temp_data_dir, monkeypatch, custom_language
):
    from PySide6.QtWidgets import QPushButton

    from src.ui.pages.settings import SettingsPage

    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    page = SettingsPage()
    qtbot.addWidget(page)
    page.resize(900, 760)
    page.show()
    page._switch_settings_section(1)
    page._stack.widget(1).select_section("look")
    qtbot.waitUntil(lambda: page._language_combo.isVisible())
    button = next(button for button in page.findChildren(QPushButton) if button.text() == "Open language packs folder")
    assert button.width() >= button.sizeHint().width()
