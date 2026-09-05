import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_idle_directory_prefetch_publishes_only_after_navigation_and_retires_stale_data(
    qtbot, mm, tmp_path, monkeypatch
):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Prepared", node_type="character")
    mod = _import_mod(mm, parent.id, tmp_path, "original")
    current, output, decoded = ["game_a", ""], [], []
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        decoded.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: tuple(current),
        recursive=lambda: False,
        cache_levels=4,
        apply=lambda direct, subtree: output.append(direct),
    )
    try:
        controller.prefetch("game_a", [parent.id])
        qtbot.waitUntil(lambda: ("game_a", parent.id, False) in controller._cache)
        assert output == [] and decoded == [mod.mod_id]
        current[1] = parent.id
        controller.schedule()
        assert len(output) == 1 and output[0][0].name == "original"
        assert decoded == [mod.mod_id]
        mm._index._db().execute("UPDATE mods SET name=? WHERE mod_id=?", ("changed", mod.mod_id))
        mm._index._db().commit()
        controller.schedule()
        qtbot.waitUntil(lambda: len(output) == 2)
        assert output[-1][0].name == "changed" and len(decoded) == 2
    finally:
        controller.stop()


@pytest.mark.parametrize("changed", [False, True])
def test_navigation_reuses_partial_prefetch_only_while_index_is_current(qtbot, mm, tmp_path, monkeypatch, changed):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    mods = [_import_mod(mm, parent.id, tmp_path, f"partial-{i}") for i in range(3)]
    current, output, decoded = ["game_a", ""], [], []
    clock = [0.0]
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        decoded.append(row["mod_id"])
        clock[0] += 0.02  # 慢记录在一次真实解码后让出执行权。
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    monkeypatch.setattr("src.ui.overview_refresh.time.perf_counter", lambda: clock[0])
    controller = OverviewRefreshController(
        mm,
        current=lambda: tuple(current),
        recursive=lambda: False,
        cache_levels=4,
        apply=lambda direct, subtree: output.append(direct),
    )
    try:
        controller.prefetch("game_a", [parent.id])
        prefetch = controller._prefetch_controller
        prefetch._timer.stop()
        prefetch._slice()
        assert len(decoded) == 1 and output == []
        first = decoded[0]
        if changed:
            mm._index._db().execute("UPDATE mods SET name=? WHERE mod_id=?", ("changed", first))
            mm._index._db().commit()
        current[1] = parent.id
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        assert {mod.mod_id for mod in output[0]} == {mod.mod_id for mod in mods}
        assert decoded.count(first) == (2 if changed else 1)
        if changed:
            assert next(mod.name for mod in output[0] if mod.mod_id == first) == "changed"
        assert not prefetch.active and not prefetch._timer.isActive()
    finally:
        controller.stop()


@pytest.mark.parametrize("cached", [False, True])
def test_immediate_apply_error_releases_the_loading_state(qtbot, mm, cached):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    broken, output, errors = [False], [], []

    def apply(direct, subtree):
        if broken[0]:
            raise RuntimeError("view update failed")
        output.append((direct, subtree))

    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", parent.id if cached else ""),
        recursive=lambda: False,
        cache_levels=4,
        apply=apply,
        on_error=lambda: errors.append(True),
    )
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        broken[0] = True
        controller.schedule()
        assert errors == [True]
        assert not controller.active
    finally:
        controller.stop()


def test_direct_directory_reads_only_its_own_mods_and_reuses_valid_data(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    child = mm.add_character(name="Child", parent_id=parent.id, node_type="character")
    direct = _import_mod(mm, parent.id, tmp_path, "direct")
    nested = _import_mod(mm, child.id, tmp_path, "nested")
    decoded, output = [], []
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        decoded.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", parent.id),
        recursive=lambda: False,
        cache_levels=4,
        apply=lambda own, tree: output.append((own, tree)),
    )
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: len(output) == 1)
        assert [mod.mod_id for mod in output[0][0]] == [direct.mod_id]
        assert decoded == [direct.mod_id]
        controller.schedule()
        assert len(output) == 2, "returning to a valid cached level should publish immediately"
        assert decoded == [direct.mod_id]
        mm._index._db().execute("UPDATE mods SET name=? WHERE mod_id=?", ("Renamed", direct.mod_id))
        mm._index._db().commit()
        controller.schedule()
        qtbot.waitUntil(lambda: len(output) == 3)
        assert output[-1][0][0].name == "Renamed"
        assert nested.mod_id not in decoded
    finally:
        controller.stop()


