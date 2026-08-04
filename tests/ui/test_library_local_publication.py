import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def window_methods():
    source = Path(__file__).resolve().parents[2] / "src/ui/main_window.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
    names = {
        "_on_source_change",
        "_on_deployment_change",
        "_refresh_from_watcher",
        "_refresh_deploy_changes",
        "_publish_local_library_delta",
    }
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
    namespace = {"Path": Path, "MOD_CONTAINER_MARKER": ".d3dxmod"}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class Timer:
    def __init__(self):
        self.calls = []

    def isActive(self):
        return bool(self.calls)

    def start(self, interval=None):
        self.calls.append(interval)


def test_local_delta_materializes_changed_ids_in_one_batch():
    from src.core.library_publication import LocalPublication

    batches, published = [], []
    mods = [SimpleNamespace(mod_id=str(i), node_id="") for i in range(100)]

    def load(ids):
        batches.append(set(ids))
        return mods

    def forbidden(*args):
        pytest.fail("local publication cannot query each Mod individually")

    host = SimpleNamespace(
        _game_service=SimpleNamespace(current_game_id="game"),
        _mod_manager=SimpleNamespace(get_mod=forbidden, _index=SimpleNamespace(get_mods_by_ids=load)),
        _overview_page=SimpleNamespace(apply_reconcile_delta=lambda *args: published.append(args)),
    )
    window_methods()["_publish_local_library_delta"](host, LocalPublication(changed_ids={m.mod_id for m in mods}))
    assert batches == [{m.mod_id for m in mods}]
    assert published == [(mods, set())]


@pytest.mark.parametrize(
    ("retrying", "hint", "delay"),
    [(False, 0, 0), (False, 27, 27), (False, None, 16), (True, 27, 27), (True, None, 500)],
)
def test_pending_missing_retries_at_grace_deadline_before_failure_backoff(retrying, hint, delay):
    from src.core.library_publication import LocalPublication

    timers, unavailable = [], []
    methods = window_methods()
    methods["call_later"] = lambda ms, context, callback: timers.append((ms, context))
    host = SimpleNamespace(
        _game_service=SimpleNamespace(current_game_id="game"),
        _mod_manager=SimpleNamespace(_index=SimpleNamespace(get_mods_by_ids=lambda ids: [])),
        _overview_page=SimpleNamespace(set_pending_missing_mods=unavailable.append),
        _local_missing_retrying=retrying,
        _confirm_local_missing=lambda: None,
    )
    methods["_publish_local_library_delta"](
        host, LocalPublication(pending_missing_ids={"missing"}, retry_after_ms=hint)
    )
    assert timers == [(delay, host)]
    assert unavailable == ([] if hint == 0 else [{"missing"}])


def test_completed_native_barrier_preempts_existing_missing_timer():
    from src.core.library_publication import LocalPublication

    timers = []
    methods = window_methods()
    methods["call_later"] = lambda ms, context, callback: timers.append((ms, context))
    host = SimpleNamespace(
        _game_service=SimpleNamespace(current_game_id="game"),
        _mod_manager=SimpleNamespace(_index=SimpleNamespace(get_mods_by_ids=lambda ids: [])),
        _overview_page=SimpleNamespace(),
        _local_missing_scheduled=True,
        _confirm_local_missing=lambda: None,
    )
    methods["_publish_local_library_delta"](host, LocalPublication(retry_after_ms=0))
    assert timers == [(0, host)]


