import json
from argparse import Namespace
from pathlib import Path
from zipfile import ZipFile

import pytest

from src.plugins.runtime import manager as runtime
from src.plugins.runtime.registry import PluginState
from src.utils import i18n
from tests.plugins.test_runtime import Host


def write_plugin_files(root: Path, files: dict[str, str]) -> None:
    """在隔离的测试源码或构建目录中写入插件文件。"""
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents.encode("utf-8"))


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
    expected_ids = {json.loads(path.read_text(encoding="utf-8"))["id"] for path in build.PLUGINS.glob("*/plugin.json")}
    expected_files = {
        f"plugins/{path.relative_to(build.PLUGINS).as_posix()}"
        for path in build.PLUGINS.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in (".pyc", ".pyo", ".log", ".stackdump")
    }
    released = temp_data_dir / "released"
    with ZipFile(archive) as package:
        names = {name.split("/", 1)[1] for name in package.namelist()}
        assert names == {"app.exe", *expected_files}
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
        for plugin_id in ("example.mod-inventory", "example.preset-cycle", "example.preset-audit"):
            record = manager.get(plugin_id)
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


def test_staging_scans_all_source_plugins_without_copying_runtime_files(tmp_path, monkeypatch):
    from scripts.release import build

    source = tmp_path / "source"
    source_files = {
        "release.alpha/plugin.json": json.dumps({"id": "release.alpha", "files": ["entry.py"]}),
        "release.alpha/entry.py": "pass\n",
        "release.alpha/modules/helper.py": "pass\n",
        "release.alpha/assets/schema.json": "{}",
        "release.alpha/languages/en-US/strings.json": "{}",
        "release.beta/plugin.json": json.dumps({"id": "release.beta"}),
        "release.beta/entry.py": "pass\n",
    }
    runtime_files = {
        "release.alpha/__pycache__/entry.pyc": "bytecode",
        "release.alpha/__pycache__/metadata.json": "cache",
        "release.alpha/helper.pyc": "bytecode",
        "release.alpha/helper.pyo": "bytecode",
        "release.alpha/run.log": "log",
        "release.alpha/crash.stackdump": "dump",
    }
    write_plugin_files(source, source_files | runtime_files)
    monkeypatch.setattr(build, "PLUGINS", source)
    product = tmp_path / "product"
    write_plugin_files(product / "plugins", {"local.extra/entry.py": "private", "release.alpha/obsolete.py": "old"})

    target = build.stage_plugins(product)

    assert {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} == set(source_files)
    for relative, contents in source_files.items():
        assert (target / relative).read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("onefile", [False, True], ids=["standalone", "onefile"])
def test_source_plugin_changes_update_zip_and_manifest_without_build_edits(tmp_path, monkeypatch, onefile):
    """连续构建验证插件及子文件增删，并覆盖没有源码插件时的清单与归档。"""
    from scripts.release import build

    source = tmp_path / "source"
    source_files = {
        "release.alpha/plugin.json": json.dumps({"id": "release.alpha", "files": ["entry.py"]}),
        "release.alpha/entry.py": "pass\n",
        "release.alpha/obsolete.py": "old",
        "release.retired/plugin.json": json.dumps({"id": "release.retired"}),
        "release.retired/entry.py": "pass\n",
    }
    write_plugin_files(source, source_files)
    monkeypatch.setattr(build, "PLUGINS", source)
    monkeypatch.setattr(build, "PROJECT_BUILD", tmp_path)
    product = tmp_path / "product"
    product.mkdir()
    executable = product / "app.exe"
    executable.write_bytes(b"program")

    def assert_release_matches_source() -> None:
        """校验当前源码快照对应的受管清单和归档，排除构建目录中的本地文件。"""
        plugins = build.stage_plugins(product)
        write_plugin_files(
            product,
            {
                "plugins/release.alpha/private.json": "private",
                "plugins/local.extra/entry.py": "private",
                "config/plugin_state.json": "private",
                "data/plugins_data/release.alpha/storage.json": "private",
            },
        )
        entries = [(executable, ""), (plugins, "plugins")] if onefile else [(product, "")]
        manifest = build.generate_release_manifest(entries, product)
        managed, preserve = manifest.read_text(encoding="utf-8").split("[preserve]")
        expected_files = {f"plugins/{relative}" for relative in source_files}
        assert {line for line in managed.splitlines() if line} == {"[managed]", "app.exe", *expected_files}
        assert "plugins/" in preserve.splitlines()
        if onefile:
            entries.append((manifest, ""))
        with ZipFile(build.make_zip(entries)) as package:
            names = {name.split("/", 1)[1] for name in package.namelist()}
            assert names == {"app.exe", "files.manifest", *expected_files}
            for relative, contents in source_files.items():
                name = next(name for name in package.namelist() if name.endswith(f"/plugins/{relative}"))
                assert package.read(name).decode("utf-8") == contents

    assert_release_matches_source()
    for relative in ("release.alpha/obsolete.py", "release.retired/plugin.json", "release.retired/entry.py"):
        (source / relative).unlink()
        del source_files[relative]
    additions = {
        "release.alpha/modules/added.py": "new",
        "release.added/plugin.json": json.dumps({"id": "release.added"}),
        "release.added/entry.py": "pass\n",
    }
    source_files.update(additions)
    write_plugin_files(source, additions)
    assert_release_matches_source()
    for relative in source_files:
        (source / relative).unlink()
    source_files.clear()
    assert_release_matches_source()
