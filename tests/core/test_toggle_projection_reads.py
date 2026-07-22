from types import SimpleNamespace

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_disable_preset_refs_need_only_ids_and_preserve_current_exclusion(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="References", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "target")
    _import_mod(mm, char.id, tmp_path, "unrelated")
    pm = mm.preset_manager
    current = pm.get_current("game_a")
    current.character_mods = {char.id: [mod.mod_id]}
    other = pm.create_preset("game_a", "Other", {})

    def forbidden(*args):
        raise AssertionError("preset-reference membership must not decode full Mods")

    monkeypatch.setattr(mm._index, "_row_to_mod", forbidden)
    assert mm._deployment_has_preset_refs(mod.mod_id)
    assert not mm._deployment_has_preset_refs(mod.mod_id, excluding_mod_id=mod.mod_id)
    other.character_mods = {char.id: [mod.mod_id]}
    assert mm._deployment_has_preset_refs(mod.mod_id, excluding_mod_id=mod.mod_id)
    assert not mm._deployment_has_preset_refs("123456781234423482341234567890ab")


def _indexed_mod(mm, tmp_path, mod_id, node_id, *, enabled=False, name=""):
    from src.models.deployment import DeploymentRecord, DeploymentState
    from tests.helpers import make_mod

    mod = make_mod(mm._index, name or mod_id, node_id=node_id)
    assert mm._index.add_mod(mod)
    if enabled:
        mm._index.upsert_deployment(DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, ""))
    return mod


def test_character_conflict_query_decodes_only_candidates_in_scope_order(mm, tmp_path, monkeypatch):
    first = mm.add_character(name="First", node_type="character")
    second = mm.add_character(name="Second", node_type="character")
    active = _indexed_mod(mm, tmp_path, "active", first.id, enabled=True, name="Z")
    pending = _indexed_mod(mm, tmp_path, "pending", first.id, name="A")
    last = _indexed_mod(mm, tmp_path, "last", second.id, enabled=True, name="B")
    target = _indexed_mod(mm, tmp_path, "target", first.id, enabled=True)
    _indexed_mod(mm, tmp_path, "disabled", first.id)
    _indexed_mod(mm, tmp_path, "outside", mm.add_character("Outside").id, enabled=True)
    mm._index.game_id = "game_b"
    foreign = mm.add_character("Other", game_id="game_b")
    _indexed_mod(mm, tmp_path, "other-game", foreign.id, enabled=True)

    decoded = []
    original = mm._index._row_to_mod

    def decode(row, *args):
        assert row["mod_id"] in {active.mod_id, pending.mod_id, last.mod_id}
        decoded.append(row["mod_id"])
        return original(row, *args)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    candidates = mm.get_character_conflict_mods(
        [second.id, first.id],
        deploying_ids={pending.mod_id, active.mod_id, target.mod_id, "outside", "other-game"},
        excluding_mod_id=target.mod_id,
    )
    assert [mod.mod_id for mod in candidates] == [last.mod_id, pending.mod_id, active.mod_id]
    assert sorted(decoded) == sorted([active.mod_id, pending.mod_id, last.mod_id])
    assert mm._index.game_id == "game_b", "explicit game query must not mutate selected index scope"
    assert mm.get_character_conflict_mods([], deploying_ids={pending.mod_id}) == []


def test_gui_conflict_preserves_pending_block_and_ordered_disable(mm, tmp_path, monkeypatch):
    from src.ui.main_window import MainWindow, QMessageBox

    parent = mm.add_character(name="Character", node_type="character")
    child = mm.add_character(name="Child", parent_id=parent.id, node_type="category")
    target = _indexed_mod(mm, tmp_path, "target", child.id)
    active = _indexed_mod(mm, tmp_path, "active", parent.id, enabled=True)
    pending = _indexed_mod(mm, tmp_path, "pending", child.id)
    disabled_mod = _indexed_mod(mm, tmp_path, "disabled", parent.id)
    deploying, disabled, messages = {pending.mod_id}, [], []
    original = mm._index._row_to_mod

    def decode(row, *args):
        assert row["mod_id"] != disabled_mod.mod_id, "GUI conflict check must not decode unrelated disabled siblings"
        return original(row, *args)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    # 此查询测试使用合成源路径；文件预检由真实容器部署测试覆盖。
    monkeypatch.setattr(mm, "check_mod_operation", lambda mod_id, **kwargs: target if mod_id == target.mod_id else None)
    monkeypatch.setattr(mm, "toggle_mod", lambda mod_id: disabled.append(mod_id) or True)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    window = SimpleNamespace(
        _mod_manager=mm,
        _mods_being_deployed=lambda: deploying,
        _config=SimpleNamespace(get=lambda *args: True),
        _CONFLICT_SUPPRESS_KEY=MainWindow._CONFLICT_SUPPRESS_KEY,
    )
    window._character_conflict_scope = lambda mod: MainWindow._character_conflict_scope(window, mod)
    assert MainWindow._resolve_character_conflict(window, target) == (False, [])
    assert disabled == [] and pending.display_name in messages[0]
    deploying.clear()
    assert MainWindow._resolve_character_conflict(window, target) == (True, [active.mod_id])
    assert disabled == [active.mod_id]


def test_batch_reference_projection_preserves_parking_without_decoding_unselected_mods(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="Batch", node_type="character")
    kept = _import_mod(mm, char.id, tmp_path, "kept")
    removed = _import_mod(mm, char.id, tmp_path, "removed")
    _import_mod(mm, char.id, tmp_path, "unselected")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    assert mm.toggle_mod(kept.mod_id).is_enabled
    assert mm.toggle_mod(removed.mod_id).is_enabled
    mm.preset_manager.create_preset("game_a", "Keep", {char.id: [kept.mod_id]})
    original = mm._index._row_to_mod

    def decode(row, *args):
        assert row["mod_id"] in {kept.mod_id, removed.mod_id}, "batch parking needs no unrelated full Mods"
        return original(row, *args)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    entries = mm.prepare_disable_batch([kept.mod_id, removed.mod_id])
    assert [(entry.mod_id, entry.park) for entry in entries] == [(kept.mod_id, True), (removed.mod_id, False)]
    results = [mm.disable_batch_step(entry) for entry in entries]
    assert all(result.ok for result in results)
    assert mm.commit_disable_batch(results) == [kept.mod_id, removed.mod_id]
    assert not mm.get_mod(kept.mod_id).is_enabled
    assert not mm.get_mod(removed.mod_id).is_enabled
    assert mm.get_mod(kept.mod_id).deployed_path
    assert not mm.get_mod(removed.mod_id).deployed_path