def test_source_entry_does_not_probe_or_wait_for_unrelated_work(tmp_path):
    methods = window_methods()
    submitted, published = [], []
    audit_yields = []

    def forbidden(*args, **kwargs):
        pytest.fail("ordinary event entered whole-library pipeline")

    host = SimpleNamespace(
        _file_watcher=SimpleNamespace(generation=3, _watch_path=tmp_path),
        _watcher_events=[],
        _watcher_changed_paths=set(),
        _watcher_debounce=Timer(),
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_source_events=lambda events: submitted.extend(events) or "delta"),
        _publish_local_library_delta=published.append,
        _library_probe=SimpleNamespace(
            invalidate=forbidden, schedule_changes=forbidden, yield_to_local_events=lambda: audit_yields.append(True)
        ),
        _refresh_current=forbidden,
        _import_active=True,
        _preset_apply_worker=object(),
    )
    event = SimpleNamespace(
        root=str(tmp_path), generation=3, path=str(tmp_path / "mod"), destination="", kind="modified"
    )
    stale = SimpleNamespace(root=str(tmp_path), generation=2, path=event.path, destination="", kind="modified")
    methods["_on_source_change"](host, stale)
    assert host._watcher_events == []
    for _ in range(40):
        methods["_on_source_change"](host, event)
    assert host._watcher_debounce.calls == [0]  # 在一轮事件循环内合并通知，不重复启动。
    methods["_refresh_from_watcher"](host)
    assert len(submitted) == 32 and len(host._watcher_events) == 8
    assert published == ["delta"]
    assert len(audit_yields) == 40


def test_deployment_entry_has_its_own_queue_and_preserves_move_pair(tmp_path):
    methods = window_methods()
    submitted = []
    host = SimpleNamespace(
        _deploy_watcher=SimpleNamespace(generation=1, _watch_path=tmp_path),
        _deploy_changed_paths=set(),
        _deploy_watcher_debounce=Timer(),
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_deployment_events=lambda events: submitted.extend(events)),
        _publish_local_library_delta=lambda delta: None,
    )
    event = SimpleNamespace(
        root=str(tmp_path), generation=1, path=str(tmp_path / "old"), destination=str(tmp_path / "new"), kind="moved"
    )
    methods["_on_deployment_change"](host, event)
    methods["_refresh_deploy_changes"](host)
    assert submitted == [event]


def test_ready_missing_is_confirmed_in_drained_source_batch():
    from src.core.library_publication import LocalPublication

    methods = window_methods()
    calls = []
    delta = LocalPublication(pending_missing_ids={"m"}, retry_after_ms=0)
    host = SimpleNamespace(
        _watcher_changed_paths=set(),
        _watcher_events=[object()],
        _watcher_debounce=Timer(),
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(apply_local_source_events=lambda events: delta),
        _publish_local_library_delta=lambda value: calls.append(("publish", host._local_missing_inline)),
        _confirm_local_missing=lambda: calls.append(("confirm", host._local_missing_inline)),
    )
    methods["_refresh_from_watcher"](host)
    assert calls == [("publish", True), ("confirm", False)]


def test_discovery_capture_is_local_not_full_layout(tmp_path):
    import sqlite3

    from src.core.library_paths import LibraryPaths
    from src.core.library_schema import create_schema
    from src.ui.library_discovery import capture_discovery

    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute("INSERT INTO nodes(id,game_id,directory_name) VALUES('node','game','real')")
    paths = LibraryPaths(connection, {"game": tmp_path})
    manager = SimpleNamespace(current_game_id="game", _index=SimpleNamespace(paths=paths))
    request = capture_discovery(manager, {tmp_path / "real/unknown/deep.7z"})
    assert request.local_targets == ((str(tmp_path / "real/unknown"), "node", False),)
    assert request.characters == () and request.existing_paths == frozenset()


def test_discovery_validity_does_not_depend_on_global_probe(qtbot):
    from src.ui.library_discovery import LibraryDiscoveryController

    host = SimpleNamespace(
        closing=False,
        active={"game_id": "game", "changed_paths": {"new"}},
        manager=SimpleNamespace(current_game_id="game"),
    )
    assert LibraryDiscoveryController._current(host)
    host._source_generation, host._discovery_generation = 2, 1
    assert not LibraryDiscoveryController._current(host)


