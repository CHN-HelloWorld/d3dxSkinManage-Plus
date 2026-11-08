"""验证后台部署期间的类别浏览，以及列表读取与事务发布的隔离。"""

from threading import Event

import pytest

from src.core.library_snapshot import library_mutation
from src.services.archive import ArchiveService
from src.ui.deploy_worker import DeployWorker
from src.ui.overview_refresh import OverviewRefreshController
from src.ui.pages.overview import OverviewPage
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_category_navigation_and_search_remain_available_during_deployment(qtbot, mm, tmp_path, monkeypatch, mode):
    """暂停真实解压后返回上级、进入其他类别并搜索，部署提交后再呈现新的启用状态。"""
    source = mm.add_character(name="部署类别", node_type="character")
    target = mm.add_character(name="浏览类别", node_type="character")
    deploying = _import_mod(mm, source.id, tmp_path, "deploying")
    visible = _import_mod(mm, target.id, tmp_path, "visible")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.set_current_game("game_a")
    page.set_characters(mm.get_characters_for_ui())
    page.set_current_mods([])
    page._set_view(mode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", page._current_parent_id, page._search_input.text()),
        apply=page.set_current_mods,
        recursive=lambda: bool(page._search_input.text().strip()),
        on_loading=lambda _game: page.begin_loading(),
        on_error=page.loading_failed,
        cache_levels=4,
        parent=page,
    )

    def refresh(*_args):
        """接续页面的类别导航和搜索加载请求。"""
        controller.schedule()

    page.set_mod_loader(refresh)
    page.category_navigated.connect(refresh)
    entered, release = Event(), Event()
    original_extract = ArchiveService.extract

    def delayed_extract(service, *args, **kwargs):
        """在存储变更保护已生效时暂停解压，保留真实归档读写与部署发布。"""
        entered.set()
        if not release.wait(20):
            raise TimeoutError("等待测试释放部署超时")
        return original_extract(service, *args, **kwargs)

    monkeypatch.setattr(ArchiveService, "extract", delayed_extract)
    worker = DeployWorker(
        deploying.mod_id,
        deploying.path,
        mm._storage,
        deploy_name=deploying.folder_name,
        content_hash=deploying.content_hash,
    )
    try:
        page.show()
        page._navigate_to(source.id)
        qtbot.waitUntil(lambda: not controller.active and deploying.mod_id in page._display_idx_map)
        worker.start()
        qtbot.waitUntil(entered.is_set)
        assert mm.library_probe_busy()

        page._navigate_back()
        qtbot.waitUntil(lambda: target.id in page._display_idx_map)
        if mode == "grid":
            qtbot.waitUntil(lambda: target.id in page._vs_card_cache)
            page._vs_card_cache[target.id].double_clicked.emit(target.id)
        else:
            page._table.cellDoubleClicked.emit(page._display_idx_map[target.id], 1)
        qtbot.waitUntil(lambda: not controller.active and visible.mod_id in page._display_idx_map)
        assert set(page._display_idx_map) == {visible.mod_id}

        page._search_input.setText("visible")
        qtbot.waitUntil(lambda: not page._search_timer.isActive() and not controller.active)
        assert set(page._display_idx_map) == {visible.mod_id}
        assert worker.isRunning()
        assert mm.library_probe_busy()

        page._navigate_back()
        page._navigate_to(source.id)
        qtbot.waitUntil(lambda: not controller.active and deploying.mod_id in page._display_idx_map)
        assert not page._mod_by_id(deploying.mod_id).is_enabled

        release.set()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=15000)
        assert worker.result[0], worker.error
        committed = mm.finalize_enable(
            deploying.mod_id,
            True,
            deploy_path=worker.result[1],
            source_receipt=worker.source_receipt,
        )
        assert committed is not None and committed.is_enabled
        controller.schedule()
        qtbot.waitUntil(lambda: not controller.active)
        assert page._mod_by_id(deploying.mod_id).is_enabled
    finally:
        release.set()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=15000)
        worker.wait(1000)
        worker.deleteLater()
        controller.stop()
        page.close()


@pytest.mark.parametrize("owner", ["_storage", "_preset_manager"])
def test_file_work_does_not_discard_partial_or_cached_view(qtbot, mm, tmp_path, monkeypatch, owner):
    """文件作业不改变索引时，已解码记录和已完成缓存都继续可用。"""
    category = mm.add_character(name="浏览类别", node_type="character")
    mods = [_import_mod(mm, category.id, tmp_path, name) for name in ("first", "second")]
    decoded, output = [], []
    original_decode = mm._index._row_to_mod

    def decode(row, cache=None):
        """记录真实行解码，检测后台作业是否导致重复读取同一列表。"""
        decoded.append(row["mod_id"])
        return original_decode(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", category.id),
        apply=lambda direct, _subtree: output.append(direct),
        cache_levels=4,
    )
    controller.BUDGET_MS = 0

    @library_mutation
    def file_work(manager):
        """使用真实变更计数包围列表时间片，模拟文件作业穿过多个界面调度。"""
        assert mm.library_probe_busy()
        controller._slice()

    try:
        controller.schedule()
        controller._slice()
        assert len(decoded) == 1
        file_work(getattr(mm, owner))
        qtbot.waitUntil(lambda: bool(output))
        assert [mod.mod_id for mod in output[-1]] == [mod.mod_id for mod in mods]
        assert decoded == [mod.mod_id for mod in mods]
        controller.schedule()
        assert len(output) == 2
        assert decoded == [mod.mod_id for mod in mods]
    finally:
        controller.stop()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("commit", [False, True])
def test_uncommitted_index_changes_never_reach_new_or_cached_views(qtbot, mm, tmp_path, cached, commit):
    """事务开始后停止读取及缓存发布，提交或回滚后才显示最终记录。"""
    category = mm.add_character(name="事务类别", node_type="character")
    mod = _import_mod(mm, category.id, tmp_path, "original")
    output = []
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", category.id),
        apply=lambda direct, _subtree: output.append(direct),
        cache_levels=4,
    )
    connection = mm._index._db()
    try:
        if cached:
            controller.schedule()
            qtbot.waitUntil(lambda: bool(output))
            output.clear()
        connection.execute("BEGIN")
        controller.schedule()
        controller._slice()
        assert controller.active and output == []
        connection.execute("UPDATE mods SET name=? WHERE mod_id=?", ("committed", mod.mod_id))
        controller._slice()
        assert output == []
        if commit:
            connection.commit()
        else:
            connection.rollback()
        qtbot.waitUntil(lambda: bool(output))
        assert output[0][0].name == ("committed" if commit else "original")
    finally:
        connection.rollback()
        controller.stop()
