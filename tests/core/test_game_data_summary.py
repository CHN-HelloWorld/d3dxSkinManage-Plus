from threading import Event

import pytest

from tests.core.test_library_reconcile import mm as mm


def test_summary_reports_file_progress_and_exact_size(mm, monkeypatch):
    root = mm._index.paths.roots["game_a"].parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "one.bin").write_bytes(b"123")
    (root / "nested").mkdir()
    (root / "nested" / "two.bin").write_bytes(b"45678")

    def no_hydration(*args):
        raise AssertionError("summary must not load every Mod and its files")

    monkeypatch.setattr(mm._index, "get_all_mods", no_hydration)
    updates = []
    info = mm.describe_game_data("game_a", root.name, progress=lambda *args: updates.append(args))
    assert info["library_size"] == 8
    assert info["library_dir"] == str(root)
    assert updates[-1][1:4] == (2, 2, 8)
    assert (root / "one.bin").read_bytes() == b"123"


def test_summary_can_cancel_while_discovering_files(mm):
    from src.core.game_data_summary import GameDataScanCancelled

    root = mm._index.paths.roots["game_a"].parent
    root.mkdir(parents=True, exist_ok=True)
    for i in range(100):
        (root / f"{i}.bin").write_bytes(b"data")
    cancel = Event()

    def progress(phase, done, total, size):
        if phase == "扫描文件" and done:
            cancel.set()

    with pytest.raises(GameDataScanCancelled):
        mm.describe_game_data("game_a", root.name, cancelled=cancel.is_set, progress=progress)
    assert len(list(root.glob("*.bin"))) == 100


def test_summary_can_cancel_during_size_collection(mm):
    from src.core.game_data_summary import GameDataScanCancelled

    root = mm._index.paths.roots["game_a"].parent
    root.mkdir(parents=True, exist_ok=True)
    for i in range(100):
        (root / f"{i}.bin").write_bytes(b"data")
    cancel = Event()

    def progress(phase, done, total, size):
        if phase == "统计容量" and 0 < done < total:
            cancel.set()

    with pytest.raises(GameDataScanCancelled):
        mm.describe_game_data("game_a", root.name, cancelled=cancel.is_set, progress=progress)
    assert len(list(root.glob("*.bin"))) == 100
