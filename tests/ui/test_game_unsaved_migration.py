from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from src.core.mod_manager import ModManager


@pytest.fixture
def game_page(qtbot, temp_data_dir):
    from src.core.game_config_store import GameConfigStore
    from src.core.game_service import GameService
    from src.ui.pages.game import GamePage

    GameConfigStore.instance().set_games([])  # 干净空环境（程序不内置任何游戏）
    GameService._instance = None
    page = GamePage()
    qtbot.addWidget(page)
    yield page
    GameService._instance = None


def _add_game(page, name: str, mods_path: str = ""):
    from src.core.game_service import GameConfig

    gid = page._svc.save_game(GameConfig(name=name, name_en=name, mods_path=mods_path))
    page.reload()
    return gid


def test_saved_game_field_change_is_detected(game_page):
    gid = _add_game(game_page, "GameA", "D:/old")
    game_page._select(gid)
    assert game_page.has_unsaved_changes() is False

    game_page._detail._mods.setText("D:/new")
    assert game_page.has_unsaved_changes() is True


def test_reselecting_same_deployment_directory_is_saved_as_an_explicit_selection(game_page, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    root = tmp_path / "Selected Mods"
    root.mkdir()
    game_id = _add_game(game_page, "GameA", str(root))
    game_page._select(game_id)
    selections = []
    game_page.deployment_directory_selected.connect(selections.append)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(root))
    game_page._detail._browse_mods()
    assert game_page.has_unsaved_changes()
    # 导航保存操作通过 collect 收集表单，与文本修改后的保存流程一致。
    assert game_page._on_save(game_page._detail.collect())
    assert selections == [game_id]
    assert not game_page.has_unsaved_changes()


def test_cancel_keeps_current_game_and_unsaved_fields(game_page, monkeypatch):
    gid_a = _add_game(game_page, "GameA", "D:/old")
    gid_b = _add_game(game_page, "GameB", "D:/other")
    game_page._select(gid_a)
    game_page._detail._mods.setText("D:/edited")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )

    game_page._select(gid_b)
    assert game_page._selected_id == gid_a
    assert game_page._detail._mods.text() == "D:/edited"


def test_discard_restores_saved_fields_then_allows_deselect(game_page, monkeypatch):
    gid = _add_game(game_page, "GameA", "D:/old")
    game_page._select(gid)
    game_page._detail._mods.setText("D:/edited")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )

    game_page._deselect()
    assert game_page._selected_id == ""
    assert game_page.has_unsaved_changes() is False
    assert game_page._svc.get_game(gid).mods_path == "D:/old"


def test_save_prompt_persists_changes(game_page, monkeypatch, tmp_path):
    old_dir = tmp_path / "old-empty"
    new_dir = tmp_path / "new-empty"
    old_dir.mkdir()
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._select(gid)
    game_page._detail._mods.setText(str(new_dir))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Save,
    )

    assert game_page.confirm_pending_changes() is True
    assert game_page._svc.get_game(gid).mods_path == str(new_dir)
    assert game_page.has_unsaved_changes() is False


def test_changing_deploy_dir_can_migrate_existing_data(game_page, monkeypatch, tmp_path):
    old_dir = tmp_path / "old Mods"
    new_dir = tmp_path / "new Mods"
    (old_dir / "ModA").mkdir(parents=True)
    (old_dir / "ModA" / "merged.ini").write_text("[Constants]\n$x = 1\n", encoding="utf-8")
    (old_dir / "disabled_mods.txt").write_text("keep", encoding="utf-8")
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._select(gid)
    game_page._detail._mods.setText(str(new_dir))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    assert game_page._on_save(game_page._detail.collect()) is True
    assert (new_dir / "ModA" / "merged.ini").is_file()
    assert (new_dir / "disabled_mods.txt").read_text(encoding="utf-8") == "keep"
    # 迁移只搬走内容，保留用户原有的部署目录。
    assert old_dir.is_dir()
    assert list(old_dir.iterdir()) == []
    assert game_page._svc.get_game(gid).mods_path == str(new_dir)


def test_declining_migration_saves_config_and_leaves_old_data(game_page, monkeypatch, tmp_path):
    old_dir = tmp_path / "old Mods"
    new_dir = tmp_path / "new Mods"
    (old_dir / "ModA").mkdir(parents=True)
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._select(gid)
    game_page._detail._mods.setText(str(new_dir))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )

    assert game_page._on_save(game_page._detail.collect()) is True
    assert (old_dir / "ModA").is_dir()
    assert not new_dir.exists()
    assert game_page._svc.get_game(gid).mods_path == str(new_dir)


