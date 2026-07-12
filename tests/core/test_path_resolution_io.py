from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_index import ModIndex
from src.utils import paths
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games

_real_get_app_dir = paths.get_app_dir


@pytest.fixture(params=["development", "portable", "fallback"])
def application_root(request, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_app_dir", _real_get_app_dir)
    application = tmp_path / "application"
    if request.param == "development":
        module = application / "src/utils/paths.py"
        module.parent.mkdir(parents=True)
        module.touch()
        monkeypatch.setattr(paths, "__file__", str(module))
        expected = application
    else:
        application.mkdir()
        monkeypatch.setattr(sys, "executable", str(application / "manager.exe"))
        monkeypatch.setattr(paths, "__compiled__", SimpleNamespace(onefile=False), raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
        expected = application
        if request.param == "fallback":
            monkeypatch.setattr(paths, "_is_writable", lambda directory: False)
            expected = tmp_path / "AppData/d3dxSkinManage-Plus"
    assert paths.get_app_dir() == expected
    assert expected.is_dir()
    return expected


def test_index_path_roundtrip_does_not_repeat_filesystem_probes(application_root, monkeypatch):
    root = application_root
    index = ModIndex("Game")
    mod = make_mod(index, "Skin")
    source = mod.path
    preview = str(Path(source).parent / ".d3dxmeta/previews/main.png")
    mod.preview_images = [preview]
    deployment = str(root / "external/Mods/Skin")
    mod.info.custom_data = {"gamebanana": {"mod_id": 123}}
    from src.models.deployment import DeploymentRecord, DeploymentState

    try:
        assert index.add_mod(mod)
        index.upsert_deployment(DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, deployment))

        def unexpected_io(*args, **kwargs):
            pytest.fail("Converting stored paths repeated application-directory filesystem I/O")

        with monkeypatch.context() as guard:
            guard.setattr(Path, "resolve", unexpected_io)
            guard.setattr(Path, "mkdir", unexpected_io)
            guard.setattr(Path, "stat", unexpected_io)
            for _ in range(3):
                (restored,) = index.get_all_mods("Game")
                assert restored.path == source
                assert restored.preview_images == [preview]
                assert restored.deployed_path == deployment
                assert restored.info.custom_data["gamebanana"]["mod_id"] == 123
                serialized = restored.to_dict()
                assert serialized["path"] == source
                assert serialized["preview_images"] == [preview]
                assert serialized["deployed_path"] == deployment
                assert serialized["info"]["custom_data"]["gamebanana"]["mod_id"] == 123
    finally:
        index.close()