def test_root_without_query_never_decodes_descendant_mods(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    _import_mod(mm, parent.id, tmp_path, "nested")
    output, decoded = [], []
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        decoded.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", ""),
        recursive=lambda: False,
        apply=lambda direct, subtree: output.append((direct, subtree)),
    )
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        assert output == [([], [])]
        assert decoded == []
    finally:
        controller.stop()


def test_query_change_during_decode_discards_old_scope(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    child = mm.add_character(name="Child", parent_id=parent.id, node_type="character")
    direct = _import_mod(mm, parent.id, tmp_path, "direct")
    nested = _import_mod(mm, child.id, tmp_path, "nested")
    query, output = [""], []
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        result = original(row, cache)
        query[0] = "nested"
        return result

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", parent.id, query[0]),
        recursive=lambda: bool(query[0]),
        apply=lambda own, tree: output.append((own, tree)),
    )
    controller.BUDGET_MS = 0
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        assert len(output) == 1
        assert [mod.mod_id for mod in output[0][0]] == [direct.mod_id]
        assert [mod.mod_id for mod in output[0][1]] == [direct.mod_id, nested.mod_id]
    finally:
        controller.stop()


def test_overview_decodes_once_preserving_parent_and_subtree_order(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    child = mm.add_character(name="Child", parent_id=parent.id, node_type="character")
    a = _import_mod(mm, parent.id, tmp_path, "zebra")
    b = _import_mod(mm, parent.id, tmp_path, "alpha")
    c = _import_mod(mm, child.id, tmp_path, "child")
    calls, output = [], []
    original = mm._index._row_to_mod

    def decode(row, cache=None):
        calls.append(row["mod_id"])
        assert not mm._index._db().in_transaction
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    controller = OverviewRefreshController(
        mm, current=lambda: ("game_a", parent.id), apply=lambda direct, subtree: output.append((direct, subtree))
    )
    controller.BUDGET_MS = 0
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        direct, subtree = output[0]
        assert [m.mod_id for m in direct] == [b.mod_id, a.mod_id]
        assert [m.mod_id for m in subtree] == [b.mod_id, a.mod_id, c.mod_id]
        assert len(calls) == len(set(calls)) == 3
        assert direct is not subtree and direct[0] is subtree[0]
    finally:
        controller.stop()


def test_cold_row_preparation_runs_in_budgeted_turns_before_publication(qtbot, mm, tmp_path):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    first = _import_mod(mm, parent.id, tmp_path, "one")
    second = _import_mod(mm, parent.id, tmp_path, "two")
    prepared, output = [], []
    controller = OverviewRefreshController(
        mm,
        current=lambda: ("game_a", parent.id),
        prepare_item=lambda mod: prepared.append(mod.mod_id),
        apply=lambda direct, subtree: output.append((list(prepared), direct)),
    )
    controller.BUDGET_MS = 0
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(output))
        assert set(output[0][0]) == {first.mod_id, second.mod_id}
        assert len(prepared) == 2
        assert len(controller.metrics["slice_ms"]) >= 3
    finally:
        controller.stop()


@pytest.mark.parametrize("change", ["navigation", "sql", "cancel"])
def test_partial_overview_never_publishes_after_change(qtbot, mm, tmp_path, monkeypatch, change):
    from src.ui.overview_refresh import OverviewRefreshController

    parent = mm.add_character(name="Parent", node_type="character")
    child = mm.add_character(name="Other", node_type="character")
    a = _import_mod(mm, parent.id, tmp_path, "old")
    b = _import_mod(mm, child.id, tmp_path, "new")
    current, output, changed = ["game_a", parent.id], [], []
    original = mm._index._row_to_mod
    controller = OverviewRefreshController(
        mm, current=lambda: tuple(current), apply=lambda direct, subtree: output.append(direct)
    )
    controller.BUDGET_MS = 0

    def decode(row, cache=None):
        result = original(row, cache)
        if not changed:
            changed.append(1)
            if change == "navigation":
                current[1] = child.id
            elif change == "sql":
                mm._index._db().execute("UPDATE mods SET name=? WHERE mod_id=?", ("Updated", a.mod_id))
                mm._index._db().commit()
            else:
                # 当前解码返回后，在下一轮 Qt 事件中停止。
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, controller.stop)
        return result

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller.closing or bool(output))
        if change == "cancel":
            assert output == [] and not controller.active
        elif change == "navigation":
            assert len(output) == 1 and [m.mod_id for m in output[0]] == [b.mod_id]
        else:
            assert len(output) == 1 and output[0][0].name == "Updated"
    finally:
        controller.stop()
