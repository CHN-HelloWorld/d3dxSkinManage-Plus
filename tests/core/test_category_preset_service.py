import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from src.core.category_preset import CategoryPresetDocument, CategoryPresetError, CategoryPresetNode
from src.core.category_preset_service import CategoryPresetService
from src.core.mod_manager import ModManager
from src.services.category_preset_file import read_category_preset, write_category_preset
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import register_games, reset_game_service


@pytest.fixture
def manager(temp_data_dir):
    register_games("game_a", "other")
    manager = ModManager()
    manager.set_game("game_a")
    yield manager
    manager._index.close()
    reset_game_service()


def preset(tmp_path, nodes, images=None, filename="source.d3dxcp"):
    return write_category_preset(
        tmp_path / filename,
        CategoryPresetDocument("分享分类", tuple(nodes), suggested_game_name="发送方"),
        images or {},
    ).source_path


def node(path, **kw):
    return CategoryPresetNode(path, kw.pop("name", path.rsplit("/", 1)[-1]), kw.pop("node_type", "category"), **kw)


def snapshot(manager):
    return [tuple(row) for row in manager._index._db().execute("SELECT * FROM nodes ORDER BY id")]


def test_complete_tree_80_plus_6_preserves_existing_and_repeat_is_noop(manager, tmp_path):
    old = [manager.add_character(name=f"目录{i:02}", node_type="category") for i in range(80)]
    before = snapshot(manager)
    markers = {n.id: (manager._index.paths.node_path(n.id) / CATEGORY_MARKER_FILE).read_bytes() for n in old}
    source = preset(
        tmp_path,
        [node(f"目录{i:02}") for i in range(80)] + [node(f"目录00/新增{i}", sort_order=5 - i) for i in range(6)],
    )
    service = CategoryPresetService(manager)
    # 公开操作须可在管理器数据库连接的所有者线程之外执行。
    with ThreadPoolExecutor(1) as worker:
        plan = worker.submit(service.preview_import, source, "game_a").result()
        assert (plan.new_count, plan.existing_count) == (6, 80)
        result = worker.submit(service.execute_import, plan).result()
        again = worker.submit(service.preview_import, source, "game_a").result()
    assert (result.new_count, result.existing_count) == (6, 80)
    assert (again.new_count, again.existing_count, again.can_import) == (0, 86, False)
    assert all(row in snapshot(manager) for row in before)
    for n in old:
        assert (manager._index.paths.node_path(n.id) / CATEGORY_MARKER_FILE).read_bytes() == markers[n.id]
    assert manager._index._db().execute("SELECT count(*) FROM mods").fetchone()[0] == 0
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0
    unchanged = snapshot(manager)
    service.execute_import(again)
    assert snapshot(manager) == unchanged
    service.discard(again)


def test_fill_only_missing_image_preserves_configured_reference_and_append_sort(manager, tmp_path):
    parent = manager.add_character(name="角色", node_type="category")
    keep = manager.add_character(name="已有", parent_id=parent.id)
    missing = manager.add_character(name="补图", parent_id=parent.id)
    conn = manager._index._db()
    conn.execute(
        "UPDATE nodes SET sort_order=40,thumbnail=? WHERE id=?",
        (json.dumps({"scope": "node", "path": "missing.png"}), keep.id),
    )
    conn.commit()
    image = tmp_path / "image.png"
    Image.new("RGB", (3, 3), "red").save(image)
    import hashlib

    image_id = hashlib.sha256(image.read_bytes()).hexdigest()
    source = preset(
        tmp_path,
        [
            node("角色"),
            node("角色/已有", image_id=image_id),
            node("角色/补图", image_id=image_id),
            node("角色/后", sort_order=9),
            node("角色/先", sort_order=1, image_id=image_id),
        ],
        {image_id: image},
    )
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")
    assert (plan.fill_count, plan.new_image_count, plan.new_count) == (1, 1, 2)
    assert next(r for r in plan.rows if r.path == "角色/已有").detail
    service.execute_import(plan)
    rows = {r["directory_name"]: dict(r) for r in conn.execute("SELECT * FROM nodes WHERE parent_id=?", (parent.id,))}
    assert rows["已有"]["thumbnail"] == json.dumps({"scope": "node", "path": "missing.png"})
    assert rows["已有"]["sort_order"] == 40
    assert rows["先"]["sort_order"] > 40
    assert rows["后"]["sort_order"] > rows["先"]["sort_order"]
    reference = json.loads(rows["补图"]["thumbnail"])
    assert manager._index.paths.resolve_image(missing.id, reference).read_bytes() == image.read_bytes()
    marker = json.loads((manager._index.paths.node_path(missing.id) / CATEGORY_MARKER_FILE).read_text("utf-8"))
    assert marker["thumbnail"] == reference


