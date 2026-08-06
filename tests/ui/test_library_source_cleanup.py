import time
import uuid
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread, QTimer

from src.core.preset_manager import PresetManager
from src.models.mod import Mod
from src.models.preset import Preset
from src.ui import library_source_cleanup as module


class Service:
    generation = 1
    clock = staticmethod(time.monotonic)

    def __init__(self, identity):
        self.token = ("root", 1)
        self.restored = False
        self._missing = {identity: object()}
        self._missing_evidence = {identity: object()}
        self._cleanup_retry_after = {}

    def recovery_token(self):
        return self.token

    def confirm_missing(self, identity, *, generation, scope_complete):
        assert generation == self.generation and scope_complete
        return identity in self._missing and not self.restored


@pytest.fixture
def setup_cleanup(temp_data_dir, tmp_path):
    identity = uuid.uuid4().hex
    mod = Mod(
        mod_id=identity,
        game_id="game",
        node_id="node",
        folder_name="Container",
        path=str(tmp_path / "library" / "Container" / "body.zip"),
    )
    pm = PresetManager()
    preset = Preset(id="preset", name="Preset", game_id="game", character_mods={"node": [identity]})
    pm._presets[preset.id] = preset
    pm._save()
    service = Service(identity)
    rows = {identity: mod}
    protected, cleaned, finalized, sync = {}, [], [], []
    owner = QThread.currentThread()
    manager = SimpleNamespace(
        current_game_id="game",
        _preset_manager=pm,
        _local_source_events=service,
        get_mod=rows.get,
        _index=SimpleNamespace(
            paths=SimpleNamespace(container_path=lambda mid: Path(mod.path).parent), get_deployment=lambda mid: None
        ),
        protect_library_operation=lambda token, **values: protected.update({token: values}),
        release_library_operation=lambda token: protected.pop(token, None),
    )

    def cleanup(current, game_id, *, locations=None):
        assert QThread.currentThread() == owner
        assert protected
        cleaned.append(current.mod_id)
        return True

    def finalize(current, game_id, *, preset_candidate):
        assert QThread.currentThread() == owner
        pm.commit_cascade_delete(preset_candidate)
        finalized.append(current.mod_id)
        rows.pop(current.mod_id)
        return True

    manager._cleanup_missing_source_artifacts = cleanup
    manager._finalize_missing_source_mod = finalize
    manager._purge_missing_source_mod = lambda *args, **kwargs: sync.append(args) or True
    published, resumed = [], []
    controller = module.LibrarySourceCleanupController(manager, published.append, lambda: resumed.append(True))
    state = SimpleNamespace(
        manager=manager,
        controller=controller,
        mod=mod,
        preset=preset,
        pm=pm,
        service=service,
        protected=protected,
        cleaned=cleaned,
        finalized=finalized,
        published=published,
        resumed=resumed,
        sync=sync,
    )
    yield state
    controller.stop()


def block_writer(monkeypatch):
    entered, release = Event(), Event()
    original = module.write_prepared_preset_delete

    def write(candidate):
        entered.set()
        assert release.wait(5), "test did not release preset writer"
        return original(candidate)

    monkeypatch.setattr(module, "write_prepared_preset_delete", write)
    return entered, release


def test_writer_wait_keeps_ui_live_and_artifact_cleanup_overlaps(qtbot, setup_cleanup, monkeypatch):
    state = setup_cleanup
    entered, release = block_writer(monkeypatch)
    original_cleanup = state.manager._cleanup_missing_source_artifacts

    def cleanup(*args, **kwargs):
        assert entered.wait(1)
        assert not release.is_set()
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(state.manager, "_cleanup_missing_source_artifacts", cleanup)
    before = state.pm._presets_file.read_bytes()
    beats = []
    try:
        assert state.controller.purge(state.mod, "game") is None
        assert state.controller.running and state.cleaned == [state.mod.mod_id]
        candidate = state.controller.active.candidate
        QTimer.singleShot(0, lambda: beats.append(True))
        qtbot.waitUntil(lambda: bool(beats))
        assert not state.published and not state.finalized
        assert state.pm._presets_file.read_bytes() == before
        assert state.protected
        release.set()
        qtbot.waitUntil(lambda: not state.controller.running)
        assert state.finalized == [state.mod.mod_id]
        assert state.published[0].removed_ids == {state.mod.mod_id}
        assert state.resumed == [True]
        assert not state.protected and not candidate.temporary.exists()
        assert state.preset.character_mods == {}
        assert not state.service._missing and not state.service._missing_evidence
    finally:
        release.set()


