import json
import os
import shutil
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import mm as mm


def imported(mm, tmp_path, *, archive_format="zip", duplicate=False):
    node = mm.add_character("Previews")
    archive = tmp_path / "previews.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Skin/mod.ini", "[Constants]\nx=1\n")
        output.writestr("Skin/a/preview.png", b"first image")
        output.writestr("Skin/b/preview.png", b"second image")
        output.writestr("Skin/unselected.png", b"third image")
        if duplicate:
            output.writestr("Skin/duplicate.bin", b"first unrelated content")
            with pytest.warns(UserWarning, match="Duplicate name"):
                output.writestr("Skin/duplicate.bin", b"second unrelated content")
    mod = mm.import_mod(str(archive), node_id=node.id, import_format=archive_format, strict_format=True)
    assert mod and len(mod.preview_images) == 3
    return mm.get_mod(mod.mod_id)


def delete(mm, mid, paths):
    from src.core.preview_operations import delete_preview_files

    return delete_preview_files(mm, mid, paths)


def snapshots(mm, mod):
    container = Path(mod.path).parent
    cache = Path(mm._index.paths.preview_roots[mod.game_id]) / mod.mod_id
    files = {str(path): path.read_bytes() for root in (container, cache) for path in root.rglob("*") if path.is_file()}
    return files, mm.get_mod(mod.mod_id).to_dict()


def test_missing_source_blocks_preview_deletion_and_reports_failure(mm, tmp_path):
    mod = imported(mm, tmp_path)
    Path(mod.path).unlink()
    before = snapshots(mm, mod)
    events = []
    mm.add_event_listener(lambda event, **payload: events.append((event, payload)))
    assert not delete(mm, mod.mod_id, mod.preview_images)
    assert snapshots(mm, mod) == before
    assert any(event == "operation_failed" for event, _ in events)


def test_preview_removal_keeps_zip_members_and_source_identity(mm, tmp_path):
    from src.core.operation_preflight import confirm_source
    from src.models.mod import Mod

    mod = imported(mm, tmp_path)
    original_bytes = Path(mod.path).read_bytes()
    selected = mod.preview_images[:2]
    assert delete(mm, mod.mod_id, selected)
    current = mm.get_mod(mod.mod_id)
    assert current.mod_id == mod.mod_id and current.imported_at == mod.imported_at
    assert current.preview_images == mod.preview_images[2:]
    assert all(not Path(path).exists() for path in selected)
    cache = Path(mm._index.paths.preview_roots[mod.game_id]) / mod.mod_id
    with zipfile.ZipFile(mod.path) as source:
        for path in selected:
            assert Path(path).relative_to(cache).as_posix() in source.namelist()
        assert Path(current.preview_images[0]).relative_to(cache).as_posix() in source.namelist()
    assert current.content_hash == Mod.compute_sha1(mod.path) == mod.content_hash
    assert Path(mod.path).read_bytes() == original_bytes
    assert confirm_source(current.path, current.mod_id, current.content_hash)
    info = json.loads((Path(mod.path).parent / ".d3dxmeta/info.json").read_text(encoding="utf8"))
    assert len(info["local"]["preview_references"]) == 1


def test_unmatched_cache_bytes_delete_only_selected_local_preview(mm, tmp_path):
    mod = imported(mm, tmp_path)
    selected = Path(mod.preview_images[0])
    selected.write_bytes(b"local replacement, not the archive member")
    source = Path(mod.path).read_bytes()
    marker = (Path(mod.path).parent / ".d3dxmod").read_bytes()
    assert delete(mm, mod.mod_id, [str(selected)])
    assert not selected.exists()
    assert Path(mod.path).read_bytes() == source
    assert (Path(mod.path).parent / ".d3dxmod").read_bytes() == marker
    assert mm.get_mod(mod.mod_id).content_hash == mod.content_hash


