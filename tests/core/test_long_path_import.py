from __future__ import annotations

import os
import threading
from pathlib import Path

from src.core.mod_manager import ModManager
from src.core.mod_package import read_container_marker
from src.core.mod_storage import ModStorage
from src.utils import fs
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import registered_games as registered_games


def _deep(parent: Path, count: int = 8) -> Path:
    out = parent
    for i in range(count):
        out /= f"segment_{i}_" + "x" * 24
    return out


def test_copy_mod_into_container_supports_paths_over_260(tmp_path):
    source = tmp_path / "SourceMod"
    fs.ensure_dir(source / "nested")
    fs.write_text(source / "nested" / "mod.ini", "[Constants]\n")
    parent = _deep(tmp_path / "library")
    assert len(str(parent)) > 260
    content_hash = "a" * 40

    body = ModStorage().copy_mod_into_container(
        source,
        parent,
        content_hash=content_hash,
    )

    assert body is not None
    assert fs.is_file(body / "nested" / "mod.ini")
    marker = read_container_marker(body.parent)
    assert marker is not None and marker["body"] == body.name
    assert marker["content_hash"] == content_hash


def test_copy_mod_into_container_rolls_back_when_marker_write_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "SourceMod"
    fs.write_text(source / "mod.ini", "[Constants]\n")
    parent = tmp_path / "library"
    monkeypatch.setattr(
        "src.core.mod_package.write_container_marker",
        lambda *_args, **_kwargs: False,
    )

    body = ModStorage().copy_mod_into_container(
        source,
        parent,
        content_hash="b" * 40,
    )

    assert body is None
    assert fs.is_file(source / "mod.ini")
    assert not fs.exists(parent / "SourceMod")


def test_long_character_chain_writes_marker_without_ghost(temp_data_dir):
    mm = ModManager()
    mm.set_game("game_a")
    parent_id = ""
    leaf = None
    for i in range(8):
        leaf = mm.add_character(
            name=f"category_{i}_" + "长" * 22,
            parent_id=parent_id,
            node_type="category",
        )
        assert leaf is not None
        parent_id = leaf.id

    leaf_path = mm._get_character_library(leaf.id, create=False)
    assert len(str(leaf_path)) > 260
    assert fs.is_file(leaf_path / CATEGORY_MARKER_FILE)


def test_failed_marker_creation_rolls_back_character_row(temp_data_dir, monkeypatch):
    mm = ModManager()
    mm.set_game("game_a")
    monkeypatch.setattr(mm, "_ensure_category_marker", lambda *_args, **_kwargs: False)

    created = mm.add_character("should_not_exist")

    assert created is None
    assert all(c.name != "should_not_exist" for c in mm.get_characters("game_a"))


def test_copy_checkpoint_freezes_active_file_io(tmp_path):
    source = tmp_path / "large.bin"
    with source.open("wb") as fh:
        fh.truncate(32 * 1024 * 1024)
    target = tmp_path / "copied.bin"
    gate = threading.Event()
    gate.set()
    paused = threading.Event()
    errors: list[BaseException] = []

    def checkpoint() -> bool:
        gate.wait()
        return True

    def progress(frac: float) -> None:
        if frac > 0 and not paused.is_set():
            gate.clear()
            paused.set()

    def run_copy() -> None:
        try:
            fs.copy_path(source, target, checkpoint=checkpoint, progress_cb=progress)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run_copy)
    thread.start()
    assert paused.wait(2.0)
    frozen_size = os.path.getsize(fs.native_path(target))
    thread.join(0.15)
    assert thread.is_alive()
    assert os.path.getsize(fs.native_path(target)) == frozen_size

    gate.set()
    thread.join(5.0)
    assert not thread.is_alive()
    assert not errors
    assert os.path.getsize(fs.native_path(target)) == 32 * 1024 * 1024
