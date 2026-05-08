import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QMessageBox

from src.ui.dialogs.import_progress import ImportProgressDialog
from src.ui.dialogs.mod_import import ModImportDialog, _ImportEntry
from tests.core.test_library_reconcile import mm as mm


def source_with_previews(mm, tmp_path, kind):
    folder = tmp_path / "Skin"
    folder.mkdir()
    (folder / "mod.ini").write_bytes(b"[Constants]\nx=1\n")
    for name, color in [("a", "red"), ("b", "blue")]:
        image = folder / name / "preview.png"
        image.parent.mkdir()
        pixels = QImage(4, 4, QImage.Format.Format_RGB32)
        pixels.fill(QColor(color))
        assert pixels.save(str(image))
    if kind == "folder":
        return folder
    return mm._archive.compress(folder, tmp_path / "download", kind)


def source_bytes(source):
    if source.is_file():
        return source.read_bytes()
    return {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def open_import(qtbot, mm, source):
    node = mm.add_character("Import previews")
    entry = _ImportEntry(str(source))
    entry.ensure_scanned()
    assert len(entry.previews) == 2
    dialog = ModImportDialog(node_id=node.id)
    qtbot.addWidget(dialog)
    dialog._entries = [entry]
    dialog._refresh_table()
    dialog._table.setCurrentCell(0, 0)
    return dialog, node


@pytest.mark.parametrize("kind", ["folder", "zip", "7z"])
@pytest.mark.parametrize("delete_all", [False, True])
def test_import_excludes_selected_images_from_copy_without_changing_source(
    qtbot,
    mm,
    tmp_path,
    monkeypatch,
    kind,
    delete_all,
):
    source = source_with_previews(mm, tmp_path, kind)
    before = source_bytes(source)
    dialog, node = open_import(qtbot, mm, source)
    scanned_previews = list(dialog._entries[0].previews)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    if delete_all:
        dialog._delete_all_previews()
    else:
        dialog._delete_current_preview()
    assert source_bytes(source) == before
    dialog._on_entry_scanned(0, [], scanned_previews)
    assert len(dialog._entries[0].previews) == (0 if delete_all else 1)

    emitted = []
    dialog.import_confirmed.connect(lambda items, node_id: emitted.append(items))
    dialog._on_import()
    progress = ImportProgressDialog(emitted[0], node.id, mm)
    qtbot.addWidget(progress)
    (request,) = progress._plan_intake_requests()
    mod = progress._intake_service.commit(progress._intake_service.prepare(request))

    names = set(mm._archive.list_contents(mod.path))
    assert names == ({"Skin/mod.ini"} if delete_all else {"Skin/mod.ini", "Skin/b/preview.png"})
    assert len(mod.preview_images) == (0 if delete_all else 1)
    mm.rebuild_index_from_filesystem(mod.game_id)
    assert len(mm.get_mod(mod.mod_id).preview_images) == (0 if delete_all else 1)
    assert source_bytes(source) == before


def test_cancelling_import_after_removing_previews_preserves_originals(qtbot, mm, tmp_path, monkeypatch):
    source = source_with_previews(mm, tmp_path, "folder")
    before = source_bytes(source)
    dialog, _ = open_import(qtbot, mm, source)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog._delete_all_previews()
    dialog.reject()
    assert source_bytes(source) == before
    assert mm.get_mods() == []


def test_failed_import_with_excluded_previews_preserves_originals(mm, tmp_path, monkeypatch):
    from src.core.mod_intake import IntakeError, ModIntakeService

    source = source_with_previews(mm, tmp_path, "folder")
    before = source_bytes(source)
    service = ModIntakeService(mm)
    request = service.plan_request(
        source_path=source,
        game_id=mm.current_game_id,
        node_id=mm.add_character("Import failure").id,
        excluded_previews=("a/preview.png",),
    )
    monkeypatch.setattr(mm._archive, "compress", lambda *args, **kwargs: None)
    with pytest.raises(IntakeError, match="failed to materialize"):
        service.prepare(request)
    assert source_bytes(source) == before
    assert mm.get_mods() == []
