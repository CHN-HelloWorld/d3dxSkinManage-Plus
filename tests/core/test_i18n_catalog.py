import json
from pathlib import Path


def test_extraction_recognizes_pure_markers_in_data_producers():
    from scripts.i18n.i18n_catalog import extract_messages

    source = """from src.utils.language_pack import N_ as source_text
PHASE = source_text("读取记录")
CHOICES = [("official", source_text("GitHub 官方直连"))]
"""
    assert set(extract_messages(source)) == {"读取记录", "GitHub 官方直连"}


def test_dynamic_ui_messages_are_extracted_and_have_english_translations():
    from scripts.i18n.i18n_catalog import collect_catalog
    from src.utils.constants import APP_UPDATE_DOWNLOAD_MIRRORS
    from src.utils.i18n import I18n

    messages = {
        "读取记录",
        "扫描文件",
        "统计容量",
        "准备目录",
        "比较分类",
        "导入分类",
        "系统无法将该路径放入回收站",
        "路径或其内部文件超过 Windows 回收站支持的长度",
        "当前系统不支持回收站操作",
        "当前系统或文件位置不支持回收站操作",
        "系统回收站操作失败",
    }
    messages.update(name for _key, name, _url in APP_UPDATE_DOWNLOAD_MIRRORS)
    assert messages <= set(collect_catalog())
    root = Path(__file__).resolve().parents[2]
    catalog = I18n(directory=root / "config/languages")
    assert catalog.set_language("en-US")
    assert all(catalog.t(source) != source for source in messages)


def test_extraction_handles_aliases_templates_and_deferred_labels():
    from scripts.i18n.i18n_catalog import extract_messages

    source = """from src.utils.i18n import t as tr, N_
LABEL = N_("等待下载")
def display(name):
    return tr("已选择 {name}", name=name), tr("设置")
"""
    messages = extract_messages(source)
    assert set(messages) == {"等待下载", "已选择 {name}", "设置"}


def test_new_language_pack_can_be_partial_and_never_overwrites_existing_work(tmp_path):
    from scripts.i18n.i18n_catalog import create_pack

    path = create_pack(tmp_path, "fr-FR", "Français", ["打开", "已选择 {name}"], ["Skins"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_meta"]["name"] == "Français"
    assert data["messages"] == {"打开": "", "已选择 {name}": ""}
    assert data["gamebanana"] == {"Skins": ""}
    path.write_text("user work", encoding="utf-8")
    try:
        create_pack(tmp_path, "fr-FR", "French", ["关闭"], [])
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing language pack was overwritten")
    assert path.read_text(encoding="utf-8") == "user work"