def test_container_manual_preview_does_not_guess_zip_member_by_filename(mm, tmp_path):
    mod = imported(mm, tmp_path)
    local = Path(mod.path).parent / ".d3dxmeta/previews/preview.png"
    local.parent.mkdir()
    local.write_bytes(b"first image")
    mod.preview_images.append(str(local))
    assert mm.update_mod(mod)
    source = Path(mod.path).read_bytes()
    assert delete(mm, mod.mod_id, [str(local)])
    assert local.read_bytes() == b"first image"
    assert Path(mod.path).read_bytes() == source


def test_zip_with_duplicate_members_keeps_archive_unchanged(mm, tmp_path):
    mod = imported(mm, tmp_path, duplicate=True)
    source = Path(mod.path).read_bytes()
    assert delete(mm, mod.mod_id, mod.preview_images[:1])
    assert Path(mod.path).read_bytes() == source
    assert not Path(mod.preview_images[0]).exists()


def test_7z_preview_deletion_keeps_archive_and_identity_unchanged(mm, tmp_path):
    mod = imported(mm, tmp_path, archive_format="7z")
    assert Path(mod.path).suffix == ".7z"
    source = Path(mod.path).read_bytes()
    marker = (Path(mod.path).parent / ".d3dxmod").read_bytes()
    assert delete(mm, mod.mod_id, mod.preview_images[:1])
    assert Path(mod.path).read_bytes() == source
    assert (Path(mod.path).parent / ".d3dxmod").read_bytes() == marker
    assert mm.get_mod(mod.mod_id).content_hash == mod.content_hash
    assert not Path(mod.preview_images[0]).exists()


@pytest.mark.parametrize("archive_format", ["7z", "zip"])
@pytest.mark.parametrize("delete_all", [False, True])
def test_removed_archive_previews_stay_hidden_until_rebuild(
    mm,
    tmp_path,
    archive_format,
    delete_all,
):
    from src.ui.library_metadata import capture_request, publish_metadata, read_metadata

    # 重复 ZIP 成员与其他归档一样，只删除本地预览引用。
    mod = imported(mm, tmp_path, archive_format=archive_format, duplicate=archive_format == "zip")
    source = Path(mod.path).read_bytes()
    batches = [mod.preview_images] if delete_all else [[path] for path in mod.preview_images]
    remaining = list(mod.preview_images)
    for batch in batches:
        assert delete(mm, mod.mod_id, batch)
        remaining = [path for path in remaining if path not in batch]
        publish_metadata(mm, read_metadata(capture_request(mm, mod.mod_id), mm._find_previews))
        assert mm.get_mod(mod.mod_id).preview_images == remaining
        assert all(not Path(path).exists() for path in mod.preview_images if path not in remaining)

    # 元数据保存保留选择，显式重建重新发现包内图片。
    current = mm.get_mod(mod.mod_id)
    current.info.description = "Edited after deleting previews"
    assert mm.update_mod(current)
    mm.rebuild_index_from_filesystem(mod.game_id)
    assert len(mm.get_mod(mod.mod_id).preview_images) == 3
    assert Path(mod.path).read_bytes() == source


@pytest.mark.parametrize("reuse_cache", [False, True])
def test_missing_cache_does_not_rediscover_removed_previews(mm, tmp_path, reuse_cache):
    from src.core.preview_cache_index import build_preview_cache_index

    mod = imported(mm, tmp_path, archive_format="7z")
    deleted, *remaining = mod.preview_images
    cache = Path(mm._index.paths.preview_roots[mod.game_id]) / mod.mod_id
    if reuse_cache:
        shutil.copytree(cache, cache.with_name("other-instance"))
    assert delete(mm, mod.mod_id, [deleted])
    for path in remaining:
        Path(path).unlink()

    found = mm._find_previews(mod.path, mod_id=mod.mod_id, cache_index=build_preview_cache_index(cache.parent))
    assert found == []
    assert not Path(deleted).exists()
    # 陈旧缓存不得使已删除预览重新出现。
    Path(deleted).parent.mkdir(parents=True, exist_ok=True)
    Path(deleted).write_bytes(b"stale cached image")
    assert mm._find_previews(mod.path, mod_id=mod.mod_id) == found


