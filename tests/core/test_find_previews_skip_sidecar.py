from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_folder_find_previews_skips_sidecar_images(mm, tmp_path):
    mod = tmp_path / "Mod"
    mod.mkdir()
    real = _png(mod / "real.png")
    _png(mod / ".d3dxmeta" / "previews" / "leaked.png")  # 残留信封内的图，不应被采纳
    found = mm._find_previews(str(mod))
    assert str(real) in found
    assert not any(".d3dxmeta" in p for p in found)


def test_zip_find_previews_skips_sidecar_images(mm, tmp_path):
    z = tmp_path / "mod.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Top/real.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Top/.d3dxmeta/previews/leaked.png", b"\x89PNG\r\n\x1a\n")
    found = mm._find_previews(str(z))
    assert any(Path(p).name == "real.png" for p in found)
    assert not any(".d3dxmeta" in p.replace("\\", "/") for p in found)
