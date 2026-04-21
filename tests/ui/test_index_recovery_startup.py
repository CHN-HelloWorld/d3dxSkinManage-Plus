import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.mod_manager import ModManager
from src.ui.main_window import MainWindow
from tests.helpers import register_games


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("damage", ["not_database", "malformed"])
def test_corrupt_index_is_handled_before_restoring_active_game(temp_data_dir, recover, damage):
    register_games("game_a")
    db = temp_data_dir / "data" / "mod_index.db"
    damaged = b"deliberately damaged SQLite database"
    if damage == "malformed":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE broken (id INTEGER)")
        conn.close()
        data = bytearray(db.read_bytes())
        data[100] = 255  # SQLite header 保留，破坏 sqlite_master 的 B-tree 页类型。
        damaged = bytes(data)
    db.write_bytes(damaged)
    manager = ModManager()
    events = []

    def prompt():
        events.append("prompt")
        assert manager.index_needs_recovery() == "corrupt"
        if recover:
            manager.recover_corrupt_index()

    def apply_environment():
        events.append("activate")
        manager.set_deploy_dir(str(temp_data_dir / "deployed"))

    window = SimpleNamespace(
        _mod_manager=manager,
        _game_service=manager._game_service,
        _game_store=Mock(get_last_game_id=Mock(return_value="game_a")),
        _overview_page=Mock(),
        _game_page=Mock(),
        _maybe_recover_index=prompt,
        _apply_migoto_for_current_game=apply_environment,
        PAGE_OVERVIEW=0,
        PAGE_GAME=1,
    )
    for name in (
        "_recover_pending_preset_applies",
        "_collect_startup_variants",
        "_schedule_activation_reconcile",
        "_load_characters",
        "_start_file_watcher",
        "_refresh_preset_combo",
        "_apply_nav_gating",
        "_select_nav_page",
        "_maybe_startup_update_check",
    ):
        setattr(window, name, Mock())
    try:
        MainWindow._init_data(window)
        assert events == (["prompt", "activate"] if recover else ["prompt"])
        if not recover:
            assert db.read_bytes() == damaged
            window._start_file_watcher.assert_not_called()
            window._collect_startup_variants.assert_not_called()
            window._schedule_activation_reconcile.assert_not_called()
            assert window._game_service.current_game_id == ""
        else:
            assert manager.index_healthy
            window._collect_startup_variants.assert_called_once_with()
    finally:
        manager._index.close()


@pytest.mark.parametrize("recover", [False, True, "failed"])
def test_real_window_can_open_with_corrupt_database(temp_data_dir, qtbot, monkeypatch, recover):
    from PySide6.QtWidgets import QMessageBox

    from src.core.config import Config
    from src.core.game_config_store import GameConfigStore

    register_games("game_a")
    games = GameConfigStore().get_games()
    games[0]["mods_path"] = str(temp_data_dir / "deployed")
    GameConfigStore().set_games(games)
    GameConfigStore().set_last_game_id("game_a")
    Config().set("gamebanana.enabled", False)
    db = temp_data_dir / "data" / "mod_index.db"
    db.write_bytes(b"deliberately damaged SQLite database")
    prompts = []
    original_recovery = ModManager.recover_corrupt_index
    if recover == "failed":

        def fail_recovery(self):
            raise OSError("模拟隔离数据库失败")

        monkeypatch.setattr(ModManager, "recover_corrupt_index", fail_recovery)

    def answer(*args, **kwargs):
        prompts.append(args[1])
        return QMessageBox.StandardButton.Yes if recover else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "critical", answer)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(MainWindow, "_init_plugins", lambda self: None)
    monkeypatch.setattr(MainWindow, "_maybe_startup_update_check", lambda self: None)
    window = MainWindow()
    qtbot.addWidget(window)
    try:
        assert prompts == ["索引损坏"]
        success = recover is True
        assert window._mod_manager.index_healthy == success
        assert window._game_service.current_game_id == ("game_a" if success else "")
        if not success:
            # 暂不恢复后，可以直接从工具页重试，不要求先激活游戏。
            recover = True
            monkeypatch.setattr(ModManager, "recover_corrupt_index", original_recovery)
            window._on_rebuild_index()
            assert prompts == ["索引损坏", "索引损坏"]
            assert window._mod_manager.index_healthy
            assert window._game_service.current_game_id == "game_a"
    finally:
        window.close()
        window._mod_manager._index.close()
