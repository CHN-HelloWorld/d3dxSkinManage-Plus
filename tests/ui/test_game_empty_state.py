import pytest

from tests.ui.test_game_unsaved_migration import game_page as game_page


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_empty_guide_can_enter_and_leave_the_first_game_draft(game_page, mode):
    page = game_page
    page._set_view(mode)
    page.resize(1100, 700)
    page.show()

    assert page._empty_add_btn.isVisible()
    assert not page._detail.isVisible()
    assert not page._search.isVisible()
    assert not page._size_slider.isVisible()

    page._empty_add_btn.click()

    assert page._detail._name.isVisible()
    assert page._detail._confirm_btn.isVisible()
    assert page._search.isVisible()
    assert page._size_slider.isVisible() is (mode == "grid")
    assert not page._empty_add_btn.isVisible()
    assert page._views.currentWidget() is (page._scroll if mode == "grid" else page._table)

    page._detail._exit_btn.click()

    assert page._svc.games == []
    assert page._empty_add_btn.isVisible()
    assert not page._detail.isVisible()
    assert not page._search.isVisible()
    assert not page._size_slider.isVisible()


def test_saving_first_game_and_removing_last_game_switches_the_guide(game_page, monkeypatch):
    page = game_page
    page.resize(1100, 700)
    page.show()
    page._empty_add_btn.click()
    page._detail._name.setText("测试游戏")
    page._detail._name_en.setText("Test Game")
    page._detail._confirm_btn.click()

    game = page._svc.games[0]
    assert not page._empty_add_btn.isVisible()
    assert page._detail._save_btn.isVisible()
    assert page._search.isVisible()

    # 确认交互由删除对话框测试覆盖，此处保留实际删除。
    monkeypatch.setattr(page, "_confirm_delete", lambda game: True)
    page._on_delete(game.id)

    assert page._svc.games == []
    assert page._empty_add_btn.isVisible()
    assert not page._detail.isVisible()
    assert not page._search.isVisible()