def _cleared_mods_field(game_page, tmp_path, monkeypatch, answer):
    """把已保存游戏的「Mod 文件夹」清空并保存；返回 (gid, 旧目录, 保存是否成功)。"""
    old_dir = tmp_path / "old Mods"
    (old_dir / "ModA").mkdir(parents=True)
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._select(gid)
    game_page._detail._mods.setText("")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answer)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    return gid, old_dir, game_page._on_save(game_page._detail.collect())


def test_clearing_deploy_dir_yes_clears_old_directory(game_page, monkeypatch, tmp_path):
    calls: list = []

    def fake_clear(self, game_id, deploy_dir):
        calls.append((game_id, Path(deploy_dir)))
        shutil.rmtree(deploy_dir)
        Path(deploy_dir).mkdir()
        return 1, ""

    monkeypatch.setattr(ModManager, "clear_deploy_dir_contents", fake_clear)
    gid, old_dir, saved = _cleared_mods_field(game_page, tmp_path, monkeypatch, QMessageBox.StandardButton.Yes)

    assert saved is True
    assert calls and calls[0][1] == old_dir
    assert list(old_dir.iterdir()) == []
    assert game_page._svc.get_game(gid).mods_path == ""


def test_clearing_deploy_dir_no_keeps_previous_config(game_page, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ModManager, "clear_deploy_dir_contents", lambda self, g, d: pytest.fail("选「否」时不得清空目录")
    )
    gid, old_dir, saved = _cleared_mods_field(game_page, tmp_path, monkeypatch, QMessageBox.StandardButton.No)

    assert saved is True
    assert (old_dir / "ModA").is_dir()
    assert game_page._svc.get_game(gid).mods_path == str(old_dir)


def test_clearing_deploy_dir_abort_on_clear_failure(game_page, monkeypatch, tmp_path):
    monkeypatch.setattr(ModManager, "clear_deploy_dir_contents", lambda self, g, d: (0, "文件被占用"))
    gid, old_dir, saved = _cleared_mods_field(game_page, tmp_path, monkeypatch, QMessageBox.StandardButton.Yes)

    assert saved is False
    assert (old_dir / "ModA").is_dir()
    assert game_page._svc.get_game(gid).mods_path == str(old_dir)


def test_clearing_deploy_dir_really_disables_library_mods(game_page, monkeypatch, tmp_path):
    """通过真实部署和游戏设置保存，验证清空部署目录后文件与索引状态一致。"""
    from src.core.config import Config
    from tests.helpers import scan_and_sync

    Config().set("import.format", "zip")
    old_dir = tmp_path / "old Mods"
    old_dir.mkdir(parents=True)
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._svc.current_game_id = gid

    mm = ModManager()
    mm.set_game(gid)
    mm.set_deploy_dir(str(old_dir))
    char = mm.add_character(name="示例角色丁", node_type="character")
    lib = mm._get_character_library(char.id)
    (lib / "A").mkdir(parents=True, exist_ok=True)
    (lib / "A" / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    assert scan_and_sync(mm) == 1
    mod = mm.get_mods()[0]
    assert mm.toggle_mod(mod.mod_id) is not None
    assert list(old_dir.iterdir()), "先确认真的部署出去了"

    game_page._select(gid)
    game_page._detail._mods.setText("")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    assert game_page._on_save(game_page._detail.collect()) is True

    assert mm.get_mod(mod.mod_id).is_enabled is False, "库里的启用状态必须同步转禁用"
    assert list(old_dir.iterdir()) == []
    assert game_page._svc.get_game(gid).mods_path == ""
    mm.stop_ini_sync()


def test_migration_conflict_aborts_without_partial_move(game_page, monkeypatch, tmp_path):
    old_dir = tmp_path / "old Mods"
    new_dir = tmp_path / "new Mods"
    (old_dir / "ModA").mkdir(parents=True)
    (old_dir / "ModB").mkdir()
    (new_dir / "ModA").mkdir(parents=True)
    gid = _add_game(game_page, "GameA", str(old_dir))
    game_page._select(gid)
    game_page._detail._mods.setText(str(new_dir))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    errors: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, text, *args, **kwargs: errors.append(text),
    )

    assert game_page._on_save(game_page._detail.collect()) is False
    assert (old_dir / "ModA").is_dir()
    assert (old_dir / "ModB").is_dir()
    assert (new_dir / "ModA").is_dir()
    assert game_page._svc.get_game(gid).mods_path == str(old_dir)
    assert errors and "同名" in errors[0]