@pytest.mark.parametrize("occupant", ["file", "folder", "mod"])
def test_path_conflict_blocks_whole_import(manager, tmp_path, occupant):
    root = manager._get_library_root("game_a")
    path = root / "占用"
    if occupant == "file":
        path.write_text("external")
    else:
        path.mkdir()
    if occupant == "mod":
        conn = manager._index._db()
        conn.execute(
            "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES('m','game_a','占用','body','now')"
        )
        conn.execute("INSERT INTO deployment_records VALUES('m','active','D:/deployed/m')")
        conn.commit()
    source = preset(tmp_path, [node("安全"), node("占用")])
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")
    assert plan.conflict_count == 1 and not plan.can_import
    assert str(path) in next(r for r in plan.rows if r.status == "conflict").detail
    with pytest.raises(CategoryPresetError):
        service.execute_import(plan)
    assert not (root / "安全").exists()


def test_original_input_changes_do_not_change_fixed_plan_but_staging_tamper_fails(manager, tmp_path):
    source = preset(tmp_path, [node("固定")])
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")
    source.write_bytes(b"changed")
    service.execute_import(plan)
    assert (manager._get_library_root("game_a") / "固定").is_dir()
    source = preset(tmp_path, [node("拒绝")], filename="second.d3dxcp")
    plan = service.preview_import(source, "game_a")
    plan.document.source_path.write_bytes(b"tampered")
    with pytest.raises(CategoryPresetError):
        service.execute_import(plan)
    assert not (manager._get_library_root("game_a") / "拒绝").exists()


def test_export_complete_snapshot_collision_safe_and_no_mod_payload(manager, tmp_path):
    manager.add_character(name="空父", node_type="category")
    service = CategoryPresetService(manager)
    plan = service.preview_export("game_a")
    assert len(plan.rows) == 1
    old = tmp_path / "分享.d3dxcp"
    old.write_bytes(b"old")
    result = service.execute_export(plan, tmp_path, "分享")
    assert old.read_bytes() == b"old" and result.path != old
    document = read_category_preset(result.path)
    assert [n.path for n in document.nodes] == ["空父"]
    assert result.node_count == 1


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("committed", [False, True])
def test_crash_restart_consumes_journal_and_decides_by_database_commit(manager, tmp_path, monkeypatch, committed):
    source = preset(tmp_path, [node("恢复"), node("恢复/叶")])
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")
    original = service._commit_import

    def crash(conn, operation_id, inserts, updates):
        if committed:
            original(conn, operation_id, inserts, updates)
        raise SimulatedCrash()

    monkeypatch.setattr(service, "_commit_import", crash)
    with pytest.raises(SimulatedCrash):
        service.execute_import(plan)
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 1
    service.discard(plan)
    assert plan.staging.exists()
    from src.core.library_protection import LibraryProtection

    manager._library_protection = LibraryProtection()
    assert CategoryPresetService(manager).recover_pending() == ()
    assert (plan.target.root / "恢复").exists() == committed
    assert len(snapshot(manager)) == (2 if committed else 0)
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0
    assert not plan.staging.exists()
    assert manager._library_protection.snapshot() == (set(), set())


