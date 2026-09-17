from __future__ import annotations

import json

import pytest


def write_pack(root, code, *, messages=None, gamebanana=None, qt=None, **metadata):
    path = root / code / "strings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_meta": {"name": code, **metadata},
                "messages": messages or {},
                "gamebanana": gamebanana or {},
                "qt": qt or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_default_catalog_reads_language_packs_from_config_only(temp_data_dir):
    from src.utils.i18n import I18n

    write_pack(temp_data_dir / "config/languages", "de-DE", name="Deutsch", messages={"打开": "Aus der Konfiguration"})
    write_pack(temp_data_dir / "languages", "de-DE", messages={"打开": "Old directory"})
    instance = I18n()
    assert instance.set_language("de-DE")
    assert instance.t("打开") == "Aus der Konfiguration"


def test_missing_config_pack_does_not_load_an_old_language_directory(temp_data_dir):
    from src.utils.i18n import I18n

    write_pack(temp_data_dir / "languages", "de-DE", messages={"打开": "Old directory"})
    instance = I18n()
    assert not instance.set_language("de-DE")
    assert instance.t("打开") == "打开"


@pytest.fixture
def language_dir(tmp_path):
    directory = tmp_path / "config/languages"
    write_pack(
        directory,
        "zh-CN",
        name="简体中文",
        messages={"打开": "打开"},
        gamebanana={"Skins": "皮肤", "Characters": "角色"},
    )
    write_pack(
        directory, "en-US", name="English", messages={"打开": "Open", "关闭": "Close"}, gamebanana={"Skins": "Skins"}
    )
    return directory


def catalog(language_dir):
    from src.utils.i18n import I18n

    return I18n(directory=language_dir)


def test_folder_name_discovers_an_unlisted_language_and_formats_before_display(language_dir):
    write_pack(
        language_dir,
        "ja-JP",
        name="日本語",
        messages={
            "打开": "開く",
            "已导入 {count} 个 Mod": "Mod を {count} 件取り込みました",
        },
    )
    i18n = catalog(language_dir)
    assert ("ja-JP", "日本語") in i18n.available_languages
    i18n.set_language("ja-JP")
    assert i18n.current_language == "ja-JP"
    assert i18n.t("打开") == "開く"
    assert i18n.t("已导入 {count} 个 Mod", count=2) == "Mod を 2 件取り込みました"


def test_partial_pack_uses_source_text_for_missing_entries(language_dir):
    write_pack(language_dir, "en-US", name="My English", messages={"打开": "Open it"})
    i18n = catalog(language_dir)
    i18n.set_language("en_US")
    assert i18n.t("打开") == "Open it"
    assert i18n.t("关闭") == "关闭"
    assert i18n.t("没有翻译的文本") == "没有翻译的文本"
    assert [name for code, name in i18n.available_languages if code == "en-US"] == ["My English"]


def test_categories_follow_selected_pack_and_missing_names_remain_in_source_language(language_dir):
    write_pack(language_dir, "ja-JP", gamebanana={"Skins": "スキン"})
    i18n = catalog(language_dir)
    i18n.set_language("ja-JP")
    assert i18n.category_name("Skins") == "スキン"
    assert i18n.category_name("skins") == "スキン"
    assert i18n.category_name("Characters") == "Characters"
    assert i18n.category_name("A New Character") == "A New Character"
    assert i18n.category_name("") == ""
    i18n.set_language("en-US")
    assert i18n.category_name("Skins") == "Skins"


def test_explicit_fallback_language_is_used_once_even_when_it_forms_a_cycle(language_dir):
    write_pack(language_dir, "pt-BR", fallback="en-US", messages={"打开": "Abrir"})
    write_pack(language_dir, "en-US", fallback="pt-BR", messages={"关闭": "Close"})
    i18n = catalog(language_dir)
    i18n.set_language("pt-BR")
    assert i18n.t("打开") == "Abrir"
    assert i18n.t("关闭") == "Close"
    assert i18n.t("缺失") == "缺失"


