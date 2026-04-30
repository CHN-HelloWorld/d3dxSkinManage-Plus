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


def test_prepare_failure_cleans_only_operation_staging_and_allows_retry(manager, tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "red").save(image)
    import hashlib

    import src.core.category_preset_service as module

    image_id = hashlib.sha256(image.read_bytes()).hexdigest()
    source = preset(tmp_path, [node("重试", image_id=image_id)], {image_id: image})
    service = CategoryPresetService(manager)
    plan = service.preview_import(source, "game_a")
    original = module.copy_category_preset_image

    def fail(*args, **kwargs):
        raise OSError("image copy disk full")

    monkeypatch.setattr(module, "copy_category_preset_image", fail)
    with pytest.raises(OSError):
        service.execute_import(plan)
    monkeypatch.setattr(module, "copy_category_preset_image", original)
    assert service.execute_import(service.recompare(plan)).new_count == 1


def test_export_cleanup_exception_after_publish_still_returns_real_success(manager, tmp_path, monkeypatch):
    manager.add_character(name="已导出")
    service = CategoryPresetService(manager)
    plan = service.preview_export("game_a")

    def fail(*args):
        raise OSError("cleanup busy")

    monkeypatch.setattr(service, "discard", fail)
    result = service.execute_export(plan, tmp_path, "成功")
    assert read_category_preset(result.path).nodes[0].path == "已导出"


def test_export_picture_roundtrip_deduplicates_bytes_and_rejects_later_changes(manager, tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "green").save(image)
    first = manager.add_character(name="一")
    second = manager.add_character(name="二")
    assert manager.update_character_thumbnail(first.id, str(image))
    assert manager.update_character_thumbnail(second.id, str(image))
    service = CategoryPresetService(manager)
    plan = service.preview_export("game_a")
    assert plan.image_count == 1 and plan.image_bytes == len(image.read_bytes())
    result = service.execute_export(plan, tmp_path, "带图")
    document = read_category_preset(result.path)
    assert len(document.images) == 1 and len(document.nodes) == 2
    target = service.preview_import(result.path, "other")
    assert target.new_image_count == 2
    service.execute_import(target)
    plan = service.preview_export("game_a")
    Path(plan.image_sources[0][0]).write_bytes(b"changed")
    with pytest.raises(CategoryPresetError):
        service.execute_export(plan, tmp_path, "不发布")
    assert not (tmp_path / "不发布.d3dxcp").exists()


def test_staging_rejected_when_data_directory_becomes_a_deployment_root(manager, tmp_path):
    source = preset(tmp_path, [node("拒绝")])
    games = manager._game_service._store.get_games()
    games[0]["mods_path"] = str(manager._index._db_path.parent)
    manager._game_service._store.set_games(games)
    with pytest.raises(CategoryPresetError):
        CategoryPresetService(manager).preview_import(source, "game_a")


def test_unresolved_startup_recovery_is_reported_and_keeps_protection(manager, tmp_path, monkeypatch):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("外部")]), "game_a")

    def crash(*args):
        raise SimulatedCrash()

    monkeypatch.setattr(service, "_commit_import", crash)
    with pytest.raises(SimulatedCrash):
        service.execute_import(plan)
    marker = plan.target.root / "外部" / CATEGORY_MARKER_FILE
    marker.write_text("external")
    manager._index.close()
    ModManager._instance = None
    restarted = ModManager()
    assert str(marker) in restarted.category_preset_recovery_errors
    assert restarted.is_library_path_protected(marker)
    assert restarted._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 1
    restarted._index.close()


def test_collision_publication_does_not_replace_competing_process_file(manager, tmp_path, monkeypatch):
    manager.add_character(name="分类")
    import src.core.category_preset_service as module

    service = CategoryPresetService(manager)
    plan = service.preview_export("game_a")
    publish = module.rename_exclusive

    def compete(source, target):
        if target.name == "竞争.d3dxcp":
            target.write_bytes(b"other process")
        return publish(source, target)

    monkeypatch.setattr(module, "rename_exclusive", compete)
    result = service.execute_export(plan, tmp_path, "竞争")
    assert (tmp_path / "竞争.d3dxcp").read_bytes() == b"other process"
    assert result.path.name == "竞争 (1).d3dxcp"


@pytest.mark.parametrize("cancel", [False, True])
def test_live_import_completion_keeps_fixed_preview_for_repeat_or_cancel_retry(manager, tmp_path, cancel):
    from src.core.category_preset import CategoryPresetCancelled

    service = CategoryPresetService(manager)
    source = preset(tmp_path, [node("再次")])
    plan = service.preview_import(source, "game_a")
    stop = False

    def progress(phase, percent):
        nonlocal stop
        if cancel and phase == "导入分类":
            stop = True

    if cancel:
        with pytest.raises(CategoryPresetCancelled):
            service.execute_import(plan, cancelled=lambda: stop, progress=progress)
    else:
        service.execute_import(plan)
    source.unlink()
    again = service.recompare(plan)
    assert again.can_import == cancel
    assert again.new_count == (1 if cancel else 0)
    service.discard(again)
    assert not plan.staging.exists()