@pytest.mark.parametrize("invalid", ["foreign", "unlisted", "missing"])
def test_entire_batch_is_validated_before_any_preview_is_deleted(mm, tmp_path, invalid):
    mod = imported(mm, tmp_path)
    if invalid == "foreign":
        bad = mm._index.paths.node_path(mod.node_id) / "node-preview.png"
        bad.write_bytes(b"belongs to node")
        mod.preview_images.append(str(bad))
        assert mm.update_mod(mod)
    elif invalid == "unlisted":
        bad = Path(mod.path).parent / "unlisted.png"
        bad.write_bytes(b"not selected by this mod")
    else:
        bad = Path(mod.preview_images[-1])
        bad.unlink()
    before = snapshots(mm, mod)
    assert not delete(mm, mod.mod_id, [mod.preview_images[0], str(bad)])
    assert snapshots(mm, mod) == before


@pytest.mark.parametrize("where", ["cache", "metadata", "database"])
def test_failed_preview_transaction_restores_files_marker_metadata_and_record(mm, tmp_path, monkeypatch, where):
    mod = imported(mm, tmp_path)
    before = snapshots(mm, mod)
    if where == "database":
        monkeypatch.setattr(mm._index, "update_mod", lambda *args, **kwargs: False)
    else:
        metadata = Path(mod.path).parent / ".d3dxmeta/info.json"
        replace = os.replace

        def fail_metadata_install(source, target):
            """分别模拟缓存移动失败或元数据安装失败。"""
            from src.utils import fs

            if where == "cache" and Path(fs.normal_path(source)) == Path(mod.preview_images[1]):
                raise PermissionError("cache move denied")
            if (
                where == "metadata"
                and Path(target).name == metadata.name
                and Path(source).name.startswith(".preview-edit-")
            ):
                raise PermissionError("metadata install denied")
            return replace(source, target)

        monkeypatch.setattr("src.core.preview_operations.os.replace", fail_metadata_install)
    assert not delete(mm, mod.mod_id, mod.preview_images)
    assert snapshots(mm, mod) == before


def test_source_replaced_during_metadata_staging_is_preserved_and_operation_fails(mm, tmp_path, monkeypatch):
    from src.core import preview_operations

    mod = imported(mm, tmp_path)
    before_images = {p: Path(p).read_bytes() for p in mod.preview_images}
    original = preview_operations._stage

    def replace_source(*args, **kwargs):
        """在暂存元数据后模拟外部替换本体，验证事务不会覆盖该外部操作。"""
        result = original(*args, **kwargs)
        Path(mod.path).write_bytes(b"external replacement")
        return result

    monkeypatch.setattr(preview_operations, "_stage", replace_source)
    assert not delete(mm, mod.mod_id, mod.preview_images)
    assert Path(mod.path).read_bytes() == b"external replacement"
    assert {p: Path(p).read_bytes() for p in mod.preview_images} == before_images
    assert mm.get_mod(mod.mod_id).content_hash == mod.content_hash


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing lock contract")
def test_source_cannot_be_replaced_between_record_update_and_sql_commit(mm, tmp_path, monkeypatch):
    mod = imported(mm, tmp_path)
    replacement = tmp_path / "external.zip"
    replacement.write_bytes(b"external replacement")
    transaction = mm._index.transaction
    attempted = []

    @contextmanager
    def commit_with_external_replacement():
        with transaction() as connection:
            yield connection
            try:
                os.replace(replacement, mod.path)
            except PermissionError:
                attempted.append("blocked")
            else:
                attempted.append("replaced")

    monkeypatch.setattr(mm._index, "transaction", commit_with_external_replacement)
    assert delete(mm, mod.mod_id, mod.preview_images[:1])
    assert attempted == ["blocked"]
    assert replacement.read_bytes() == b"external replacement"
    from src.core.operation_preflight import confirm_source

    current = mm.get_mod(mod.mod_id)
    assert confirm_source(current.path, current.mod_id, current.content_hash)


