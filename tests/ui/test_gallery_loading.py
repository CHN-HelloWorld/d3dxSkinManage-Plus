"""验证画廊加载状态、异步结果与隐藏页面的布局边界。"""

from types import SimpleNamespace

import pytest

from src.ui.overview_refresh import OverviewRefreshController
from src.ui.pages.gallery import GalleryPage
from src.utils.i18n import t
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.fixture
def page(qtbot, temp_data_dir):
    widget = GalleryPage()
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    # 执行与主窗口加载前相同的初始化。
    widget.set_adult_mode("show")
    widget.show()
    yield widget
    widget.close()


def _controller(manager, page, current):
    controller = OverviewRefreshController(
        manager,
        current=lambda: (current[0], ""),
        apply=lambda _direct, mods: page.set_mods(mods),
        on_loading=page.set_loading,
        on_error=page.set_load_failed,
        on_progress=page.set_loading_progress,
        parent=page,
    )
    controller.BUDGET_MS = 0
    return controller


def _has_empty_message(page):
    return page._empty_label.isVisible() and page._empty_label.text() == t("暂无 Mod 数据\n请先在类别页面导入 Mod")


def test_unloaded_gallery_does_not_claim_library_is_empty(page):
    assert not _has_empty_message(page)
    assert page._loading_panel.isVisible()


def test_hidden_gallery_can_refresh_before_its_parent_has_a_layout(qtbot, temp_data_dir, qtlog):
    """隐藏视口尺寸不足时不提交负高度，进入页面后仍能恢复正常布局。"""
    from PySide6.QtWidgets import QStackedWidget

    stack = QStackedWidget()
    qtbot.addWidget(stack)
    stack.resize(100, 30)
    widget = GalleryPage(stack)
    try:
        widget.resize(100, 30)
        widget._scroll_area.viewport().resize(20, 10)
        widget._check_and_refresh_layout()

        assert not [record.message for record in qtlog.records if "Negative sizes" in record.message]

        stack.addWidget(widget)
        stack.resize(900, 600)
        stack.show()
        qtbot.waitUntil(lambda: widget._loading_panel.height() > 100)
        assert widget._loading_panel.isVisible()
    finally:
        widget.close()


def test_pending_gallery_shows_loading_through_publication_and_decode(qtbot, mm, page, tmp_path, monkeypatch):
    category = mm.add_character(name="Character", node_type="character")
    mods = [_import_mod(mm, category.id, tmp_path, name) for name in ("one", "two")]
    busy = [True]
    monkeypatch.setattr(mm, "library_view_busy", lambda: busy[0])
    controller = _controller(mm, page, ["game_a"])
    try:
        controller.schedule()
        assert page._loading_panel.isVisible()
        assert page._empty_label.isHidden()
        qtbot.wait(60)
        page.set_adult_mode("blur")
        assert not _has_empty_message(page)
        assert controller.active
        assert page._loading_panel.isVisible()
        busy[0] = False
        qtbot.waitUntil(lambda: not controller.active)
        assert {mod.mod_id for mod in page._mods} == {mod.mod_id for mod in mods}
        assert page._empty_label.isHidden()
        assert page._loading_panel.isHidden()
    finally:
        controller.stop()


def test_confirmed_empty_result_finishes_loading_even_when_unchanged(qtbot, mm, page):
    controller = _controller(mm, page, ["game_a"])
    try:
        for _ in range(2):
            controller.schedule()
            assert not _has_empty_message(page)
            qtbot.waitUntil(lambda: not controller.active)
            assert _has_empty_message(page)
    finally:
        controller.stop()


@pytest.mark.parametrize("while_waiting", [False, True])
def test_game_change_replaces_previous_gallery_before_new_result(
    qtbot,
    mm,
    page,
    tmp_path,
    monkeypatch,
    while_waiting,
):
    category = mm.add_character(name="First", node_type="character")
    first = _import_mod(mm, category.id, tmp_path, "first")
    mm.set_game("game_b")
    other = mm.add_character(name="Second", node_type="character")
    second = _import_mod(mm, other.id, tmp_path, "second")
    current = ["game_a"]
    busy = [False]
    monkeypatch.setattr(mm, "library_view_busy", lambda: busy[0])
    controller = _controller(mm, page, current)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: not controller.active)
        assert [mod.mod_id for mod in page._mods] == [first.mod_id]
        busy[0] = while_waiting
        controller.schedule()
        if while_waiting:
            qtbot.wait(60)
        current[0] = "game_b"
        if not while_waiting:
            controller.schedule()
        qtbot.waitUntil(lambda: not page._mods)
        assert not _has_empty_message(page)
        assert page._loading_panel.isVisible()
        busy[0] = False
        qtbot.waitUntil(lambda: not controller.active)
        assert [mod.mod_id for mod in page._mods] == [second.mod_id]
    finally:
        controller.stop()


def test_refresh_preserves_populated_cards(qtbot, mm, page, tmp_path):
    category = mm.add_character(name="Character", node_type="character")
    _import_mod(mm, category.id, tmp_path, "one")
    controller = _controller(mm, page, ["game_a"])
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: not controller.active)
        cards = dict(page._vs_card_cache)
        assert cards
        controller.schedule()
        assert page._vs_card_cache == cards
        assert page._empty_label.isHidden()
        qtbot.waitUntil(lambda: not controller.active)
        assert page._vs_card_cache == cards
    finally:
        controller.stop()


def test_load_error_does_not_report_empty_and_retry_can_finish(qtbot, mm, page, monkeypatch):
    original = mm._index.iter_mods

    def fail(*args):
        raise OSError("test read failure")

    monkeypatch.setattr(mm._index, "iter_mods", fail)
    controller = _controller(mm, page, ["game_a"])
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: not controller.active)
        assert not _has_empty_message(page)
        assert page._empty_label.isVisible()
        assert page._empty_label.text() == t("Mod 数据加载失败\n请按 F5 重试")
        assert page._loading_panel.isHidden()
        monkeypatch.setattr(mm._index, "iter_mods", original)
        controller.schedule()
        qtbot.waitUntil(lambda: not controller.active)
        assert _has_empty_message(page)
    finally:
        controller.stop()


def test_loading_reports_real_decode_count_and_resets_for_another_game(qtbot, mm, page, tmp_path):
    category = mm.add_character(name="Character", node_type="character")
    for index in range(3):
        _import_mod(mm, category.id, tmp_path, f"mod{index}")
    controller = _controller(mm, page, ["game_a"])
    controller.YIELD_MS = 100
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: "1" in page._loading_panel.detail.text())
        assert page._loading_panel.isVisible()
        assert page._mods == []
        qtbot.waitUntil(lambda: not controller.active)
        assert page._loading_panel.isHidden()
        page.set_loading("game_b")
        assert page._loading_panel.detail.text() == t("正在准备 Mod 库，完成后将显示预览")
    finally:
        controller.stop()


def test_deactivation_cancels_pending_gallery_result(qtbot, mm, page, tmp_path):
    from src.ui.main_window import MainWindow

    category = mm.add_character(name="Character", node_type="character")
    _import_mod(mm, category.id, tmp_path, "one")
    current = ["game_a"]
    controller = _controller(mm, page, current)
    owner = SimpleNamespace(
        _game_service=SimpleNamespace(has_active_game=lambda: False),
        _gallery_refresh=controller,
        _gallery_page=page,
    )
    try:
        controller.schedule()
        current[0] = ""
        MainWindow._refresh_gallery(owner)
        qtbot.wait(80)
        assert not controller.active
        assert page._mods == []
        assert _has_empty_message(page)
    finally:
        controller.stop()
