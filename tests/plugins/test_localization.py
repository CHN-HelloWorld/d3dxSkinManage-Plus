import json

from src.utils import i18n
from tests.plugins.test_runtime import activate, write_plugin
from tests.plugins.test_runtime import env as env


def write_pack(directory, locale, messages, *, fallback=""):
    path = directory / "languages" / locale / "strings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"_meta": {"format": 1, "fallback": fallback}, "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_catalogs_are_isolated_and_follow_the_current_host_language(env, monkeypatch):
    manager, directory, _ = env
    first = write_plugin(directory)
    second = write_plugin(directory, "test.other")
    write_pack(first, "en-US", {"共 {count} 项": "{count} items"})
    write_pack(second, "en-US", {"共 {count} 项": "{count} entries"})
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    one = activate(manager).context
    manager.trust("test.other")
    manager.enable("test.other")
    two = manager.get("test.other").context
    assert one.i18n.language == "en-US"
    assert one.i18n.t("共 {count} 项", count=2) == "2 items"
    assert two.i18n.t("共 {count} 项", count=2) == "2 entries"
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "zh-CN")
    assert one.i18n.language == "zh-CN"
    assert one.i18n.t("共 {count} 项", count=2) == "共 2 项"


def test_fallback_and_invalid_placeholders_keep_the_plugin_usable(env, monkeypatch):
    manager, directory, _ = env
    plugin = write_plugin(directory)
    write_pack(plugin, "zh-CN", {"Label": "默认名称"})
    write_pack(plugin, "en-US", {"共 {count} 项": "{wrong} items"}, fallback="zh-CN")
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    context = activate(manager).context
    assert context.i18n.t("Label") == "默认名称"
    assert context.i18n.t("共 {count} 项", count=2) == "共 2 项"
    assert context.i18n.t("未知 {value}", value="text") == "未知 text"
    assert context.i18n.t("未知 {value}", unrelated=1) == "未知 {value}"


def test_contribution_titles_and_inactive_metadata_use_the_same_catalog(env, monkeypatch):
    manager, directory, _ = env
    plugin = write_plugin(
        directory,
        name="插件名称",
        description="插件说明",
        permissions=["ui:tools-page"],
        contributes={
            "commands": [{"id": "test.sample.command", "title": "命令名称"}],
            "toolsPage": [{"command": "test.sample.command"}],
        },
    )
    write_pack(
        plugin, "en-US", {"插件名称": "Example plugin", "插件说明": "Plugin description", "命令名称": "Example command"}
    )
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    record = activate(manager)
    assert record.name == "Example plugin"
    assert record.description == "Plugin description"
    (contribution,) = manager.contributions.tools_page()
    assert contribution.title == "Example command"
    assert contribution.plugin_name == "Example plugin"
    manager.disable(record.id)
    assert record.name == "Example plugin"


def test_reload_reads_updated_plugin_catalog(env, monkeypatch):
    manager, directory, _ = env
    plugin = write_plugin(directory)
    write_pack(plugin, "en-US", {"标题": "First"})
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    record = activate(manager)
    assert record.context.i18n.t("标题") == "First"
    write_pack(plugin, "en-US", {"标题": "Later"})
    assert manager.reload(record.id)
    assert manager.get(record.id).context.i18n.t("标题") == "Later"


def test_absent_or_broken_catalog_never_prevents_activation(env, monkeypatch):
    manager, directory, _ = env
    plugin = write_plugin(directory)
    pack = write_pack(plugin, "en-US", {})
    pack.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    context = activate(manager).context
    assert context.i18n.language == "en-US"
    assert context.i18n.t("Hello {name}", name="world") == "Hello world"


def test_plugin_page_uses_plugin_catalog_for_metadata_and_contribution_titles(env, monkeypatch, qtbot):
    from PySide6.QtWidgets import QLabel

    from src.ui.pages.plugins import PluginsPage

    manager, directory, _ = env
    plugin = write_plugin(
        directory,
        name="插件名称",
        description="插件说明",
        permissions=["storage", "ui:settings"],
        contributes={"settings": [{"key": "label", "type": "str", "title": "设置名称", "default": ""}]},
    )
    write_pack(
        plugin, "en-US", {"插件名称": "Example plugin", "插件说明": "Plugin description", "设置名称": "Example setting"}
    )
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    activate(manager)
    page = PluginsPage()
    qtbot.addWidget(page)
    page.set_manager(manager)
    assert page._entries["test.sample"].accessibleName() == "Example plugin"
    labels = [label.text() for label in page.findChildren(QLabel)]
    assert "Example plugin" in labels
    assert "Plugin description" in labels
    assert "Example setting" in labels


def test_plugin_shortcut_titles_are_not_translated_again_by_host(env, monkeypatch, qtbot, tmp_path):
    from PySide6.QtWidgets import QLabel, QWidget

    from src.plugins.runtime.shortcuts import PluginShortcutHost
    from src.ui.pages.shortcuts import ShortcutsPage
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    host_root = tmp_path / "host-pack"
    write_pack(host_root, "en-US", {"Copy": "HOST COPY"})
    host = i18n.I18n(directory=host_root / "languages")
    host.set_language("en-US")
    monkeypatch.setattr(i18n, "_i18n", host)
    manager, directory, _ = env
    plugin = write_plugin(
        directory,
        permissions=["ui:shortcuts"],
        contributes={
            "commands": [{"id": "test.sample.command", "title": "标题"}],
            "shortcuts": [{"command": "test.sample.command", "default": "Ctrl+Alt+L"}],
        },
    )
    write_pack(plugin, "en-US", {"标题": "Copy"})
    window = QWidget()
    qtbot.addWidget(window)
    settings = ShortcutSettings(window)
    manager.set_shortcut_host(PluginShortcutHost(settings, ShortcutDispatcher(settings, window)))
    activate(manager)
    page = ShortcutsPage(settings)
    qtbot.addWidget(page)
    row = page._rows["test.sample.command"]
    assert "Copy" in [label.text() for label in row.findChildren(QLabel)]
    page.open_editor("test.sample.command")
    assert "Copy" in [label.text() for label in page._dialog.findChildren(QLabel)]
    page._dialog.reject()
