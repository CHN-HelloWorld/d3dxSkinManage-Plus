"""验证所有业务只读门面在权限、线程与停用状态下阻止访问宿主。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.plugin_sdk.errors import PermissionDenied, PluginError
from src.plugin_sdk.facades import GameFacade, ModFacade
from src.plugin_sdk.presets import PresetFacade
from src.plugin_sdk.variants import VariantFacade
from tests.plugins.test_facades import library as library


@pytest.mark.parametrize("resource", ["mods", "game", "presets", "variants"])
def test_reads_require_permission_and_live_owner_thread(library, resource):
    manager, mod, _ = library
    facades = {"mods": ModFacade, "game": GameFacade, "presets": PresetFacade, "variants": VariantFacade}

    def read(facade):
        """通过每类门面的实际查询入口触发边界检查。"""
        if resource == "mods":
            return facade.get_mods()
        if resource == "game":
            return facade.get_characters()
        if resource == "presets":
            return facade.get_presets()
        return facade.get_variants(mod.mod_id)

    forbidden = facades[resource](manager, frozenset())
    with pytest.raises(PermissionDenied):
        read(forbidden)
    allowed = facades[resource](manager, frozenset({"mods:read"}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read, allowed)
        with pytest.raises(PluginError):
            future.result(timeout=3)
    read(allowed)
    allowed._close()
    with pytest.raises(PluginError):
        read(allowed)
