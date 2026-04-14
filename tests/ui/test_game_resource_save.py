"""验证游戏资源更名的保存反馈、原生监控恢复和相关路径保留。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QMessageBox

from tests.ui.test_deployment_root_main_window import _assert_active_root, _refresh_idle
from tests.ui.test_deployment_root_main_window import real_window as real_window
from tests.ui.test_game_unsaved_migration import _add_game
from tests.ui.test_game_unsaved_migration import game_page as game_page


def test_failed_resource_move_reports_saved_settings_and_keeps_original_files(game_page, temp_data_dir, monkeypatch):
    game_id = _add_game(game_page, "Original Resource")
    old_root = temp_data_dir / "data" / "Original Resource"
    old_root.mkdir(parents=True, exist_ok=True)
    (old_root / "keep.txt").write_text("original data", encoding="utf-8")
    blocked_root = temp_data_dir / "data" / "Renamed Resource"
    blocked_root.mkdir()
    (blocked_root / "keep.txt").write_text("unrelated data", encoding="utf-8")
    game_page._select(game_id)
    game_page._detail._name_en.setText("Renamed Resource")
    notices = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, title, message, *_a, **_k: notices.append((title, message))
    )

    assert game_page._on_save(game_page._detail.collect())

    saved = game_page._svc.get_game(game_id)
    assert saved.name_en == "Renamed Resource"
    assert saved.resource_dir == "Original Resource"
    assert not game_page.has_unsaved_changes()
    assert (old_root / "keep.txt").read_text(encoding="utf-8") == "original data"
    assert (blocked_root / "keep.txt").read_text(encoding="utf-8") == "unrelated data"
    assert len(notices) == 1
    assert "Original Resource" in notices[0][1]
    assert "Renamed Resource" in notices[0][1]


def test_resource_move_preserves_the_internal_game_preview(game_page, temp_data_dir):
    game_id = _add_game(game_page, "Original Resource")
    old_root = temp_data_dir / "data" / "Original Resource"
    old_root.mkdir(parents=True, exist_ok=True)
    preview = old_root / "_game_preview.png"
    image = QPixmap(16, 16)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(preview))
    original_bytes = preview.read_bytes()
    game = game_page._svc.get_game(game_id)
    game.preview = str(preview)
    game_page._svc.save_game(game)
    game_page.reload()
    game_page._select(game_id)
    game_page._detail._name_en.setText("Renamed Resource")

    assert game_page._on_save(game_page._detail.collect())

    saved = game_page._svc.get_game(game_id)
    expected = temp_data_dir / "data" / "Renamed Resource" / "_game_preview.png"
    assert Path(saved.preview) == expected
    assert expected.read_bytes() == original_bytes
    assert not old_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 目录监控句柄与祖先目录更名")
@pytest.mark.parametrize("move_deployment", [False, True])
def test_active_resource_move_resumes_watchers_and_preserves_deployments(
    real_window, qtbot, temp_data_dir, monkeypatch, move_deployment
):
    window, manager, service, mods, root, _moved = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    old_resource = manager._index.paths.roots[game_id].parent
    expected_resource = temp_data_dir / "data" / "Renamed Resource"
    expected_deploy = temp_data_dir / "new-deployment" if move_deployment else root
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")
    if move_deployment:
        window._game_page._detail._mods.setText(str(expected_deploy))
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)

    assert window._game_page._on_save(window._game_page._detail.collect())
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)

    assert service.get_game(game_id).resource_dir == "Renamed Resource"
    assert service.get_game(game_id).mods_path == str(expected_deploy)
    assert expected_resource.is_dir() and not old_resource.exists()
    assert window._file_watcher.is_running()
    assert window._file_watcher._watch_path == expected_resource / "mods"
    assert window._deploy_watcher.is_running()
    assert window._deploy_watcher._watch_path == expected_deploy
    assert manager._storage.get_deploy_dir() == expected_deploy
    for original in mods:
        mod = manager.get_mod(original.mod_id)
        assert mod.is_enabled
        assert Path(mod.path).is_relative_to(expected_resource)
        assert Path(mod.path).exists()
        assert Path(mod.deployed_path) == expected_deploy / Path(original.deployed_path).name
        assert (Path(mod.deployed_path) / "mod.ini").read_bytes() == (Path(mod.path) / "mod.ini").read_bytes()
        pairs = manager._ini_sync._pairs[mod.mod_id]
        assert pairs and all(pair.override_path.is_relative_to(expected_resource) for pair in pairs)


def test_cancelled_deployment_move_restores_active_resource_watchers(real_window, qtbot, temp_data_dir, monkeypatch):
    window, manager, service, mods, root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    original_resource = service.get_resource_dir(game_id)
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")
    window._game_page._detail._mods.setText(str(temp_data_dir / "another-deployment"))
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Cancel)

    assert not window._game_page._on_save(window._game_page._detail.collect())
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)

    assert service.get_resource_dir(game_id) == original_resource
    assert not (temp_data_dir / "data" / "Renamed Resource").exists()
    assert window._game_page.has_unsaved_changes()
    _assert_active_root(window, manager, service, mods, root)
    window._game_page._detail.set_game(service.get_game(game_id), is_active=True)


def test_active_resource_conflict_restores_original_environment(real_window, qtbot, temp_data_dir, monkeypatch):
    window, manager, service, mods, root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    original_resource = service.get_resource_dir(game_id)
    conflict = temp_data_dir / "data" / "Renamed Resource"
    conflict.mkdir()
    sentinel = conflict / "keep.txt"
    sentinel.write_text("unrelated data", encoding="utf-8")
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *_a, **_k: notices.append(text))

    assert window._game_page._on_save(window._game_page._detail.collect())
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)

    assert service.get_resource_dir(game_id) == original_resource
    assert sentinel.read_text(encoding="utf-8") == "unrelated data"
    assert len(notices) == 1
    _assert_active_root(window, manager, service, mods, root)


def test_resource_save_waits_for_active_file_operations(real_window, qtbot, monkeypatch):
    window, manager, service, mods, root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    original_resource = service.get_resource_dir(game_id)
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *_a, **_k: notices.append(text))
    with monkeypatch.context() as busy:
        busy.setattr(window, "_plugin_operations", SimpleNamespace(busy=True), raising=False)
        assert not window._game_page._on_save(window._game_page._detail.collect())

    assert service.get_resource_dir(game_id) == original_resource
    assert len(notices) == 1
    assert window._game_page.has_unsaved_changes()
    _assert_active_root(window, manager, service, mods, root)
    window._game_page._detail.set_game(service.get_game(game_id), is_active=True)


def test_resource_move_preserves_pending_deployed_copy_edits(real_window, qtbot, monkeypatch):
    """保存前部署侧替换 INI，迁移须先回写未完成防抖的内容再重建同步器。"""
    window, manager, service, mods, _root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    original = mods[0]
    pair = manager._ini_sync._pairs[original.mod_id][0]
    destination = pair.deployed_path
    replacement = destination.with_suffix(".tmp")
    changed = b"[KeyExample]\nkey = K\n$variant = 2\n"
    replacement.write_bytes(changed)
    monkeypatch.setattr(manager._ini_sync, "_debounce_seconds", 30)
    os.replace(replacement, destination)
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")

    assert window._game_page._on_save(window._game_page._detail.collect())
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)

    new_pair = manager._ini_sync._pairs[original.mod_id][0]
    assert service.get_resource_dir(game_id) == "Renamed Resource"
    assert new_pair.override_path.read_bytes() == changed
    assert new_pair.deployed_path.read_bytes() == changed
    assert manager.get_mod(original.mod_id).is_enabled


def test_incomplete_resource_rollback_keeps_actual_location_and_deactivates_game(real_window, qtbot, monkeypatch):
    """核心报告无法完整回滚时，保留实际资源指针并停止旧环境，禁止自动创建空的旧根。"""
    from src.core.mod_manager import ResourceDirectoryMigrationError

    window, manager, service, _mods, _root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    old_root = manager._index.paths.roots[game_id].parent
    actual_root = old_root.with_name("Renamed Resource")

    def fail_after_move(gid, _old, new):
        """模拟目录已经移动而链接恢复失败，由核心提供实际位置。"""
        old_root.rename(actual_root)
        manager._index.paths.roots[gid] = actual_root / "mods"
        manager._index.paths.preview_roots[gid] = actual_root / "preview_cache"
        raise ResourceDirectoryMigrationError(new, actual_root, ["回滚文件被占用"])

    monkeypatch.setattr(manager, "migrate_game_resource_dir", fail_after_move)
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *_a, **_k: notices.append(text))
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")

    assert not window._game_page._on_save(window._game_page._detail.collect())
    qtbot.wait(50)

    assert service.get_resource_dir(game_id) == "Renamed Resource"
    assert service.current_game_id == manager.current_game_id == ""
    assert window._game_store.get_last_game_id() == ""
    assert actual_root.is_dir() and not old_root.exists()
    assert not window._file_watcher.is_running()
    assert not window._deploy_watcher.is_running()
    assert manager._ini_sync is None
    assert len(notices) == 1 and str(actual_root) in notices[0]


@pytest.mark.parametrize("move_deployment", [False, True])
def test_failed_settings_write_restores_resources_and_moved_deployments(
    real_window, qtbot, temp_data_dir, monkeypatch, move_deployment
):
    """配置文件不可写时回退已完成的目录迁移，原部署与目标目录内的无关文件均保留。"""
    from src.core import game_config_store

    window, manager, service, mods, root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    game_id = manager.current_game_id
    old_resource = service.get_resource_dir(game_id)
    old_name = service.get_game(game_id).name_en
    new_deployment = temp_data_dir / "another-deployment"
    new_deployment.mkdir()
    unrelated = new_deployment / "unrelated.txt"
    unrelated.write_bytes(b"unrelated data")
    before = window._game_store._path.read_bytes()
    window._game_page._select(game_id)
    window._game_page._detail._name_en.setText("Renamed Resource")
    if move_deployment:
        window._game_page._detail._mods.setText(str(new_deployment))
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *_a, **_k: notices.append(text))

    def refuse_write(_path, _data):
        """注入配置文件被占用导致的原子写入失败。"""
        raise PermissionError("settings file is in use")

    with monkeypatch.context() as blocked:
        blocked.setattr(game_config_store, "write_json_atomic", refuse_write)
        saved = window._game_page._on_save(window._game_page._detail.collect())
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)

    assert not saved
    assert service.get_game(game_id).name_en == old_name
    assert service.get_resource_dir(game_id) == old_resource
    assert window._game_store._path.read_bytes() == before
    assert not (temp_data_dir / "data" / "Renamed Resource").exists()
    assert list(new_deployment.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"unrelated data"
    assert len(notices) == 1
    _assert_active_root(window, manager, service, mods, root)
    window._game_page._detail.set_game(service.get_game(game_id), is_active=True)


def test_failed_save_preserves_deployment_records_already_in_destination(temp_data_dir):
    """目标目录已有停放副本时，回滚仅还原本次搬迁的记录，不重映射整个目标根。"""
    from src.core.game_service import GameService
    from src.models.deployment import DeploymentRecord, DeploymentState
    from src.ui.pages.game import GamePage
    from tests.ui.test_deployment_root_watch import _environment

    manager, service, mods, root = _environment(temp_data_dir / "deployment-home")
    try:
        game_id = manager.current_game_id
        new_root = temp_data_dir / "another-deployment"
        new_root.mkdir()
        active, parked = mods
        kept_parked = new_root / f"DISABLED_{parked.mod_id[:12]}_Preserved"
        shutil.move(parked.deployed_path, kept_parked)
        original_record = DeploymentRecord(parked.mod_id, DeploymentState.DISABLED, str(kept_parked))
        manager._index.upsert_deployment(original_record)
        original_content = (kept_parked / "mod.ini").read_bytes()
        source = Path(active.deployed_path)
        target = new_root / source.name
        shutil.move(source, target)
        manager.rebase_deployments(game_id, root, new_root)

        GamePage._rollback_deploy_contents(SimpleNamespace(_svc=service), game_id, [(source, target)])

        assert manager._index.get_deployment(parked.mod_id) == original_record
        assert (kept_parked / "mod.ini").read_bytes() == original_content
        assert source.is_dir() and not target.exists()
        assert manager.get_mod(active.mod_id).deployed_path == str(source)
    finally:
        manager.stop_ini_sync()
        manager.variants.stop_watch()
        manager._index.close()
        GameService._instance = None


@pytest.mark.parametrize("entry", ["detail", "card_drop"])
def test_preview_save_failure_reports_error_without_claiming_saved_config(game_page, temp_data_dir, monkeypatch, entry):
    from src.core import game_config_store

    game_id = _add_game(game_page, "Preview Example")
    game_page._select(game_id)
    preview = temp_data_dir / "selected.png"
    image = QPixmap(12, 12)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(preview))
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *_a, **_k: notices.append(text))

    def refuse_write(_path, _data):
        """模拟预览配置写盘失败，不影响源图片复制。"""
        raise PermissionError("settings file is in use")

    with monkeypatch.context() as blocked:
        blocked.setattr(game_config_store, "write_json_atomic", refuse_write)
        if entry == "detail":
            game_page._on_preview_changed(str(preview))
        else:
            game_page._on_card_image_dropped(game_id, str(preview))

    assert len(notices) == 1
    assert game_page._svc.get_game(game_id).preview == ""
    assert game_page.has_unsaved_changes()
    assert preview.is_file()
