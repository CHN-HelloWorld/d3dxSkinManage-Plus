"""验证插件包导入隔离、嵌套模块和完整重载。"""

import sys

import pytest

from src.plugins.runtime.manifest import parse_manifest
from tests.plugins.test_manifest_v2 import make_plugin


def test_relative_imports_support_nested_packages_and_dataclasses(tmp_path):
    from src.plugins.runtime.module_loader import PluginModuleLoader

    directory = make_plugin(tmp_path)
    package = directory / "logic"
    package.mkdir()
    (package / "__init__.py").write_text("from .model import Item\n", encoding="utf-8")
    (package / "model.py").write_text(
        "from dataclasses import dataclass\n@dataclass\nclass Item:\n    value: int = 17\n", encoding="utf-8"
    )
    (directory / "entry.py").write_text("from .logic import Item\nVALUE = Item().value\n", encoding="utf-8")
    before = list(sys.path)
    loader = PluginModuleLoader(parse_manifest(directory))
    try:
        assert loader.load().VALUE == 17
        assert sys.path == before
        assert loader.namespace + ".logic.model" in sys.modules
    finally:
        loader.unload()
    assert not any(name == loader.namespace or name.startswith(loader.namespace + ".") for name in sys.modules)
    assert loader not in sys.meta_path


def test_similar_plugin_ids_have_independent_modules_and_reload_reads_source(tmp_path):
    from src.plugins.runtime.module_loader import PluginModuleLoader

    loaders = []
    try:
        for plugin_id, value in [("test.a-b", 11), ("test.a_b", 22)]:
            directory = make_plugin(tmp_path, plugin_id)
            (directory / "helper.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
            (directory / "entry.py").write_text("from .helper import VALUE\n", encoding="utf-8")
            loader = PluginModuleLoader(parse_manifest(directory))
            loaders.append(loader)
            assert loader.load().VALUE == value
        assert loaders[0].namespace != loaders[1].namespace
        loaders[0].unload()
        (tmp_path / "test.a-b/helper.py").write_text("VALUE = 33\n", encoding="utf-8")
        replacement = PluginModuleLoader(parse_manifest(tmp_path / "test.a-b"))
        loaders.append(replacement)
        assert replacement.load().VALUE == 33
        assert sys.modules[loaders[1].namespace + ".helper"].VALUE == 22
    finally:
        for loader in loaders:
            loader.unload()


def test_failed_import_releases_all_modules_and_the_finder(tmp_path):
    from src.plugins.runtime.module_loader import PluginModuleLoader

    directory = make_plugin(tmp_path)
    (directory / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
    (directory / "entry.py").write_text("from .helper import VALUE\nraise RuntimeError('failed')\n", encoding="utf-8")
    loader = PluginModuleLoader(parse_manifest(directory))
    with pytest.raises(RuntimeError, match="failed"):
        loader.load()
    assert loader not in sys.meta_path
    assert not any(name.startswith(loader.namespace) for name in sys.modules)