@pytest.mark.parametrize("side", ["input", "target", "output"])
def test_junction_ancestors_rejected_for_input_target_and_output(manager, tmp_path, side):
    import os

    real = tmp_path / "real"
    real.mkdir()
    source = preset(real, [node("分类")])
    linked = tmp_path / "linked"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(real), str(linked))
    else:
        linked.symlink_to(real, target_is_directory=True)
    service = CategoryPresetService(manager)
    if side == "input":
        with pytest.raises(CategoryPresetError, match="链接|联接"):
            service.preview_import(linked / source.name, "game_a")
    elif side == "target":
        root = manager._index.paths.roots["game_a"]
        root.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            _winapi.CreateJunction(str(real), str(root))
        else:
            root.symlink_to(real, target_is_directory=True)
        with pytest.raises(CategoryPresetError, match="链接|联接"):
            service.preview_import(source, "game_a")
    else:
        manager.add_character(name="分类")
        plan = service.preview_export("game_a")
        with pytest.raises(CategoryPresetError, match="链接|联接"):
            service.execute_export(plan, linked, "拒绝")


def test_commit_success_then_notification_and_cleanup_errors_report_real_success(manager, tmp_path, monkeypatch):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("已提交")]), "game_a")
    commit = service._commit_import

    def after_commit(*args):
        commit(*args)
        raise OSError("driver error after successful commit")

    monkeypatch.setattr(service, "_commit_import", after_commit)

    def progress(phase, percent):
        if phase == "已提交":
            raise RuntimeError("UI callback failed")

    result = service.execute_import(plan, progress=progress)
    assert result.new_count == 1
    assert len(snapshot(manager)) == 1


def test_committed_cleanup_pending_is_retained_until_recovery_and_discard_does_not_delete(
    manager, tmp_path, monkeypatch
):
    import src.core.category_preset_service as module

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("清理待重试")]), "game_a")
    original = module.remove_inventory
    monkeypatch.setattr(module, "remove_inventory", lambda *args: [str(plan.staging)])
    result = service.execute_import(plan)
    assert result.new_count == 1 and result.recovery_paths == (str(plan.staging),)
    service.discard(plan)
    assert plan.document.source_path.is_file()
    assert manager.is_library_path_protected(plan.target.root / "清理待重试")
    monkeypatch.setattr(module, "remove_inventory", original)
    assert service.recover_pending() == ()
    assert len(snapshot(manager)) == 1


def test_export_cancel_before_publish_leaves_no_half_file_and_after_publish_succeeds(manager, tmp_path):
    from src.core.category_preset import CategoryPresetCancelled

    manager.add_character(name="取消导出")
    service = CategoryPresetService(manager)
    plan = service.preview_export("game_a")
    stop = False

    def before(phase, percent):
        nonlocal stop
        if phase == "写入完成":
            stop = True

    with pytest.raises(CategoryPresetCancelled):
        service.execute_export(plan, tmp_path, "取消", cancelled=lambda: stop, progress=before)
    assert not list(tmp_path.glob("*.d3dxcp"))
    stop = False

    def after(phase, percent):
        nonlocal stop
        if phase == "已提交":
            stop = True

    result = service.execute_export(plan, tmp_path, "提交", cancelled=lambda: stop, progress=after)
    assert result.path.exists() and stop


def test_source_rejected_in_current_runtime_deployment_root(manager, tmp_path):
    deployed = tmp_path / "runtime-deployment"
    deployed.mkdir()
    manager._storage.set_migoto_mods_dir(str(deployed))
    source = preset(deployed, [node("不能纳管")])
    with pytest.raises(CategoryPresetError):
        CategoryPresetService(manager).preview_import(source, "game_a")


def test_preview_thumbnail_file_is_immutable_and_checked_before_recompare(manager, tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "red").save(image)
    import hashlib

    image_id = hashlib.sha256(image.read_bytes()).hexdigest()
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("图片", image_id=image_id)], {image_id: image}), "game_a")
    from pathlib import Path

    Path(plan.rows[0].image_path).write_bytes(b"external modification")
    with pytest.raises(CategoryPresetError):
        service.recompare(plan)


