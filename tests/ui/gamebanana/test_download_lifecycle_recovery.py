import pytest

from tests.services.gamebanana.test_download_consumers import _profile
from tests.services.gamebanana.test_download_consumers import manager as manager
from tests.ui.gamebanana.test_download_consumer_handoff import handoff as handoff


@pytest.mark.parametrize("state", ["done", "failed", "canceled"])
def test_request_restarts_terminal_task_without_archive(manager, monkeypatch, state):
    profile = _profile("abcd")
    file = profile.files[0]
    old = manager.request(profile, file, game_id="game-a")
    old._set_state(state)
    scheduled = []
    monkeypatch.setattr(manager, "_schedule", scheduled.append)
    task = manager.request(profile, file, game_id="game-a")
    assert task.state == "queued"
    assert scheduled == [task]


def test_restored_history_without_archive_starts_new_transfer(manager, monkeypatch):
    profile = _profile("abcd")
    file = profile.files[0]
    old = manager.request(profile, file, game_id="game-a")
    manager._persist_history(file, old.archive_path, profile, "game-a")
    manager._tasks.clear()
    manager._restore_history()
    scheduled = []
    monkeypatch.setattr(manager, "_schedule", scheduled.append)
    task = manager.request(profile, file, game_id="game-a")
    assert task.state == "queued"
    assert task.profile is profile
    assert scheduled == [task]


def test_deleted_task_restores_detail_button_and_ignores_late_progress(handoff, monkeypatch):
    case = handoff
    case.archive.unlink()
    page = case.page()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    monkeypatch.setattr(page, "_show_download_flyout", lambda: None)
    case.profile.files = [case.file]
    page.detail_view.load_profile(case.profile)
    page._on_download_requested(case.file)
    task = case.dm.get_task(case.file.file_id)
    button = page.detail_view._file_buttons[case.file.file_id]
    assert not button.isEnabled()
    case.dm.delete_task(case.file.file_id)
    assert button.isEnabled()
    task.progress.emit(1, 2)
    assert button.isEnabled()


def test_deleted_library_mod_can_download_again_after_auto_cleanup(handoff, monkeypatch):
    case = handoff
    page = case.page()
    case.profile.files = [case.file]
    page.detail_view.load_profile(case.profile)
    case.dm._md5_to_path.clear()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    monkeypatch.setattr(page, "_show_download_flyout", lambda: None)
    task = case.dm.request(case.profile, case.file, game_id="game-a")
    task._set_state("done")
    case.dm._on_task_done(task, case.archive)
    page._after_download(case.file, case.archive, case.profile)
    mod = case.manager.get_mods()[0]
    assert page.download_flyout._row_widgets[case.file.file_id].primary_btn.isEnabled()
    assert case.manager.delete_mod(mod.mod_id)
    case.archive.unlink()
    page.notify_mod_deleted(case.profile.mod_id)
    assert not page.download_flyout._row_widgets[case.file.file_id].primary_btn.isEnabled()
    page.detail_view._file_buttons[case.file.file_id].click()
    assert case.dm.get_task(case.file.file_id).state == "queued"


def test_open_completed_download_disabled_without_installed_mod(handoff, monkeypatch):
    case = handoff
    page = case.page()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    case.dm._md5_to_path.clear()
    task = case.dm.request(case.profile, case.file, game_id="game-a")
    task._set_state("done")
    page.download_flyout._refresh()
    assert not page.download_flyout._row_widgets[case.file.file_id].primary_btn.isEnabled()


def test_open_checks_library_file_even_when_index_entry_remains(handoff, monkeypatch):
    case = handoff
    page = case.page()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    case.dm._md5_to_path.clear()
    task = case.dm.request(case.profile, case.file, game_id="game-a")
    task._set_state("done")
    page._after_download(case.file, case.archive, case.profile)
    row = page.download_flyout._row_widgets[case.file.file_id]
    assert row.primary_btn.isEnabled()
    from pathlib import Path

    Path(case.manager.get_mods()[0].path).unlink()
    emitted = []
    row.open_mod_requested.connect(emitted.append)
    row.primary_btn.click()
    assert not emitted
    assert not row.primary_btn.isEnabled()


def test_switching_game_invalidates_open_target_cache(handoff, monkeypatch):
    from types import SimpleNamespace

    from src.services.gamebanana.models import GbInstallMetadata

    case = handoff
    page = case.page()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    case.dm._md5_to_path.clear()
    task = case.dm.request(case.profile, case.file, game_id="game-a")
    task._set_state("done")
    page._after_download(case.file, case.archive, case.profile)
    installed_a = case.manager.get_mods()[0]
    assert page._can_open_downloaded_mod(task)
    case.manager.set_game("game-b")
    character_b = case.manager.add_character(name="B character", node_type="character")
    installed_b = case.manager.import_mod(
        str(case.archive),
        character_b.id,
        gb_meta=GbInstallMetadata.from_dict(installed_a.info.custom_data["gamebanana"]),
    )
    page.set_game("game-b")
    assert not page._can_open_downloaded_mod(task)
    task_b = SimpleNamespace(game_id="game-b", profile=case.profile, file=case.file)
    assert page._downloaded_local_mod_id(task_b) == installed_b.mod_id
    case.manager.set_game("game-a")
    page.set_game("game-a")
    assert page._can_open_downloaded_mod(task)
    assert page._downloaded_local_mod_id(task) == installed_a.mod_id


def test_open_targets_matching_file_not_first_mod_on_same_gb_page(handoff, monkeypatch):
    from types import SimpleNamespace

    from src.ui.main_window import MainWindow

    case = handoff
    page = case.page()
    monkeypatch.setattr(case.dm, "_schedule", lambda task: None)
    case.dm._md5_to_path.clear()
    task = case.dm.request(case.profile, case.file, game_id="game-a")
    task._set_state("done")
    page._after_download(case.file, case.archive, case.profile)
    target = case.manager.get_mods()[0]
    # 同一页面中不同文件和分类的响应先返回。
    unrelated = SimpleNamespace(
        mod_id="unrelated",
        node_id="wrong",
        path=target.path,
        info=SimpleNamespace(custom_data={"gamebanana": {"mod_id": case.profile.mod_id, "file_id": 99}}),
    )
    monkeypatch.setattr(case.manager, "get_mods", lambda: [unrelated, target])
    page.refresh_installed_state()
    opened = []
    page.open_local_mod_in_overview_requested.connect(opened.append)
    page.download_flyout._row_widgets[case.file.file_id].primary_btn.click()
    assert opened == [target.mod_id]
    navigation = []
    host = SimpleNamespace(
        _mod_manager=case.manager,
        PAGE_OVERVIEW=0,
        _select_nav_page=lambda page: None,
        _overview_page=SimpleNamespace(_navigate_to=navigation.append),
    )
    MainWindow._open_local_mod_in_overview(host, opened[0])
    assert navigation == [target.node_id]
