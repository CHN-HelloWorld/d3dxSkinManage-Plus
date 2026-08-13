"""编辑直接刷新一次；后续文件事件只发布局部变化，不再次重排整层卡片。

使用真实编辑入口、watcher 批处理和局部发布方法。替身只提供数据和视图边界。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models.mod import Mod
from src.models.tag import TagCommitResult
from src.ui.main_window import MainWindow


class _FakeDialog:
    """替代 ModEditDialog：exec 恒确认，返回预置结果。"""

    result: dict = {}

    def __init__(self, mod, parent=None):
        self._mod = mod

    def bind_mod(self, mod, *, tag_service):
        self._mod = mod

    def set_save_handler(self, handler):
        self._save_handler = handler

    def exec(self):
        self._result = dict(self.result)
        self._result["tag_commit_result"] = self._save_handler(self._result, object())
        return True

    def get_result(self):
        return self._result


class _FakeManager:
    def __init__(self, mod: Mod, *, rename_succeeds: bool = True):
        self._mod = mod
        self._rename_succeeds = rename_succeeds
        self.rename_calls = 0
        self.update_calls = 0
        self.source_batches = []
        self._index = SimpleNamespace(get_mods_by_ids=lambda ids: [self._mod] if self._mod.mod_id in ids else [])

    def get_mod(self, mod_id):
        return self._mod if mod_id == self._mod.mod_id else None

    def check_mod_operation(self, mod_id, **_checks):
        return self.get_mod(mod_id)

    def rename_mod(self, mod_id, new_name, sync_folder=True):
        self.rename_calls += 1
        if not self._rename_succeeds:
            return None
        self._mod.name = new_name
        self._mod.folder_name = new_name
        return self._mod

    def get_tag_service(self):
        return object()

    def commit_tag_edit(self, draft, fields):
        self.update_calls += 1
        self._mod.name = fields.get("name", self._mod.name)
        return TagCommitResult(mods=[self._mod])

    def apply_local_source_events(self, events):
        self.source_batches.append(list(events))
        return SimpleNamespace(changed_ids=(self._mod.mod_id,), removed_ids=())


class _Harness:
    """只提供 _on_mod_edit 真正用到的协作者，其余行为用真实方法。"""

    _on_mod_edit = MainWindow._on_mod_edit
    _suppress_watcher = MainWindow._suppress_watcher
    _refresh_from_watcher = MainWindow._refresh_from_watcher
    _publish_local_library_delta = MainWindow._publish_local_library_delta

    def __init__(self, manager: _FakeManager):
        self._mod_manager = manager
        self._overview_page = SimpleNamespace(
            update_mods_in_view=lambda mods: self._in_place.extend(mod.mod_id for mod in mods),
            refresh_tags=lambda **kwargs: False,
            apply_reconcile_delta=lambda mods, removed: self._local_updates.extend(mod.mod_id for mod in mods),
        )
        self._in_place: list[str] = []
        self._local_updates: list[str] = []
        self._game_service = SimpleNamespace(has_active_game=lambda: True, current_game_id="game")
        self._watcher_suppress_until = 0.0
        # 替身也须收集变化路径，以供局部对账确定范围。
        self._watcher_changed_paths: set = set()
        self._watcher_events = []
        self._import_active = False
        self._preset_apply_worker = None
        self.overview_refreshes = 0
        self.watcher_refreshes = 0

    def _refresh_overview_mods(self):
        self.overview_refreshes += 1

    def _refresh_current(self, **_kwargs):
        self.watcher_refreshes += 1


@pytest.fixture
def harness(monkeypatch, tmp_path):
    source = tmp_path / "old.rar"
    source.write_bytes(b"test source")
    mod = Mod(
        game_id="game",
        imported_at="2026-10-05T10:00:00",
        mod_id="m1",
        name="旧名",
        folder_name="旧名",
        path=str(source),
    )
    manager = _FakeManager(mod)
    monkeypatch.setattr("src.ui.main_window.ModEditDialog", _FakeDialog)
    return _Harness(manager), manager, mod


def test_rename_refreshes_cards_once_and_watcher_publishes_locally(harness):
    h, manager, _mod = harness
    _FakeDialog.result = {"name": "新名", "sync_folder": True}

    h._on_mod_edit("m1")

    assert manager.rename_calls == 1
    assert h.overview_refreshes == 0
    assert h._in_place == ["m1"]

    h._watcher_events.append("renamed-container")
    h._refresh_from_watcher()

    assert h._local_updates == ["m1"]
    assert manager.source_batches == [["renamed-container"]]
    assert h.overview_refreshes == 0
    assert h.watcher_refreshes == 0, "watcher 又重排了一次卡片（冗余刷新）"


def test_metadata_only_edit_does_not_rebuild_or_suppress(harness):
    h, manager, _mod = harness
    _FakeDialog.result = {"name": "旧名", "author": "作者", "tags": ["a"]}

    h._on_mod_edit("m1")

    assert manager.rename_calls == 0
    assert h.overview_refreshes == 0
    assert h._in_place == ["m1"]
    assert h._watcher_suppress_until == 0.0

    h._watcher_events.append("metadata-changed")
    h._refresh_from_watcher()

    assert h._local_updates == ["m1"]
    assert h.watcher_refreshes == 0


def test_failed_rename_still_suppresses_nothing(harness, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    mod = Mod(
        game_id="game",
        imported_at="2026-10-05T10:00:00",
        mod_id="m1",
        name="旧名",
        folder_name="旧名",
        path=harness[2].path,
    )
    manager = _FakeManager(mod, rename_succeeds=False)
    h = _Harness(manager)
    _FakeDialog.result = {"name": "新名", "sync_folder": True}
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    h._on_mod_edit("m1")

    assert manager.rename_calls == 1
    assert warnings
    assert h.overview_refreshes == 0
    assert h._watcher_suppress_until == 0.0

    h._refresh_from_watcher()

    assert not manager.source_batches
    assert h.watcher_refreshes == 0