def test_external_change_during_rollback_keeps_file_journal_and_protection(manager, tmp_path, monkeypatch):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("保留")]), "game_a")

    def fail(conn, operation_id, inserts, updates):
        (plan.target.root / "保留" / CATEGORY_MARKER_FILE).write_text("external change", encoding="utf-8")
        raise OSError("commit failed")

    monkeypatch.setattr(service, "_commit_import", fail)
    with pytest.raises(CategoryPresetError, match="待恢复"):
        service.execute_import(plan)
    marker = plan.target.root / "保留" / CATEGORY_MARKER_FILE
    assert marker.read_text("utf-8") == "external change"
    assert not snapshot(manager)
    assert manager.is_library_path_protected(marker)
    service.discard(plan)
    assert plan.staging.exists()
    assert service.recover_pending()
    assert marker.read_text("utf-8") == "external change"


@pytest.mark.parametrize("after_commit", [False, True])
def test_cancellation_before_commit_rolls_back_after_commit_returns_success(manager, tmp_path, after_commit):
    from src.core.category_preset import CategoryPresetCancelled

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("取消")]), "game_a")
    stop = False

    def progress(phase, percent):
        nonlocal stop
        if phase == ("已提交" if after_commit else "导入分类"):
            stop = True

    if after_commit:
        result = service.execute_import(plan, cancelled=lambda: stop, progress=progress)
        assert result.new_count == 1
    else:
        with pytest.raises(CategoryPresetCancelled):
            service.execute_import(plan, cancelled=lambda: stop, progress=progress)
    assert bool(snapshot(manager)) == after_commit
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0


def test_manager_startup_runs_recovery_before_returning(manager, tmp_path, monkeypatch):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("启动恢复")]), "game_a")

    def crash(*args):
        raise SimulatedCrash()

    monkeypatch.setattr(service, "_commit_import", crash)
    with pytest.raises(SimulatedCrash):
        service.execute_import(plan)
    manager._index.close()
    ModManager._instance = None
    restarted = ModManager()
    assert not (plan.target.root / "启动恢复").exists()
    assert restarted.category_preset_recovery_errors == ()
    assert restarted._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0
    restarted._index.close()


@pytest.mark.parametrize("change", ["delete", "root", "directory", "marker"])
def test_target_change_after_preview_stops_publication(manager, tmp_path, change):
    existing = manager.add_character(name="已有", node_type="category")
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("已有"), node("新增")]), "game_a")
    store = manager._game_service._store
    if change in ("delete", "root"):
        games = store.get_games()
        if change == "delete":
            games = [g for g in games if g["id"] != "game_a"]
        else:
            next(g for g in games if g["id"] == "game_a")["resource_dir"] = "moved"
        store.set_games(games)
    elif change == "directory":
        (plan.target.root / "新增").mkdir()
    else:
        (manager._index.paths.node_path(existing.id) / CATEGORY_MARKER_FILE).write_text("changed")
    with pytest.raises(CategoryPresetError):
        service.execute_import(plan)
    assert len(snapshot(manager)) == 1
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0


@pytest.mark.parametrize("location", ["library", "other_library", "deployment"])
def test_input_and_output_rejected_inside_any_managed_root(manager, tmp_path, location):
    service = CategoryPresetService(manager)
    manager.add_character(name="分类", node_type="category")
    if location == "deployment":
        destination = tmp_path / "deployed"
        destination.mkdir()
        games = manager._game_service._store.get_games()
        next(g for g in games if g["id"] == "other")["mods_path"] = str(destination)
        manager._game_service._store.set_games(games)
    else:
        destination = manager._get_library_root("game_a" if location == "library" else "other")
    source = preset(destination, [node("导入")])
    with pytest.raises(CategoryPresetError):
        service.preview_import(source, "game_a")
    plan = service.preview_export("game_a")
    with pytest.raises(CategoryPresetError):
        service.execute_export(plan, destination, "导出")
    service.discard(plan)