@pytest.mark.parametrize("invalidate", ["restored", "game", "row", "proof"])
def test_late_writer_cannot_finalize_invalidated_source(qtbot, setup_cleanup, monkeypatch, invalidate):
    state = setup_cleanup
    entered, release = block_writer(monkeypatch)
    before = state.pm._presets_file.read_bytes()
    try:
        state.controller.purge(state.mod, "game")
        candidate = state.controller.active.candidate
        assert entered.wait(1)
        if invalidate == "restored":
            state.service.restored = True
        elif invalidate == "game":
            state.manager.current_game_id = "other"
        elif invalidate == "row":
            state.mod.name = "Edited while saving"
        else:
            state.service.token = ("root", 2)
        release.set()
        qtbot.waitUntil(lambda: not state.controller.running)
        assert not state.finalized and not state.protected
        assert state.pm._presets_file.read_bytes() == before
        assert not candidate.temporary.exists()
        assert state.preset.enabled_mod_ids == [state.mod.mod_id]
        if invalidate == "game":
            assert not state.published
        else:
            assert state.published[0].pending_missing_ids == {state.mod.mod_id}
            assert state.published[0].retry_after_ms == 500
    finally:
        release.set()


@pytest.mark.parametrize("failure", ["writer", "artifacts", "finalize"])
def test_failure_retains_intent_and_releases_protection(qtbot, setup_cleanup, monkeypatch, failure):
    state = setup_cleanup
    before = state.pm._presets_file.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("blocked disk")

    if failure == "writer":
        monkeypatch.setattr(module, "write_prepared_preset_delete", fail)
    elif failure == "artifacts":
        monkeypatch.setattr(state.manager, "_cleanup_missing_source_artifacts", lambda *args, **kwargs: False)
    else:
        monkeypatch.setattr(state.manager, "_finalize_missing_source_mod", fail)
    state.controller.purge(state.mod, "game")
    candidate = state.controller.active.candidate
    qtbot.waitUntil(lambda: not state.controller.running)
    assert not state.finalized and not state.protected
    assert not candidate.temporary.exists()
    assert state.pm._presets_file.read_bytes() == before
    assert state.published[0].pending_missing_ids == {state.mod.mod_id}
    assert state.published[0].retry_after_ms == 500
    assert state.resumed == [True]


def test_stop_cancels_inflight_candidate_and_cleans_after_worker_returns(qtbot, setup_cleanup, monkeypatch):
    state = setup_cleanup
    entered, release = block_writer(monkeypatch)
    before = state.pm._presets_file.read_bytes()
    try:
        state.controller.purge(state.mod, "game")
        job = state.controller.active
        assert entered.wait(1)
        state.controller.stop()
        assert not state.controller.running and not state.protected
        release.set()
        qtbot.waitUntil(job.future.done)
        qtbot.waitUntil(lambda: not job.candidate.temporary.exists())
        assert not state.finalized and not state.published
        assert state.pm._presets_file.read_bytes() == before
    finally:
        release.set()


def test_no_preset_candidate_uses_existing_synchronous_purge(setup_cleanup, monkeypatch):
    state = setup_cleanup
    monkeypatch.setattr(state.pm, "prepare_cascade_delete", lambda *args: None)
    assert state.controller.purge(state.mod, "game") is True
    assert state.sync and not state.controller.running and not state.protected


def test_prepare_failure_returns_false_without_starting_cleanup(setup_cleanup, monkeypatch):
    state = setup_cleanup
    before = state.pm._presets_file.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("cannot prepare preset deletion")

    monkeypatch.setattr(state.pm, "prepare_cascade_delete", fail)
    assert state.controller.purge(state.mod, "game") is False
    assert not state.controller.running and not state.protected
    assert not state.cleaned and not state.finalized and not state.sync
    assert not state.published and not state.resumed
    assert state.pm._presets_file.read_bytes() == before
