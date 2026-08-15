from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from src.core import tag_file_batch
from src.core.mod_package import read_sidecar
from src.core.mod_update_service import ModUpdateService
from src.core.operation_preflight import OperationPreflightError
from src.ui.operation_check import run_file_check
from tests.core.test_library_reconcile import mm as mm
from tests.core.test_tag_edit_batch import seed_shared_mods, sidecar_path


@pytest.mark.parametrize("fail_write", [False, True])
def test_queued_update_completion_is_independent_of_tag_save(qtbot, mm, monkeypatch, fail_write):
    mods = seed_shared_mods(mm, 3)
    unrelated = mm.get_mod(mods[-1].mod_id)
    unrelated.info.tags = []
    assert mm.update_mod(unrelated)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    mm.accept_operation_trust(mm.current_game_id)
    original_bytes = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods[:2]}
    owner = QWidget()
    qtbot.addWidget(owner)
    updated = threading.Event()
    callback_errors = []
    writes = []
    original_write = tag_file_batch.write_sidecar

    def write(path, manifest):
        writes.append(path)
        assert updated.wait(5), "The owner event loop must run during file I/O"
        if fail_write and len(writes) == 2:
            raise PermissionError("injected tag sidecar failure")
        return original_write(path, manifest)

    def update_other_mod():
        try:
            ModUpdateService._write_last_checked(SimpleNamespace(_mm=mm), unrelated.mod_id)
        except Exception as error:
            callback_errors.append(error)
        finally:
            updated.set()

    def runner(work):
        if work.func is tag_file_batch.write_tag_files:
            QTimer.singleShot(0, update_other_mod)
        return run_file_check(work, owner)

    monkeypatch.setattr(tag_file_batch, "write_sidecar", write)
    mm.operation_check_runner = runner
    if fail_write:
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {})
    else:
        mm.commit_tag_edit(draft, {})

    assert callback_errors == []
    assert updated.is_set()
    assert len(writes) == 2
    assert not mm._index.in_transaction
    saved_other = mm.get_mod(unrelated.mod_id)
    checked = saved_other.info.custom_data["gamebanana"]["last_checked_iso"]
    assert checked
    assert read_sidecar(saved_other.path)["info"]["custom_data"]["gamebanana"]["last_checked_iso"] == checked
    expected = "Shared" if fail_write else "Renamed"
    for mod in mods[:2]:
        assert mm.get_mod(mod.mod_id).info.tags == [expected]
        assert read_sidecar(mod.path)["info"]["tags"] == [expected]
        if fail_write:
            assert sidecar_path(mod).read_bytes() == original_bytes[mod.mod_id]
