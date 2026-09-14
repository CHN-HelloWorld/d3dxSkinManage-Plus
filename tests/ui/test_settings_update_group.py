def test_settings_has_update_group(qtbot, temp_data_dir):
    from src.ui.pages.settings import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    assert hasattr(page, "_upd_enable_check")
    assert hasattr(page, "_upd_periodic_combo")
    assert hasattr(page, "_upd_notify_combo")


def test_settings_uses_unified_import_format(qtbot, temp_data_dir):
    from src.ui.pages.settings import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)
    # 设置页仅提供归档存储，不显示文件夹部署方式选项。
    assert [page._import_format_combo.itemData(i) for i in range(page._import_format_combo.count())] == [
        "zip",
        "7z",
        "rar",
    ]
    assert hasattr(page, "_import_strict_format_check")
    assert not hasattr(page, "_deploy_method_combo")
    assert not hasattr(page, "_compress_check")
    assert not hasattr(page, "_compress_format_combo")


def test_settings_delete_method_autosaves(qtbot, temp_data_dir):
    from src.core.config import Config
    from src.ui.pages.settings import SettingsPage

    page = SettingsPage()
    qtbot.addWidget(page)

    assert [page._delete_method_combo.itemData(i) for i in range(page._delete_method_combo.count())] == [
        "recycle_bin",
        "permanent",
    ]
    assert page._delete_method_combo.currentData() == "permanent"

    page._delete_method_combo.setCurrentIndex(0)
    assert Config().get("mods.delete_method") == "recycle_bin"