def test_real_delta_renames_one_mod_without_global_filter_or_sort(qtbot, monkeypatch):
    from dataclasses import replace

    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    page._view_mode = "list"
    page._sort_key = "alpha_asc"
    page.set_characters([{"id": "node", "name": "Node"}], refresh=False)
    page._current_parent_id = "node"
    mods = [Mod(mod_id=f"{i:032x}", node_id="node", name=f"Mod {i:03}") for i in range(60)]
    page.set_current_mods(mods, mods)
    page._selected_ids = {mods[10].mod_id}
    original_sort = page._apply_sort

    def limited_sort(cats, values):
        assert len(values) <= 2
        return original_sort(cats, values)

    monkeypatch.setattr(page, "_apply_sort", limited_sort)
    monkeypatch.setattr(page, "_filtered_content", lambda *args: pytest.fail("full filtering"))
    changed = replace(mods[10], name="AAA moved")
    assert page.apply_reconcile_delta([changed], ())
    assert page._display_items[0]["id"] == changed.mod_id
    assert page._selected_ids == {changed.mod_id}


def test_path_delta_does_not_inline_pending_card_population(qtbot, monkeypatch):
    from dataclasses import replace

    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_characters([{"id": "node", "name": "Node"}], refresh=False)
    page._current_parent_id = "node"
    mods = [
        Mod(mod_id=f"{i:032x}", node_id="node", name=f"Mod {i:03}", path=f"C:/before/Mod{i}/body.7z")
        for i in range(100)
    ]
    page.set_current_mods(mods, mods)
    page._selected_ids = {mods[0].mod_id}
    before = list(page._display_idx_map)
    changed = [replace(mod, path=mod.path.replace("/before/", "/after/")) for mod in mods]
    with monkeypatch.context() as patch:
        patch.setattr(
            page, "_vs_create_card", lambda *a, **kw: pytest.fail("path delta constructed an unrelated card inline")
        )
        assert page.apply_reconcile_delta(changed, ())
    assert list(page._display_idx_map) == before
    assert page._selected_ids == {mods[0].mod_id}
    assert all("/after/" in mod.path for mod in page._current_mods)
    assert page._vs_scroll_timer.isActive()


def test_moving_current_node_preserves_descendant_cards_and_updates_breadcrumb(qtbot, monkeypatch):
    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    nodes = [
        {"id": "old", "name": "Old"},
        {"id": "new", "name": "New"},
        {"id": "node", "name": "Node", "parent_id": "old"},
    ]
    page.set_characters(nodes, refresh=False)
    page._current_parent_id = "node"
    mod = Mod(mod_id="a" * 32, node_id="node", name="Visible")
    page.set_current_mods([mod], [mod])
    cards = dict(page._vs_card_cache)
    page._selected_ids = {mod.mod_id}
    with monkeypatch.context() as patch:
        patch.setattr(
            page, "_refresh_current_level", lambda: pytest.fail("node move rebuilt unchanged descendant cards")
        )
        page.apply_node_delta([dict(nodes[2], parent_id="new")])
    assert page._vs_card_cache == cards
    assert page._selected_ids == {mod.mod_id}
    assert page._breadcrumb._nodes[1:] == [("new", "New"), ("node", "Node")]


def test_node_move_updates_search_membership_using_new_parent(qtbot):
    from src.core.library_publication import LocalPublication
    from src.models.mod import Mod
    from src.ui.pages.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    nodes = [
        {"id": "old", "name": "Old"},
        {"id": "new", "name": "New"},
        {"id": "node", "name": "Node", "parent_id": "old"},
    ]
    page.set_characters(nodes, refresh=False)
    page._current_parent_id = "old"
    mod = Mod(mod_id="b" * 32, node_id="node", name="Visible")
    page.set_current_mods([], [mod])
    page._search_input.setText("Visible")
    host = SimpleNamespace(
        _game_service=SimpleNamespace(current_game_id="game"),
        _mod_manager=SimpleNamespace(
            _index=SimpleNamespace(get_mods_by_ids=lambda ids: [mod]),
            get_nodes_for_ui=lambda ids: [dict(nodes[2], parent_id="new")],
        ),
        _overview_page=page,
    )
    window_methods()["_publish_local_library_delta"](
        host, LocalPublication(changed_ids={mod.mod_id}, moved_node_ids={"node"})
    )
    assert mod.mod_id not in page._delta_subtree_positions
    assert mod.mod_id not in page._display_idx_map
