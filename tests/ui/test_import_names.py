"""验证导入项的默认名称完整传递到资源库及本地元数据。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_intake import ModIntakeService
from src.core.mod_package import read_sidecar
from src.ui.dialogs.mod_import import _ImportEntry
from tests.core.test_intake_source_name import library as library


@pytest.mark.parametrize(
    ("source_name", "is_directory", "expected"),
    [
        ("Skin.v1.2", True, "Skin.v1.2"),
        ("7.2版本合集", True, "7.2版本合集"),
        ("Skin.zip", True, "Skin.zip"),
        ("Skin.7z", True, "Skin.7z"),
        ("Skin.rar", True, "Skin.rar"),
        ("Skin.v1.2.zip", False, "Skin.v1.2"),
        ("7.2版本合集.ZIP", False, "7.2版本合集"),
    ],
)
def test_default_import_name_preserves_dots_through_persistence(library, tmp_path, source_name, is_directory, expected):
    """目录保留完整基名，真实归档只移除最后一个格式后缀，显示名与容器名一致。"""
    manager, node = library
    source = tmp_path / source_name
    if is_directory:
        source.mkdir()
        (source / "mod.ini").write_bytes(b"[Constants]\nx = 1\n")
    else:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("mod.ini", b"[Constants]\nx = 1\n")
    entry = _ImportEntry(str(source))
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=entry.source_path,
        game_id=manager.current_game_id,
        node_id=node.id,
        display_name=entry.name,
        target_format="zip",
    )

    mod = service.commit(service.prepare(request))

    assert entry.is_archive is not is_directory
    assert entry.name == expected
    assert mod.name == expected
    assert mod.folder_name == expected
    assert Path(mod.path).parent.name == expected
    assert read_sidecar(mod.path)["name"] == expected
    assert manager.get_mod(mod.mod_id).name == expected