def test_local_only_deletion_rejects_replacement_with_identical_source_bytes(mm, tmp_path, monkeypatch):
    import src.core.preview_operations as operations

    mod = imported(mm, tmp_path)
    Path(mod.preview_images[0]).write_bytes(b"local replacement")
    before = snapshots(mm, mod)
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(Path(mod.path).read_bytes())
    apply = operations._Change.apply

    def replace_during_metadata_install(change, directories):
        apply(change, directories)
        if change.path.name == "info.json":
            os.replace(replacement, mod.path)

    monkeypatch.setattr(operations._Change, "apply", replace_during_metadata_install)
    assert not delete(mm, mod.mod_id, mod.preview_images[:1])
    assert snapshots(mm, mod) == before


def test_source_checks_run_in_worker_and_database_publication_stays_on_owner(mm, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from src.models.mod import Mod

    mod = imported(mm, tmp_path)
    owner = threading.get_ident()
    compute = Mod.compute_sha1
    update = mm._index.update_mod

    def hash_in_worker(source):
        assert threading.get_ident() != owner
        return compute(source)

    def update_on_owner(*args, **kwargs):
        assert threading.get_ident() == owner
        return update(*args, **kwargs)

    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(hash_in_worker))
    monkeypatch.setattr(mm._index, "update_mod", update_on_owner)
    with ThreadPoolExecutor(max_workers=1) as executor:
        mm.operation_check_runner = lambda work: executor.submit(work).result()
        assert delete(mm, mod.mod_id, mod.preview_images[:1])
    assert not Path(mod.preview_images[0]).exists()
    assert mm.get_mod(mod.mod_id).preview_images == mod.preview_images[1:]


@pytest.mark.parametrize("rebuild_result", ["cancel", "commit", "fail"])
def test_removed_selection_survives_restart_and_only_committed_rebuild_restores_it(
    mm, tmp_path, monkeypatch, rebuild_result
):
    """六张图片覆盖候选上限，重启和刷新不能从未显示的资源中补入新预览。"""
    from src.core.mod_manager import ModManager
    from src.core.rebuild_session import RebuildSession
    from src.ui.library_metadata import capture_request, publish_metadata, read_metadata

    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("mod.ini", "[Constants]\nx=2\n")
        for number in range(6):
            output.writestr(f"{number}.png", f"image {number}")
    mod = mm.import_mod(str(archive), node_id=mm.add_character("Selection").id)
    before = Path(mod.path).read_bytes()
    assert len(mod.preview_images) == 5
    assert delete(mm, mod.mod_id, mod.preview_images)
    restarted = ModManager()
    restarted.set_game(mod.game_id)
    try:
        publish_metadata(restarted, read_metadata(capture_request(restarted, mod.mod_id), restarted._find_previews))
        assert restarted.get_mod(mod.mod_id).preview_images == []
        session = RebuildSession(restarted, mod.game_id)
        try:
            session.prepare()
            while not session.stage_batch():
                pass
            session.validate()
            if rebuild_result == "commit":
                session.commit()
            elif rebuild_result == "fail":
                transaction = restarted._index.transaction

                @contextmanager
                def fail_commit():
                    """在元数据写入后模拟索引提交失败，检查选择记录回滚。"""
                    with transaction():
                        yield
                        raise OSError("commit failed")

                with monkeypatch.context() as patch:
                    patch.setattr(restarted._index, "transaction", fail_commit)
                    with pytest.raises(OSError, match="commit failed"):
                        session.commit()
        finally:
            session.close()
        publish_metadata(restarted, read_metadata(capture_request(restarted, mod.mod_id), restarted._find_previews))
        assert len(restarted.get_mod(mod.mod_id).preview_images) == (5 if rebuild_result == "commit" else 0)
        assert Path(mod.path).read_bytes() == before
    finally:
        restarted.stop_ini_sync()
        restarted._index.close()