@pytest.mark.parametrize("value", ["../../outside", "..", "a/b", "a\\b", "", "C:en-US"])
def test_invalid_language_paths_cannot_be_loaded(language_dir, value):
    i18n = catalog(language_dir)
    i18n.set_language(value)
    assert i18n.current_language == "zh-CN"
    assert i18n.t("打开") == "打开"


@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"messages": [], "_meta": {"name": "Broken"}}', '{"messages": {"打开": "One", "打开": "Two"}}'],
)
def test_corrupt_pack_uses_source_text_without_overwriting_the_file(language_dir, content):
    path = write_pack(language_dir, "en-US")
    path.write_text(content, encoding="utf-8")
    i18n = catalog(language_dir)
    i18n.set_language("en-US")
    assert i18n.t("打开") == "打开"
    assert i18n.diagnostics
    assert path.read_text(encoding="utf-8") == content


def test_bad_entries_and_placeholders_are_ignored_individually(language_dir):
    write_pack(
        language_dir,
        "en-US",
        messages={
            "打开": 123,
            "关闭": "Shut",
            "已导入 {count} 个 Mod": "Imported {unknown} mods",
            "进度 {count}": "Progress {count",
            "无译文": "",
        },
    )
    i18n = catalog(language_dir)
    i18n.set_language("en-US")
    assert i18n.t("打开") == "打开"
    assert i18n.t("关闭") == "Shut"
    assert i18n.t("已导入 {count} 个 Mod", count=3) == "已导入 3 个 Mod"
    assert i18n.t("进度 {count}", count=4) == "进度 4"
    assert i18n.t("无译文") == "无译文"
    assert len(i18n.diagnostics) >= 3


def test_invalid_format_specifier_falls_back_without_crashing(language_dir):
    write_pack(language_dir, "en-US", messages={"进度 {count}": "Progress {count:invalid}"})
    i18n = catalog(language_dir)
    i18n.set_language("en-US")
    assert i18n.t("进度 {count}", count=4) == "进度 4"


@pytest.mark.parametrize("translated", ["Progress {count!q}", "Progress {count:invalid}", "Progress {count:999999999}"])
def test_invalid_or_changed_formatting_is_reported_before_rendering(language_dir, translated):
    from src.utils.language_pack import read_language_pack

    path = write_pack(language_dir, "en-US", messages={"进度 {count}": translated})
    pack = read_language_pack(path)
    assert pack.diagnostics
    assert "进度 {count}" not in pack.messages


def test_misspelled_sections_and_boolean_format_versions_are_rejected(language_dir):
    from src.utils.language_pack import read_language_pack

    path = write_pack(language_dir, "en-US")
    path.write_text('{"_meta":{"format":true},"messages":{}}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_language_pack(path)
    path.write_text('{"_meta":{},"message":{"打开":"Open"}}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_language_pack(path)


def test_reloading_the_same_language_picks_up_user_edits(language_dir):
    i18n = catalog(language_dir)
    i18n.set_language("en-US")
    write_pack(language_dir, "en-US", messages={"打开": "Updated"})
    i18n.set_language("en-US")
    assert i18n.t("打开") == "Updated"


def test_utf8_bom_and_plain_source_translation_dictionary_are_supported(language_dir):
    path = language_dir / "fr-FR" / "strings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"打开": "Ouvrir"}, ensure_ascii=False), encoding="utf-8-sig")
    i18n = catalog(language_dir)
    i18n.set_language("fr-FR")
    assert i18n.t("打开") == "Ouvrir"


def test_custom_locale_is_preserved_by_config_reload(temp_data_dir):
    from src.core.config import Config

    config = Config()
    assert config.set_atomic("general.language", "pt-BR")
    config.reload()
    assert config.get("general.language") == "pt-BR"


@pytest.mark.parametrize("value", ["../pt-BR", "pt/BR", "", 7])
def test_config_reload_discards_invalid_language_directory_names(temp_data_dir, value):
    from src.core.config import Config

    config = Config()
    assert config.set_atomic("general.language", value)
    config.reload()
    assert config.get("general.language") == "zh-CN"
