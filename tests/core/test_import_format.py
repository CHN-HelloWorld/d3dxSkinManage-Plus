from __future__ import annotations

import zipfile
from pathlib import Path

from tests.helpers import registered_games as registered_games


def _zip(path: Path, top: str = "Skin") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{top}/mod.ini", "[Constants]\nx=1\n")
    return path


def _manager_with_character():
    from src.core.mod_manager import ModManager

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    return mm, char


def test_archive_import_keeps_original_format_when_not_strict(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from src.core.config import Config
    from src.services.gamebanana.models import GbInstallMetadata

    mm, char = _manager_with_character()
    Config().set("import.format", "7z")
    Config().set("import.strict_format", False)
    source = _zip(tmp_path / "download.zip")
    source_bytes = source.read_bytes()

    def _must_not_extract(*_args, **_kwargs):
        raise AssertionError("非强制压缩包导入不应解压")

    monkeypatch.setattr(mm._archive, "extract", _must_not_extract)
    gb_meta = GbInstallMetadata(
        mod_id=1,
        file_id=2,
        file_md5="abc",
        file_ts=3,
        page_url="https://gamebanana.com/mods/1",
        version="1.0",
        submitter_id=4,
        submitter_name="作者",
        last_checked_iso="",
    )
    mod = mm.import_mod(source_path=str(source), node_id=char.id, gb_meta=gb_meta)

    assert mod is not None
    body = Path(mod.path)
    assert body.is_file() and body.suffix == ".zip"
    assert body.read_bytes() == source_bytes
    assert mod.info.custom_data["gamebanana"]["mod_id"] == 1


def test_archive_import_converts_when_strict(temp_data_dir, tmp_path):
    from src.core.config import Config

    mm, char = _manager_with_character()
    Config().set("import.format", "7z")
    Config().set("import.strict_format", True)
    source = _zip(tmp_path / "download.zip")

    mod = mm.import_mod(source_path=str(source), node_id=char.id)

    assert mod is not None
    body = Path(mod.path)
    assert body.is_file() and body.suffix == ".7z"


def test_retired_folder_format_falls_back_to_archive(temp_data_dir, tmp_path):
    from src.core.config import Config

    mm, char = _manager_with_character()
    Config().set("import.format", "folder")
    source = _zip(tmp_path / "download.zip")

    mod = mm.import_mod(source_path=str(source), node_id=char.id)

    assert mod is not None
    body = Path(mod.path)
    assert body.is_file(), "库内本体只允许压缩档，不得再产出文件夹本体"
    assert body.suffix.lower() in (".zip", ".7z", ".rar")


def test_folder_import_to_zip_compresses(temp_data_dir, tmp_path):
    from src.core.config import Config

    mm, char = _manager_with_character()
    Config().set("import.format", "zip")
    Config().set("import.strict_format", False)
    source = tmp_path / "FolderSkin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")

    mod = mm.import_mod(source_path=str(source), node_id=char.id)

    assert mod is not None
    body = Path(mod.path)
    assert body.is_file() and body.suffix == ".zip"
