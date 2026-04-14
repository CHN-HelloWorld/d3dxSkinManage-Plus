import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def main_window(qtbot, temp_data_dir, monkeypatch, native_qt_teardown):
    from PySide6.QtWidgets import QDialog, QMessageBox

    from src.services.gamebanana import client as client_module
    from src.ui.main_window import MainWindow
    from tests.helpers import reset_game_service

    reset_game_service()

    def _unexpected_message(_parent, title, text, *_args, **_kwargs):
        raise AssertionError(f"未预期的模态提示：{title}：{text}")

    def _unexpected_dialog(dialog):
        raise AssertionError(f"未预期的模态窗口：{dialog.windowTitle()}")

    # 导航用例不需要模态交互，意外提示应显示失败位置，避免阻塞测试进程。
    for method in ("question", "information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, _unexpected_message)
    monkeypatch.setattr(QDialog, "exec", _unexpected_dialog)

    def _fake_client_factory(*args, **kwargs):
        fake = MagicMock()

        async def _fake_aclose():
            return None

        async def _fake_categories(_gid):
            return []

        async def _fake_match_game(_name):
            return []

        async def _fake_search(*a, **kw):
            return ([], False)

        async def _fake_mods_by_category(*a, **kw):
            return ([], False)

        fake.aclose = _fake_aclose
        fake.categories = _fake_categories
        fake.match_game = _fake_match_game
        fake.search = _fake_search
        fake.mods_by_category = _fake_mods_by_category
        return fake

    monkeypatch.setattr(client_module, "GameBananaClient", _fake_client_factory)
    window = MainWindow()
    qtbot.addWidget(window)
    yield window
    window.close()
    reset_game_service()


def test_startup_update_check_timer_is_owned_by_window(main_window):
    """启动更新检查的一次性定时器必须归主窗口所有。

    主窗口销毁后应同步取消定时回调，防止访问已释放的更新服务。
    """
    timer = main_window._startup_update_timer
    assert timer.isSingleShot()  # 一次性
    assert timer.parent() is main_window  # 父子绑定 → 随 window 一起销毁，不会悬空触发


def test_window_has_no_content_hash_backfill_machinery(main_window):
    """启动窗口不创建自动补写资源身份的后台任务。"""
    import src.ui.main_window as mw

    assert not hasattr(mw, "_ContentHashBackfillWorker")
    for attr in (
        "_content_hash_backfill_timer",
        "_content_hash_backfill_worker",
        "_content_hash_backfill_ui_ready",
        "_content_hash_backfill_closing",
        "_content_hash_backfill_pending_game_id",
        "_schedule_content_hash_backfill",
        "_start_content_hash_backfill",
        "_stop_content_hash_backfill",
    ):
        assert not hasattr(main_window, attr), f"回填残留: {attr}"


def test_switching_game_does_not_compute_content_hashes(main_window, monkeypatch):
    from src.models.mod import Mod

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("switching game must not compute content hashes")

    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(fail_if_called))

    main_window._on_game_changed("game_a")

    assert main_window._game_service.current_game_id == "game_a"


def test_nav_has_store_entry(main_window):
    labels = [
        main_window._nav.item(i).data(0x100 + 2)  # UserRole 加二保存完整文本。
        for i in range(main_window._nav.count())
    ]
    # 至少有一个项匹配 "Mod" 字样（中英文皆可）
    assert any("Mod" in (lbl or "") for lbl in labels), labels


def test_game_switch_reloads_overview_update_count(main_window, tmp_path):
    import json

    from src.core.game_service import GameConfig
    from src.services.gamebanana.update_checker import UpdateInfo
    from src.utils.paths import get_data_dir

    window = main_window
    manager = window._mod_manager
    game_ids = []
    expected = {}
    for name, count in (("UpdateGameA", 2), ("UpdateGameB", 1), ("UpdateGameC", 0)):
        gid = window._game_service.save_game(GameConfig(name=name, name_en=name))
        game_ids.append(gid)
        manager.set_game(gid)
        character = manager.add_character(name="Character", node_type="character")
        records = {}
        for index in range(count):
            archive = tmp_path / f"{name}-{index}.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("mod.ini", f"[Constants]\n; {name}-{index}\nx={index}\n")
            mod = manager.import_mod(str(archive), character.id)
            assert mod is not None
            records[mod.mod_id] = UpdateInfo.from_dict(
                {
                    "local_mod_id": mod.mod_id,
                    "latest_file": {"file_id": 11, "file_name": "update.zip"},
                }
            ).to_dict()
        expected[gid] = set(records)
        if records:
            status_path = get_data_dir() / manager._res_dir(gid) / "update_status.json"
            status_path.write_text(json.dumps(records), encoding="utf-8")

    nav_item = next(
        window._nav.item(row)
        for row in range(window._nav.count())
        if window._nav.item(row).data(0x100) == window.PAGE_OVERVIEW
    )
    base = nav_item.data(0x100 + 2)
    expected[""] = set()
    for gid in (*game_ids, "", game_ids[0], game_ids[1]):
        window._on_game_changed(gid)
        count = len(expected[gid])
        assert nav_item.text() == (f"{base}  ({count})" if count else base)
        assert window._overview_page._updatable_ids == expected[gid]


