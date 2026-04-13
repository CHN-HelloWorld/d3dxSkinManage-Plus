from pathlib import Path


def test_detailed_move_keeps_old_and_new_together():
    from watchdog.events import DirMovedEvent

    from src.core.watcher import _FileEventHandler

    legacy, changes = [], []
    handler = _FileEventHandler(lambda *args: legacy.append(args), detailed_callback=lambda *args: changes.append(args))
    handler.on_moved(DirMovedEvent("old", "new"))
    assert legacy == [("moved", "old"), ("moved", "new")]
    assert changes == [("moved", "old", "new", True)]


def test_marker_modification_invalidates_identity_without_override_noise():
    from watchdog.events import FileModifiedEvent

    from src.core.watcher import _FileEventHandler

    changes = []
    handler = _FileEventHandler(lambda *a: None, detailed_callback=lambda *a: changes.append(a))
    handler.on_modified(FileModifiedEvent(str(Path("container") / ".d3dxmod")))
    handler.on_modified(FileModifiedEvent(str(Path("container") / ".d3dxmeta/ini_overrides/mod.ini")))
    assert len(changes) == 1 and changes[0][0] == "modified"
