from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import write_container_marker
from src.core.mod_storage import ModStorage
from src.models.mod import Mod
from src.utils import fs
from tests.helpers import registered_games as registered_games


def test_recycle_delete_moves_complete_container(tmp_path, monkeypatch):
    container = tmp_path / "Container"
    body = container / "Body"
    body.mkdir(parents=True)
    assert write_container_marker(container, body.name, "0" * 40, imported_at="2026-10-05T10:00:00")

    moved: list[Path] = []
    monkeypatch.setattr(fs, "move_to_trash", lambda path: moved.append(Path(path)) or "")

    assert ModStorage().delete_source(body, delete_method="recycle_bin")
    assert moved == [container]


def test_long_path_recycle_preflight_happens_before_undeploy(temp_data_dir):
    mod = Mod(mod_id="long", name="Long", path=r"D:\mods\Long")

    class _Index:
        @staticmethod
        def get_mod(_mod_id):
            return mod

    class _Storage:
        @staticmethod
        def ensure_source_recycle_compatible(_path):
            raise fs.RecycleBinError(_path, path_too_long=True)

    manager = ModManager()
    manager._index.close()
    manager._index = _Index()
    manager._storage = _Storage()

    with pytest.raises(fs.RecycleBinError) as captured:
        manager.delete_mod(mod.mod_id, delete_method="recycle_bin")

    assert captured.value.path_too_long is True