def test_switch_to_mod_store_with_query(main_window, qtbot):
    main_window.switch_to_mod_store_with_query("sigma")
    assert main_window._stack.currentIndex() == main_window.PAGE_STORE
    assert main_window._gb_store_page.filter_bar.search_box.text() == "sigma"


def test_cancel_unsaved_game_keeps_game_page_and_nav_selection(main_window, monkeypatch):
    main_window._select_nav_page(main_window.PAGE_GAME)
    # 清理前恢复关闭确认，使 closeEvent 先停止后台任务，再销毁窗口。
    with monkeypatch.context() as patch:
        patch.setattr(main_window._game_page, "confirm_pending_changes", lambda: False)
        overview_row = next(
            i
            for i in range(main_window._nav.count())
            if main_window._nav.item(i).data(0x100) == main_window.PAGE_OVERVIEW
        )

        main_window._nav.setCurrentRow(overview_row)
        assert main_window._stack.currentIndex() == main_window.PAGE_GAME
        current = main_window._nav.item(main_window._nav.currentRow())
        assert current.data(0x100) == main_window.PAGE_GAME


def _wait_for_game_environment(window, qtbot):
    """等待异步环境激活与当前提交完成，再通过游戏页保存路径设置。"""
    qtbot.waitUntil(lambda: not window._deployment_activation.running and not window._plugin_host_busy(), timeout=15000)


def test_saved_active_game_mods_path_updates_next_deploy(main_window, tmp_path, qtbot):
    from src.core.game_service import GameConfig

    old_deploy = tmp_path / "old Mods"
    new_deploy = tmp_path / "new Mods"
    old_deploy.mkdir()
    new_deploy.mkdir()
    svc = main_window._game_service
    gid = svc.save_game(
        GameConfig(
            name="Deploy Path Game",
            name_en="DeployPathGame",
            mods_path=str(old_deploy),
        )
    )
    svc.current_game_id = gid
    main_window._mod_manager.set_game(gid)
    main_window._apply_migoto_for_current_game()
    assert main_window._mod_manager._storage.get_deploy_dir() == old_deploy
    _wait_for_game_environment(main_window, qtbot)

    # 走真实 GamePage 保存信号链：_on_save → games_changed → MainWindow 重下发部署目录。
    page = main_window._game_page
    page.reload()
    page._select(gid)
    page._detail._mods.setText(str(new_deploy))
    assert page._on_save(page._detail.collect()) is True
    assert main_window._mod_manager._storage.get_deploy_dir() == new_deploy
    _wait_for_game_environment(main_window, qtbot)

    # 部署源必须使用归档本体。
    source = tmp_path / "ExampleMod.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("ExampleMod/merged.ini", "[Constants]\n$state = 0\n")
    manager = main_window._mod_manager
    category = manager.add_character(name="Examples", node_type="character")
    mod = manager.import_mod(str(source), category.id)
    assert mod is not None
    enabled = manager.toggle_mod(mod.mod_id)
    assert enabled is not None
    assert Path(enabled.deployed_path) == new_deploy / "ExampleMod"
    assert not (old_deploy / "ExampleMod").exists()


def test_migoto_path_alone_gives_no_deploy_dir(main_window, tmp_path, qtbot):
    """3DMigoto 路径不能代替显式部署目录，避免部署到未被游戏加载的位置。"""
    from src.core.game_service import GameConfig

    migoto = tmp_path / "3dmigoto"
    (migoto / "Mods").mkdir(parents=True)
    svc = main_window._game_service
    gid = svc.save_game(
        GameConfig(
            name="Migoto Only",
            name_en="MigotoOnly",
            migoto_path=str(migoto),
        )
    )
    svc.current_game_id = gid
    main_window._mod_manager.set_game(gid)
    main_window._apply_migoto_for_current_game()

    assert main_window._mod_manager._storage.get_deploy_dir() is None
    assert main_window._mod_manager.has_deploy_dir() is False
    _wait_for_game_environment(main_window, qtbot)

    # 用户显式指定 Mod 文件夹后才有部署目录（可以指向别处，不必是 <migoto>/Mods）
    explicit = tmp_path / "elsewhere Mods"
    explicit.mkdir()
    page = main_window._game_page
    page.reload()
    page._select(gid)
    page._detail._mods.setText(str(explicit))
    assert page._on_save(page._detail.collect()) is True
    assert main_window._mod_manager._storage.get_deploy_dir() == explicit