@pytest.mark.parametrize("scope", ["existing_root", "new_root", "existing_parent"])
def test_physical_directory_replacement_during_publication_blocks_commit_and_keeps_evidence(manager, tmp_path, scope):
    service = CategoryPresetService(manager)
    if scope == "existing_parent":
        manager.add_character(name="Parent", node_type="category")
        source = preset(tmp_path, [node("Parent"), node("Parent/A"), node("Parent/B")])
    else:
        source = preset(tmp_path, [node("A"), node("B")])
    if scope == "existing_root":
        manager._get_library_root("game_a")
    plan = service.preview_import(source, "game_a")
    before = snapshot(manager)
    replaced = plan.target.root / "Parent" if scope == "existing_parent" else plan.target.root
    displaced = replaced.with_name("moved-" + replaced.name)
    changed = False

    def progress(phase, percent):
        nonlocal changed
        if phase == "导入分类" and not changed:
            replaced.rename(displaced)
            replaced.mkdir()
            changed = True

    with pytest.raises(CategoryPresetError, match="待恢复|发生变化"):
        service.execute_import(plan, progress=progress)
    assert snapshot(manager) == before
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 1
    assert displaced.exists()
    service.discard(plan)
    assert plan.staging.exists()
    assert manager.is_library_path_protected(replaced)
    assert manager.is_library_path_protected(displaced)
    assert service.recover_pending()
    replaced.rmdir()
    displaced.rename(replaced)
    assert service.recover_pending() == ()
    assert snapshot(manager) == before
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0


def test_last_publication_callback_cannot_change_marker_before_database_commit(manager, tmp_path):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A")]), "game_a")
    marker = plan.target.root / "A" / CATEGORY_MARKER_FILE

    def progress(phase, percent):
        if phase == "导入分类":
            marker.write_text("external modification")

    with pytest.raises(CategoryPresetError):
        service.execute_import(plan, progress=progress)
    assert snapshot(manager) == []
    assert marker.read_text() == "external modification"
    assert manager.is_library_path_protected(marker)


def test_cancel_before_journal_does_not_adopt_external_file_in_operation_staging(manager, tmp_path):
    from pathlib import Path

    from src.core.category_preset import CategoryPresetCancelled

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A")]), "game_a")
    external = None

    def cancelled():
        nonlocal external
        folders = list(plan.staging.glob("publication-*"))
        if folders:
            external = Path(folders[0]) / "external-user-file.txt"
            external.write_text("external user data")
            return True
        return False

    with pytest.raises((CategoryPresetCancelled, CategoryPresetError)):
        service.execute_import(plan, cancelled=cancelled)
    assert external is not None and external.read_text() == "external user data"
    with pytest.raises(CategoryPresetError, match="暂存|清理"):
        service.discard(plan)
    assert external.read_text() == "external user data"
    assert snapshot(manager) == []


@pytest.mark.parametrize("kind", ["import", "export"])
def test_preview_failure_does_not_adopt_external_staging_file(manager, tmp_path, monkeypatch, kind):
    service = CategoryPresetService(manager)
    manager.add_character(name="A")
    external = None
    original = service._new_stage

    def create_stage():
        nonlocal external
        stage = original()
        external = stage / "external-user-file.txt"
        external.write_text("external user data")
        return stage

    monkeypatch.setattr(service, "_new_stage", create_stage)

    def fail(phase, percent):
        if external is not None:
            raise RuntimeError("preview failed")

    with pytest.raises((RuntimeError, CategoryPresetError)):
        if kind == "import":
            service.preview_import(preset(tmp_path, [node("A")]), "game_a", progress=fail)
        else:
            service.preview_export("game_a", progress=fail)
    assert external.read_text() == "external user data"


def test_discard_reports_changed_preview_file_and_retains_evidence(manager, tmp_path):
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A")]), "game_a")
    plan.document.source_path.write_bytes(b"external modification")
    with pytest.raises(CategoryPresetError, match="input.d3dxcp"):
        service.discard(plan)
    assert plan.document.source_path.read_bytes() == b"external modification"


def test_cancel_during_final_published_receipt_verification_stops_before_commit(manager, tmp_path, monkeypatch):
    import src.core.category_preset_service as module
    from src.core.category_preset import CategoryPresetCancelled

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A")]), "game_a")
    check = module.published_errors
    stop = False

    def verify(*args, **kwargs):
        nonlocal stop
        result = check(*args, **kwargs)
        stop = True
        return result

    monkeypatch.setattr(module, "published_errors", verify)
    with pytest.raises(CategoryPresetCancelled):
        service.execute_import(plan, cancelled=lambda: stop)
    assert snapshot(manager) == []