def test_nested_file_conflicts_are_reported_for_parent_and_children(manager, tmp_path):
    root = manager._get_library_root("game_a")
    (root / "占用").write_text("external")
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("占用"), node("占用/孩子")]), "game_a")
    assert plan.conflict_count == 2
    assert all(row.status == "conflict" for row in plan.rows)


def test_export_bad_images_require_explicit_omission_and_preserve_nodes(manager, tmp_path):
    existing = manager.add_character(name="坏图")
    conn = manager._index._db()
    conn.execute(
        "UPDATE nodes SET thumbnail=? WHERE id=?", (json.dumps({"scope": "node", "path": "missing.png"}), existing.id)
    )
    conn.commit()
    service = CategoryPresetService(manager)
    first = service.preview_export("game_a")
    assert list(first.image_issues) == ["坏图"]
    with pytest.raises(CategoryPresetError):
        service.execute_export(first, tmp_path, "不发布")
    plan = service.preview_export("game_a", omitted_images=("坏图",))
    assert not plan.image_issues and len(plan.rows) == 1
    result = service.execute_export(plan, tmp_path, "目录仍保留")
    document = read_category_preset(result.path)
    assert len(document.nodes) == 1 and not document.images


def test_recompare_retains_fixed_source_and_sees_resolved_conflict(manager, tmp_path):
    root = manager._get_library_root("game_a")
    conflict = root / "冲突"
    conflict.write_text("external")
    service = CategoryPresetService(manager)
    source = preset(tmp_path, [node("冲突")])
    plan = service.preview_import(source, "game_a")
    source.unlink()
    conflict.unlink()
    compared = service.recompare(plan)
    assert compared.staging == plan.staging and compared.can_import
    assert service.execute_import(compared).new_count == 1


@pytest.mark.parametrize("name", ["CONIN$", "CONOUT$.txt", "COM¹", "name.", "name ", "../bad", "😀" * 130])
def test_export_filename_rejects_invalid_windows_names(name):
    from src.core.category_preset_service import category_preset_filename

    with pytest.raises(CategoryPresetError):
        category_preset_filename(name)


@pytest.mark.parametrize("name", ["分类", "分类.D3DXCP", "分类.d3dxcp.D3DXCP"])
def test_export_filename_has_one_canonical_suffix(name):
    from src.core.category_preset_service import category_preset_filename

    assert category_preset_filename(name) == "分类.d3dxcp"


def test_failed_fill_restores_original_marker_and_no_image_reference(manager, tmp_path, monkeypatch):
    existing = manager.add_character(name="补图")
    marker = manager._index.paths.node_path(existing.id) / CATEGORY_MARKER_FILE
    old_bytes = marker.read_bytes()
    image = tmp_path / "fill.png"
    Image.new("RGB", (2, 2), "blue").save(image)
    import hashlib

    image_id = hashlib.sha256(image.read_bytes()).hexdigest()
    source = preset(tmp_path, [node("补图", image_id=image_id)], {image_id: image})
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")

    def fail(*args):
        raise OSError("database commit failed")

    monkeypatch.setattr(service, "_commit_import", fail)
    with pytest.raises(OSError):
        service.execute_import(plan)
    assert marker.read_bytes() == old_bytes
    assert manager._index._db().execute("SELECT thumbnail FROM nodes WHERE id=?", (existing.id,)).fetchone()[0] == ""
    assert not list(marker.parent.glob(".d3dxmeta/category-*"))
    assert service.recompare(plan).can_import
    service.discard(plan)
    assert not plan.staging.exists()


def test_publication_failure_after_one_directory_rolls_back_all(manager, tmp_path, monkeypatch):
    manager._get_library_root("game_a")
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("甲"), node("乙")]), "game_a")
    publish = service._publish
    calls = 0

    def fail_second(action):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        publish(action)

    monkeypatch.setattr(service, "_publish", fail_second)
    with pytest.raises(OSError):
        service.execute_import(plan)
    assert not (plan.target.root / "甲").exists() and not (plan.target.root / "乙").exists()
    assert not snapshot(manager)
    service.discard(plan)
    assert not plan.staging.exists()
