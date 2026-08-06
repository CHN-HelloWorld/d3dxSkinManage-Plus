from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock


def _patch_gb_client(monkeypatch) -> None:
    """避免测试构造商店页时触发任何真实网络客户端行为。"""
    from src.services.gamebanana import client as client_mod

    def _fake_client_factory(*_args, **_kwargs):
        fake = MagicMock()

        async def _aclose():
            return None

        async def _categories(_game_id):
            return []

        async def _records(*_args, **_kwargs):
            return ([], False)

        fake.aclose = _aclose
        fake.categories = _categories
        fake.search = _records
        fake.mods_by_category = _records
        return fake

    monkeypatch.setattr(client_mod, "GameBananaClient", _fake_client_factory)


def _register_game(gid: str = "game_a") -> None:
    """注册活动游戏，使启动对账使用实际游戏配置。"""
    from src.core.game_config_store import GameConfigStore

    GameConfigStore().set_games(
        [
            {
                "id": gid,
                "name": gid,
                "name_en": gid,
                "mods_path": "",
                "resource_dir": gid,
            }
        ]
    )


def _set_last_game(gid: str) -> None:
    from src.core.game_config_store import GameConfigStore

    GameConfigStore().set_last_game_id(gid)


def test_main_window_startup_reconciles_deleted_source_container(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from src.core import game_service as game_service_mod
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    game_service_mod.GameService._instance = None
    cfg = Config()
    _set_last_game("game_a")
    _register_game()
    cfg.set("import.format", "zip")
    cfg.set("import.strict_format", False)
    deploy_root = tmp_path / "deploy"

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="Character", node_type="character")
    source = tmp_path / "StartupMod"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = mm.import_mod(str(source), char.id, name="Startup Mod")
    assert mod is not None
    mm.set_deploy_dir(str(deploy_root))
    assert mm.toggle_mod(mod.mod_id).is_enabled
    deploy_target = deploy_root / mod.folder_name
    shutil.rmtree(Path(mod.path).parent)

    _patch_gb_client(monkeypatch)

    from src.ui.main_window import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    try:
        qtbot.waitUntil(lambda: mm.get_mod(mod.mod_id) is None, timeout=5000)
        assert mm.get_mod(mod.mod_id) is None
        assert not deploy_target.exists()
    finally:
        win.close()
        qtbot.waitUntil(
            lambda: (
                win._library_probe.worker is None
                and win._library_discovery.worker is None
                and not win._library_probe.applying
            ),
            timeout=5000,
        )


def test_running_window_automatically_reconciles_deleted_source_container(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from src.core import game_service as game_service_mod
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    game_service_mod.GameService._instance = None
    cfg = Config()
    _set_last_game("game_a")
    _register_game()
    cfg.set("import.format", "zip")
    cfg.set("import.strict_format", False)
    deploy_root = tmp_path / "deploy"

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="Character", node_type="character")
    source = tmp_path / "LiveMod"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = mm.import_mod(str(source), char.id, name="Live Mod")
    assert mod is not None
    mm.set_deploy_dir(str(deploy_root))
    assert mm.toggle_mod(mod.mod_id).is_enabled
    deploy_target = deploy_root / mod.folder_name

    _patch_gb_client(monkeypatch)

    from src.ui.main_window import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    try:
        assert win._source_state_poll.isActive()
        win._file_watcher.stop()  # 强制走轮询回退处理，而不是依赖本次恰好收到删除事件
        shutil.rmtree(Path(mod.path).parent)

        qtbot.waitUntil(lambda: mm.get_mod(mod.mod_id) is None, timeout=5000)
        assert not deploy_target.exists()
    finally:
        win.close()


def test_main_window_startup_intakes_content_dropped_while_closed(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    """程序关闭期间投放的普通 Mod 应在启动后自动纳管并写入索引。"""
    from src.core import game_service as game_service_mod
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    game_service_mod.GameService._instance = None
    cfg = Config()
    _set_last_game("game_a")
    _register_game()
    cfg.set("import.format", "zip")
    cfg.set("import.strict_format", False)
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="Character", node_type="character")
    dropped = mm._get_character_library(char.id) / "OfflineDrop"
    dropped.mkdir(parents=True)
    (dropped / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")

    _patch_gb_client(monkeypatch)

    from src.ui.main_window import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    try:
        qtbot.waitUntil(lambda: len(mm.get_mods(node_id=char.id)) == 1, timeout=10000)
        registered = mm.get_mods(node_id=char.id)[0]
        body = Path(registered.path)
        # 受管路径必须位于容器内，使 INI 编辑和补充预览能够定位元数据目录。
        assert (body.parent / ".d3dxmod").is_file()
        assert registered.content_hash
    finally:
        win.close()
