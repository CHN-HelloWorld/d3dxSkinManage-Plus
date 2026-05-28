"""验证 ModVariantManager 对启用和禁用资源的命名空间归属解析。

变体采集可能发生在资源尚未启用或即将禁用时，因此隐式命名空间不能依赖
is_enabled 或当前 deployed_path。
"""

from __future__ import annotations

from pathlib import Path

from src.core.mod_variant_manager import ModVariantManager
from src.models.mod import Mod, ModStatus


def _mod(**kw) -> Mod:
    base = {
        "path": "D:/lib/AlphaSkin/AlphaSkin.zip",
        "folder_name": "AlphaSkin",
        "mod_id": "dep-a",
        "status": ModStatus.DISABLED,
    }
    base.update(kw)
    return Mod(**base)


def test_implicit_prefix_works_for_disabled_mod():
    assert ModVariantManager._implicit_prefix(_mod(), "mods") == "mods\\alphaskin"


def test_implicit_prefix_ignores_deployed_path_and_enabled_state():
    enabled = _mod(status=ModStatus.ENABLED, deployed_path="D:/3DMigoto/Mods/AlphaSkin")
    assert ModVariantManager._implicit_prefix(enabled, "mods") == ModVariantManager._implicit_prefix(_mod(), "mods")


def test_implicit_prefix_falls_back_to_body_stem():
    assert ModVariantManager._implicit_prefix(_mod(folder_name=""), "mods") == "mods\\alphaskin"


def test_implicit_prefix_empty_without_deploy_root_prefix():
    assert ModVariantManager._implicit_prefix(_mod(), "") == ""


def test_implicit_prefix_skips_parked_name():
    parked = _mod(folder_name="DISABLED_abc123def456_AlphaSkin")
    assert ModVariantManager._implicit_prefix(parked, "mods") == ""


def test_implicit_prefix_supports_nested_deploy_root():
    assert ModVariantManager._implicit_prefix(_mod(), "mods\\characters") == "mods\\characters\\alphaskin"


class _FakeStorage:
    def __init__(self, deploy_dir):
        self._d = deploy_dir

    def get_deploy_dir(self):
        return self._d


class _FakeManager:
    def __init__(self, deploy_dir):
        self._storage = _FakeStorage(deploy_dir)


def test_deploy_root_prefix_relative_to_migoto(tmp_path, monkeypatch):
    migoto = tmp_path / "3DMigoto"
    deploy = migoto / "Mods"
    deploy.mkdir(parents=True)
    mgr = ModVariantManager(_FakeManager(deploy))
    monkeypatch.setattr(mgr, "_migoto_root", lambda game_id="": migoto)
    assert mgr._deploy_root_prefix("g") == "mods"


def test_deploy_root_prefix_empty_when_outside_migoto(tmp_path, monkeypatch):
    migoto = tmp_path / "3DMigoto"
    migoto.mkdir()
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    mgr = ModVariantManager(_FakeManager(outside))
    monkeypatch.setattr(mgr, "_migoto_root", lambda game_id="": migoto)
    assert mgr._deploy_root_prefix("g") == ""


def test_deploy_root_prefix_empty_without_migoto_path(tmp_path):
    mgr = ModVariantManager(_FakeManager(tmp_path / "Mods"))
    mgr._migoto_root = lambda game_id="": None
    assert mgr._deploy_root_prefix("g") == ""


def test_prefs_and_user_ini_paths_require_both_prerequisites(tmp_path, monkeypatch):
    class _M:
        def __init__(self, res):
            self._res = res
            self._storage = _FakeStorage(None)

        def _res_dir(self, game_id):
            return self._res

        @property
        def current_game_id(self):
            return "g"

        _game_service = None

    import src.core.mod_variant_manager as mod

    monkeypatch.setattr(mod, "get_data_dir", lambda: tmp_path)
    mgr = ModVariantManager(_M(""))
    assert mgr.prefs_file() is None
    mgr2 = ModVariantManager(_M("Probe"))
    assert mgr2.prefs_file() == tmp_path / "Probe" / "mod_preferences.json"
    assert "mods" not in Path("Probe/mod_preferences.json").parts[:-1]
