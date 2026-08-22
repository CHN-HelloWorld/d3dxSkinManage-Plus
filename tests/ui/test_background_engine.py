import threading

from src.core.config import Config


def test_legacy_background_and_dynamic_paths_survive_reload(temp_data_dir):
    cfg = Config()
    cfg.set("appearance.background", {"enabled": True, "path": str(temp_data_dir / "old.png"), "blur": 42})
    from src.ui.backgrounds.config import background_options

    assert background_options(cfg)["source"] == "image"
    assert background_options(cfg)["blur"] == 42
    cfg.set("appearance.background.video_path", str(temp_data_dir / "movie.mp4"))
    cfg.set("appearance.background.source", "video")
    cfg.reload()
    assert background_options(cfg)["source"] == "video"
    assert background_options(cfg)["path"] == str(temp_data_dir / "old.png")
    assert background_options(cfg)["video_path"] == str(temp_data_dir / "movie.mp4")
    cfg.set("appearance.background.enabled", False)
    assert background_options(cfg)["source"] == "off"


def test_named_executable_is_not_sufficient(tmp_path):
    from src.ui.backgrounds.engine import validate_engine_path

    exe = tmp_path / "wallpaper64.exe"
    exe.write_bytes(b"MZ" + b"fake executable" * 20)
    assert not validate_engine_path(str(exe))[0]
    assert not validate_engine_path("")[0]


def test_old_validation_cannot_reopen_cleared_path(qtbot, monkeypatch):
    from src.ui.backgrounds import engine

    started, release = threading.Event(), threading.Event()

    def check(path):
        started.set()
        release.wait(3)
        return True, "ok"

    monkeypatch.setattr(engine, "validate_engine_path", check)
    validator = engine.EnginePathValidator()
    validator.set_path("old.exe")
    validator.check_now()
    qtbot.waitUntil(started.is_set)
    validator.set_path("")
    release.set()
    qtbot.wait(100)
    assert not validator.valid
    assert validator.path == ""
    assert validator.status == "empty"
    validator.deleteLater()
