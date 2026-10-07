"""使用独立包命名空间加载插件源码，支持相对导入及无字节码缓存的重载。"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType

from src.plugins.runtime.manifest import Manifest


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """直接编译当前源文件，避免同大小且时间戳相同的编辑命中旧字节码。"""

    def get_code(self, fullname: str):
        """只读取源码，不读取或写入 __pycache__。"""
        return compile(Path(self.path).read_bytes(), self.path, "exec", dont_inherit=True)


class PluginModuleLoader(importlib.abc.MetaPathFinder):
    """每次激活拥有独立命名空间和查找器，停用时完整撤销。"""

    def __init__(self, manifest: Manifest) -> None:
        """绑定插件根目录，并生成不会与其他插件或旧实例冲突的包名。"""
        self._manifest = manifest
        self._directory = manifest.directory.resolve()
        self.namespace = f"_d3dx_plugin_{manifest.id.encode('utf-8').hex()}_{uuid.uuid4().hex}"

    def find_spec(self, fullname: str, path=None, target=None):
        """仅处理本插件命名空间，按插件目录解析包、模块与命名空间子包。"""
        if fullname != self.namespace and not fullname.startswith(self.namespace + "."):
            return None
        relative = fullname[len(self.namespace) :].lstrip(".")
        parts = relative.split(".") if relative else []
        candidate = self._directory.joinpath(*parts)
        source = candidate / "__init__.py" if candidate.is_dir() else candidate.with_suffix(".py")
        if not source.resolve().is_relative_to(self._directory):
            raise ImportError(f"插件模块越出根目录: {fullname}")
        if source.is_file():
            loader = _SourceLoader(fullname, str(source))
            return importlib.util.spec_from_file_location(
                fullname,
                source,
                loader=loader,
                submodule_search_locations=[str(candidate)] if candidate.is_dir() else None,
            )
        if candidate.is_dir():
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(candidate)]
            return spec
        raise ModuleNotFoundError(f"插件包内没有模块: {fullname}", name=fullname)

    def load(self) -> ModuleType:
        """导入入口及其相对依赖；任何导入异常均先回收已创建的模块。"""
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)
        parts = list(Path(self._manifest.entry).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join([self.namespace, *parts])
        try:
            return importlib.import_module(name)
        except BaseException:
            self.unload()
            raise

    def unload(self) -> None:
        """撤销查找器并移除全部所属模块，保留其他插件和共享依赖。"""
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in tuple(sys.modules):
            if name == self.namespace or name.startswith(self.namespace + "."):
                sys.modules.pop(name, None)
