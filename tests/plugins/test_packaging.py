import json
from argparse import Namespace
from zipfile import ZipFile

import pytest

from src.plugins.runtime import manager as runtime
from src.plugins.runtime.registry import PluginState
from src.utils import i18n
from tests.plugins.test_runtime import Host


@pytest.mark.parametrize("onefile", [False, True], ids=["standalone", "onefile"])
def test_build_keeps_plugin_sources_outside_the_executable(monkeypatch, onefile):
    from scripts.release import build

    monkeypatch.setattr(build, "have", lambda name: False)
    command = build.build_command(Namespace(onefile=onefile, console=False, nuitka_args=[]))
    assert "--include-package=src" in command
    assert not any("src.plugins.builtin" in argument for argument in command)


@pytest.mark.parametrize("onefile", [False, True], ids=["standalone", "onefile"])
def test_release_zip_plugins_load_translate_and_uninstall(temp_data_dir, monkeypatch, onefile):
    from scripts.release import build

    product = temp_data_dir / "product"
    product.mkdir()
    executable = product / "app.exe"
    executable.write_bytes(b"program")
    monkeypatch.setattr(build, "PROJECT_BUILD", temp_data_dir)
    plugins = build.stage_plugins(product)
    assert plugins == product / "plugins"

    # 程序运行产生的本地文件不得进入发行归档。
    for relative in (
        "plugins/local.extra/plugin.json",
        "plugins/example.mod-inventory/private.json",
        "plugins/example.mod-inventory/__pycache__/entry.pyc",
        "config/plugin_state.json",
        "data/plugins_data/example.mod-inventory/storage.json",
        "logs/run.log",
    ):
        path = product / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    entries = [(executable, ""), (plugins, "plugins")] if onefile else [(product, "")]
    archive = build.make_zip(entries)
    expected_ids = {
        "example.mod-inventory",
        "example.preset-cycle",
        "example.preset-audit",
        "official.legacy-migration",
    }
    released = temp_data_dir / "released"
    with ZipFile(archive) as package:
        names = {name.split("/", 1)[1] for name in package.namelist()}
        assert names == {"app.exe"} | {
            f"plugins/{pid}/{relative}"
            for pid in expected_ids
            for relative in ("plugin.json", "entry.py", "languages/en-US/strings.json", "README.md")
        } | {"plugins/example.mod-inventory/inventory.py"} | {
            f"plugins/official.legacy-migration/{relative}"
            for relative in ("legacy.py", "controller.py", "wizard.py", "preview.py", "languages/zh-CN/strings.json")
        }
        package.extractall(released)
    application = next(released.iterdir())
    plugin_root = application / "plugins"
    monkeypatch.setattr(runtime, "get_plugins_dir", lambda: plugin_root)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    manager = runtime.PluginManager(Host())
    try:
        assert manager.load_all() == 0
        assert {record.id for record in manager.records} == expected_ids
        for record in manager.records:
            assert record.manifest.directory == plugin_root / record.id
            assert manager.trust(record.id)
            assert manager.enable(record.id), record.error
            expected = PluginState.ACTIVE if record.id == "example.preset-audit" else PluginState.READY
            assert record.state is expected
        assert manager.get("example.mod-inventory").name == "Mod inventory export"
        assert manager.uninstall("example.mod-inventory")
        assert not (plugin_root / "example.mod-inventory").exists()
        assert manager.load_all() == 1
        assert manager.get("example.mod-inventory") is None
    finally:
        manager.shutdown()


def test_staging_copies_only_declared_example_assets(tmp_path, monkeypatch):
    from scripts.release import build

    source = tmp_path / "source"
    for plugin_id in (
        "example.mod-inventory",
        "example.preset-cycle",
        "example.preset-audit",
        "official.legacy-migration",
    ):
        directory = source / plugin_id
        (directory / "languages/en-US").mkdir(parents=True)
        (directory / "plugin.json").write_text(json.dumps({"id": plugin_id}), encoding="utf-8")
        (directory / "entry.py").write_text("# sample", encoding="utf-8")
        if plugin_id == "example.mod-inventory":
            (directory / "inventory.py").write_text("# 导出计算模块", encoding="utf-8")
        if plugin_id == "official.legacy-migration":
            for filename in ("legacy.py", "controller.py", "wizard.py", "preview.py"):
                (directory / filename).write_text("# 迁移模块", encoding="utf-8")
            (directory / "languages/zh-CN").mkdir()
            (directory / "languages/zh-CN/strings.json").write_text("{}", encoding="utf-8")
        (directory / "languages/en-US/strings.json").write_text("{}", encoding="utf-8")
        (directory / "README.md").write_text("# Example", encoding="utf-8")
        (directory / "local-settings.json").write_text("private", encoding="utf-8")
    (source / "third.party").mkdir()
    (source / "third.party/entry.py").write_text("# local", encoding="utf-8")
    monkeypatch.setattr(build, "PLUGINS", source)
    product = tmp_path / "product"
    previous = product / "plugins/third.party"
    previous.mkdir(parents=True)
    (previous / "entry.py").write_text("# installed during a local test run", encoding="utf-8")
    old_setting = product / "plugins/example.mod-inventory/local-settings.json"
    old_setting.parent.mkdir(parents=True)
    old_setting.write_text("private", encoding="utf-8")
    target = build.stage_plugins(product)
    assert not (target / "third.party").exists()
    assert list(target.rglob("local-settings.json")) == []
    assert len(list(target.glob("*/entry.py"))) == 4
