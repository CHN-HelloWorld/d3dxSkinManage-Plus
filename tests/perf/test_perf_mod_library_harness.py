from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def test_gui_teardown_waits_for_deferred_window_and_worker_close(qapp):
    from types import SimpleNamespace

    import shiboken6
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    controller = SimpleNamespace(worker=object(), applying=False)

    class Window(QWidget):
        _library_probe = controller

        def closeEvent(self, event):
            if controller.worker is not None:
                event.ignore()
                QTimer.singleShot(20, self.finish)
            else:
                super().closeEvent(event)

        def finish(self):
            controller.worker = None
            self.close()

    window = Window()
    window.show()
    harness().close_gui_window(window, qapp)
    assert not shiboken6.isValid(window)
    assert controller.worker is None


def harness():
    path = Path(__file__).parents[2] / "scripts" / "performance" / "perf_mod_library.py"
    assert path.is_file(), "The reusable benchmark entry point must exist"
    spec = importlib.util.spec_from_file_location("perf_mod_library", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_isolation_restores_paths_and_singletons_after_error(tmp_path):
    import tempfile

    bench = harness()
    from src.core.config import Config
    from src.utils import paths

    previous_path = paths.get_app_dir
    previous_config = Config._instance
    previous_temp = tempfile.gettempdir()
    root = tmp_path / "isolated"
    with pytest.raises(RuntimeError, match="deliberate"):
        with bench.isolated_manager(root) as manager:
            assert paths.get_app_dir() == root
            assert manager._index._db_path.is_relative_to(root)
            assert Config._instance is not previous_config
            assert Path(tempfile.gettempdir()).is_relative_to(root)
            Config().set("general.language", "en-US")
            raise RuntimeError("deliberate")
    assert paths.get_app_dir is previous_path
    assert Config._instance is previous_config
    assert tempfile.gettempdir() == previous_temp
    assert (root / "config" / "settings.json").is_file()


def test_cleanup_refuses_anchor_and_outside_paths(tmp_path):
    bench = harness()
    anchor = tmp_path / "run"
    anchor.mkdir()
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "valuable").write_text("keep")
    for target in (anchor, outside, anchor / ".." / "keep"):
        with pytest.raises(ValueError, match="outside|anchor"):
            bench.owned_remove(target, anchor)
    assert (outside / "valuable").read_text() == "keep"
    generated = anchor / "case"
    generated.mkdir()
    bench.owned_remove(generated, anchor)
    assert not generated.exists()


def test_isolated_app_root_lookup_does_not_add_filesystem_work(tmp_path, monkeypatch):
    bench = harness()
    from src.utils import paths

    with bench.isolated_manager(tmp_path / "isolated"):
        mkdir_calls = []
        original = Path.mkdir

        def tracked(path, *args, **kwargs):
            mkdir_calls.append(path)
            return original(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", tracked)
            for _ in range(5):
                assert paths.get_app_dir() == tmp_path / "isolated"
        assert mkdir_calls == []


def test_failed_case_keeps_error_and_incomplete_result(tmp_path, monkeypatch):
    bench = harness()

    def fail(*args, **kwargs):
        raise RuntimeError("injected benchmark failure")

    monkeypatch.setattr(bench, "run_case", fail)
    output = tmp_path / "output"
    assert bench.main(["--counts", "2", "--mode", "core", "--output", str(output)]) == 1
    result = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert result["completed"] is False
    assert "injected benchmark failure" in result["error"]["message"]
    assert result["isolation"]["cleaned"] is True


def test_gui_defaults_preserve_full_acceptance_conditions():
    options = harness().parse_args(["--output", "unused"])
    assert options.counts == [300, 1000, 5000]
    assert options.warm_rounds == 2
    assert options.gui_seconds == 60
    assert options.gui_rounds == 3


def test_preview_sample_has_valid_png_chunks():
    import zlib

    preview = harness().PREVIEW
    assert len(preview) == 120 * 1024
    offset = 8
    while True:
        length = int.from_bytes(preview[offset : offset + 4], "big")
        chunk = preview[offset + 4 : offset + 8 + length]
        crc = int.from_bytes(preview[offset + 8 + length : offset + 12 + length], "big")
        assert zlib.crc32(chunk) == crc, f"invalid PNG {chunk[:4]!r} CRC"
        offset += length + 12
        if chunk[:4] == b"IEND":
            break