def test_rolled_back_import_can_retry_staging_cleanup_without_false_displacement(manager, tmp_path, monkeypatch):
    import src.core.category_preset_service as module

    manager._get_library_root("game_a")
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A")]), "game_a")
    original = module.remove_inventory
    monkeypatch.setattr(module, "remove_inventory", lambda *args: [str(plan.staging)])
    stop = False

    def progress(phase, percent):
        nonlocal stop
        if phase == "导入分类":
            stop = True

    with pytest.raises(CategoryPresetError, match="待恢复"):
        service.execute_import(plan, cancelled=lambda: stop, progress=progress)
    assert snapshot(manager) == []
    assert not (plan.target.root / "A").exists()
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 1
    persisted = manager._index._db().execute("SELECT plan_json FROM category_import_journal").fetchone()[0]
    assert json.loads(persisted)["rollback_done"] is True
    monkeypatch.setattr(module, "remove_inventory", original)
    assert service.recover_pending() == ()
    assert service.recover_pending() == ()
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0
    assert manager._library_protection.snapshot() == (set(), set())


def test_crash_midway_through_multi_tree_rollback_is_resumable(manager, tmp_path, monkeypatch):
    import src.core.category_preset_journal as journal_module

    manager._get_library_root("game_a")
    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A"), node("B")]), "game_a")

    def fail_commit(*args):
        raise OSError("database commit failed")

    monkeypatch.setattr(service, "_commit_import", fail_commit)
    remove = journal_module.remove_inventory
    rename = journal_module.rename_exclusive
    interrupted = False

    def crash_after_removal(root, entries):
        nonlocal interrupted
        result = remove(root, entries)
        if str(root).startswith(str(plan.target.root)) and not interrupted:
            interrupted = True
            raise SimulatedCrash()
        return result

    def crash_after_inverse_rename(source, target):
        nonlocal interrupted
        result = rename(source, target)
        if str(source).startswith(str(plan.target.root)) and not interrupted:
            interrupted = True
            raise SimulatedCrash()
        return result

    monkeypatch.setattr(journal_module, "remove_inventory", crash_after_removal)
    monkeypatch.setattr(journal_module, "rename_exclusive", crash_after_inverse_rename)
    with pytest.raises(SimulatedCrash):
        service.execute_import(plan)
    assert interrupted
    monkeypatch.setattr(journal_module, "remove_inventory", remove)
    monkeypatch.setattr(journal_module, "rename_exclusive", rename)
    assert service.recover_pending() == ()
    assert snapshot(manager) == []
    assert not (plan.target.root / "A").exists() and not (plan.target.root / "B").exists()
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0


def test_crash_during_post_rollback_staging_cleanup_resumes_without_touching_library(manager, tmp_path, monkeypatch):
    import src.core.category_preset_service as module

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node("A"), node("B")]), "game_a")
    original = module.remove_inventory

    def fail_commit(*args):
        raise OSError("commit failed")

    monkeypatch.setattr(service, "_commit_import", fail_commit)

    def crash_cleanup(root, entries):
        original(root, entries)
        raise SimulatedCrash()

    monkeypatch.setattr(module, "remove_inventory", crash_cleanup)
    with pytest.raises(SimulatedCrash):
        service.execute_import(plan)
    assert not (plan.target.root / "A").exists() and not (plan.target.root / "B").exists()
    monkeypatch.setattr(module, "remove_inventory", original)
    assert service.recover_pending() == ()
    assert snapshot(manager) == []
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0


@pytest.mark.parametrize("cancel_preparation", [False, True])
def test_directory_preparation_reports_throttled_real_progress_and_can_cancel(manager, tmp_path, cancel_preparation):
    from src.core.category_preset import CategoryPresetCancelled

    service = CategoryPresetService(manager)
    plan = service.preview_import(preset(tmp_path, [node(f"N{index:03}") for index in range(200)]), "game_a")
    percentages = []
    stop = False

    def progress(phase, percent):
        nonlocal stop
        if phase != "准备目录":
            return
        percentages.append(percent)
        # 准备结果对应真实暂存文件，此时尚未发布类别。
        assert not plan.target.root.exists()
        if 0 < percent < 100:
            markers = list(plan.staging.glob("publication-*/**/" + CATEGORY_MARKER_FILE))
            assert len(markers) >= percent * 200 // 100
        if cancel_preparation and percent >= 10:
            stop = True

    if cancel_preparation:
        with pytest.raises(CategoryPresetCancelled):
            service.execute_import(plan, cancelled=lambda: stop, progress=progress)
        assert snapshot(manager) == []
        assert service.recompare(plan).can_import
    else:
        assert service.execute_import(plan, progress=progress).new_count == 200
        assert percentages[-1] == 100
    assert percentages[0] == 0
    assert percentages == sorted(set(percentages))
    assert 2 <= len(percentages) <= 101
    assert manager._index._db().execute("SELECT count(*) FROM category_import_journal").fetchone()[0] == 0
    service.discard(plan)
