from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import new_mod_id, read_source_mod_id
from src.core.mod_manager import ModManager
from src.core.mod_package import write_container_marker
from src.models.mod import Mod
from tests.helpers import registered_games as registered_games


@pytest.fixture
def manager(temp_data_dir):
    manager = ModManager()
    manager.set_game("game_a")
    return manager


def archive_at(root, name="body", payload=b"current", *, images=True):
    container = root / name
    container.mkdir(parents=True, exist_ok=True)
    archive = container / "body.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("Top/mod.ini", "[Constants]\nx=1\n")
        if images:
            stream.writestr("Top/nested/preview.png", payload)
    digest = Mod.compute_sha1(str(archive))
    assert write_container_marker(container, archive.name, digest, imported_at="2026-01-01T00:00:00")
    return archive, digest


def cache_at(root, mod_id, digest, payload=b"current"):
    cache = root / mod_id
    image = cache / "Top/nested/preview.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(payload)
    (cache / ".content-fingerprint.json").write_text(json.dumps({"content_hash": digest}), encoding="utf-8")
    return cache


def cache_root(manager, temp_data_dir):
    return temp_data_dir / "data" / manager._res_dir() / "preview_cache"


@pytest.mark.parametrize("explicit", [False, True])
def test_replaced_archive_during_extraction_is_not_published(manager, temp_data_dir, monkeypatch, explicit):
    from src.core.preview_cache_index import capture_source_version

    archive, digest = archive_at(temp_data_dir / "library", payload=b"old-preview")
    evidence = {"content_hash": digest, "source_version": capture_source_version(archive)} if explicit else {}
    replacement, _ = archive_at(temp_data_dir / "replacement", payload=b"new-preview")
    original_bytes = archive.read_bytes()
    mod_id = new_mod_id(digest)
    extract = manager._archive.extract_files

    def replace_and_extract(path, *args, **kwargs):
        os.replace(replacement, archive)
        return extract(path, *args, **kwargs)

    monkeypatch.setattr(manager._archive, "extract_files", replace_and_extract)
    manager._find_previews(str(archive), mod_id=mod_id, **evidence)
    assert not (cache_root(manager, temp_data_dir) / mod_id).exists()
    archive.write_bytes(original_bytes)
    monkeypatch.setattr(manager._archive, "extract_files", extract)
    found = manager._find_previews(str(archive), mod_id=new_mod_id(digest))
    assert [Path(path).read_bytes() for path in found] == [b"old-preview"]


def test_explicit_fingerprint_evidence_rejects_replacement_before_preview_lookup(manager, temp_data_dir):
    from src.core.preview_cache_index import capture_source_version

    archive, digest = archive_at(temp_data_dir / "library", payload=b"old-preview")
    version = capture_source_version(archive)
    replacement, _ = archive_at(temp_data_dir / "replacement", payload=b"new-preview")
    os.replace(replacement, archive)
    assert (
        manager._find_previews(str(archive), mod_id=new_mod_id(digest), content_hash=digest, source_version=version)
        == []
    )


@pytest.mark.parametrize("folder", [False, True])
def test_intake_rejects_source_changed_after_hash_before_preview_staging(manager, temp_data_dir, monkeypatch, folder):
    from src.core import mod_intake

    character = manager.add_character(name="Race", node_type="character")
    if folder:
        source = temp_data_dir / "incoming"
        source.mkdir()
        (source / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
        (source / "preview.png").write_bytes(b"old-preview")
    else:
        source, _ = archive_at(temp_data_dir / "incoming", payload=b"old-preview")
    replacement, _ = archive_at(temp_data_dir / "replacement", payload=b"new-preview")
    stage = mod_intake.stage_import_metadata

    def changed(prepared, *args, **kwargs):
        if prepared.clean_path.is_dir():
            (prepared.clean_path / "preview.png").write_bytes(b"new-preview")
        else:
            os.replace(replacement, prepared.clean_path)
        return stage(prepared, *args, **kwargs)

    monkeypatch.setattr(mod_intake, "stage_import_metadata", changed)
    service = mod_intake.ModIntakeService(manager)
    request = mod_intake.IntakeRequest(source_path=source, game_id="game_a", node_id=character.id)
    with pytest.raises(mod_intake.IntakeError):
        service.prepare(request)
    assert manager._index.get_all_mods("game_a") == []


@pytest.mark.parametrize("warm", [False, True])
def test_rebuild_enumerates_candidate_root_once(manager, temp_data_dir, monkeypatch, warm):
    root = cache_root(manager, temp_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    descriptors = []
    for number in range(30):
        payload = f"preview-{number}".encode()
        archive, digest = archive_at(temp_data_dir / "library", str(number), payload)
        descriptors.append(
            {"path": str(archive), "mod_id": read_source_mod_id(archive), "imported_at": "2026-01-01T00:00:00"}
        )
        if warm:
            cache_at(root, read_source_mod_id(archive), digest, payload)
    scans = []
    extracts = []
    real_scan = os.scandir
    real_extract = manager._archive.extract_files

    def scan(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(root.absolute())):
            scans.append(path)
        return real_scan(path)

    def extract(*args, **kwargs):
        extracts.append(args[0])
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(manager._archive, "extract_files", extract)
    items = manager._hash_rebuild_descriptors(descriptors)
    assert len(items) == 30
    assert all(Path(item["preview_images"][0]).read_bytes() == f"preview-{n}".encode() for n, item in enumerate(items))
    assert len(scans) <= 1
    assert len(extracts) == (0 if warm else 30)


def test_complete_cache_is_reused_with_verified_fingerprint(manager, temp_data_dir, monkeypatch):
    archive, digest = archive_at(temp_data_dir / "library")
    mod_id = new_mod_id(digest)
    cache_at(cache_root(manager, temp_data_dir), mod_id, digest)
    monkeypatch.setattr(manager._archive, "extract_files", lambda *a, **k: pytest.fail("hot cache extracted"))
    found = manager._find_previews(str(archive), mod_id=mod_id)
    assert [Path(path).read_bytes() for path in found] == [b"current"]
    assert manager._find_previews(str(archive), mod_id=mod_id) == found


def test_stable_identity_does_not_reuse_preview_after_body_changes(manager, temp_data_dir):
    archive, digest = archive_at(temp_data_dir / "library", payload=b"old")
    mod_id = new_mod_id()
    cache_at(cache_root(manager, temp_data_dir), mod_id, digest, payload=b"old")
    replacement, _ = archive_at(temp_data_dir / "replacement", payload=b"new")
    os.replace(replacement, archive)
    found = manager._find_previews(str(archive), mod_id=mod_id)
    assert [Path(path).read_bytes() for path in found] == [b"new"]


def test_snapshot_is_immutable_sorted_and_does_not_rescan(manager, temp_data_dir, monkeypatch):
    from src.core.preview_cache_index import build_preview_cache_index

    archive, digest = archive_at(temp_data_dir / "library")
    root = cache_root(manager, temp_data_dir)
    later = cache_at(root, digest[:16] + "bb", digest)
    first = cache_at(root, digest[:16] + "aa", digest)
    snapshot = build_preview_cache_index(root)
    assert snapshot[digest] == (first, later)
    with pytest.raises(TypeError):
        snapshot[digest] = ()
    # 不可变快照中的失效候选不应阻止继续尝试后续候选。
    (first / "Top/nested/preview.png").unlink()
    real_scandir = os.scandir

    def scan(path):
        if Path(path) == root:
            pytest.fail("snapshot lookup rescanned the root")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(manager._archive, "extract_files", lambda *a, **k: pytest.fail("valid successor missed"))
    found = manager._find_previews(str(archive), mod_id=new_mod_id(digest), content_hash=digest, cache_index=snapshot)
    assert [Path(path).read_bytes() for path in found] == [b"current"]
    assert not Path(found[0]).is_relative_to(later)


def test_partial_copy_cannot_contaminate_next_candidate(manager, temp_data_dir, monkeypatch):
    import shutil

    archive, digest = archive_at(temp_data_dir / "library")
    root = cache_root(manager, temp_data_dir)
    first = cache_at(root, digest[:16] + "aa", digest)
    cache_at(root, digest[:16] + "bb", digest)
    real_copy = shutil.copy2

    def copy(source, target, *args, **kwargs):
        if "aa" == Path(source).parents[2].name[-2:]:
            Path(target).write_bytes(b"partial")
            raise OSError("copy interrupted")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", copy)
    monkeypatch.setattr(manager._archive, "extract_files", lambda *a, **k: pytest.fail("successor must be copied"))
    found = manager._find_previews(
        str(archive),
        mod_id=new_mod_id(digest),
        cache_index=__import__(
            "src.core.preview_cache_index", fromlist=["build_preview_cache_index"]
        ).build_preview_cache_index(root),
    )
    assert [Path(path).read_bytes() for path in found] == [b"current"]
    assert first.exists()


def test_extraction_failure_does_not_publish_partial_images(manager, temp_data_dir, monkeypatch):
    from src.core.preview_cache_index import read_cached_previews

    archive, digest = archive_at(temp_data_dir / "library")
    mod_id = new_mod_id(digest)
    own = cache_root(manager, temp_data_dir) / mod_id

    def partial(path, names, stage):
        (stage / "preview.png").write_bytes(b"partial")
        return []

    monkeypatch.setattr(manager._archive, "extract_files", partial)
    assert manager._find_previews(str(archive), mod_id=mod_id) == []
    assert not own.exists()
    assert read_cached_previews(own, {".png"}) == []


def test_cancelled_extraction_propagates_without_published_cache(manager, temp_data_dir, monkeypatch):
    archive, digest = archive_at(temp_data_dir / "library")
    mod_id = new_mod_id(digest)

    def cancel(*args):
        raise InterruptedError("cancelled")

    monkeypatch.setattr(manager._archive, "extract_files", cancel)
    with pytest.raises(InterruptedError):
        manager._find_previews(str(archive), mod_id=mod_id)
    assert not (cache_root(manager, temp_data_dir) / mod_id).exists()


def test_archive_without_images_has_no_extraction(manager, temp_data_dir, monkeypatch):
    archive, digest = archive_at(temp_data_dir / "library", images=False)
    monkeypatch.setattr(manager._archive, "extract_files", lambda *a, **k: pytest.fail("no images to extract"))
    assert manager._find_previews(str(archive), mod_id=new_mod_id(digest)) == []


@pytest.mark.parametrize("partial", [False, True])
def test_staged_preview_publication_requires_complete_extraction(manager, temp_data_dir, monkeypatch, partial):
    from src.core.import_format import (
        PreparedImportSource,
        materialize_staged_previews,
        stage_import_metadata,
    )
    from src.core.preview_cache_index import capture_source_version, read_cached_previews

    archive, digest = archive_at(temp_data_dir / "library")
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr("Top/second.png", b"second")
    digest = Mod.compute_sha1(str(archive))
    prepared = PreparedImportSource(
        archive, "zip", "zip", passthrough_archive=True, content_version=capture_source_version(archive)
    )
    real_extract = manager._archive.extract_files
    if partial:
        monkeypatch.setattr(
            manager._archive, "extract_files", lambda path, names, target: real_extract(path, names[:1], target)
        )
    assert stage_import_metadata(prepared, manager._archive, temp_data_dir / "staging")
    own = cache_root(manager, temp_data_dir) / new_mod_id(digest)
    previews = materialize_staged_previews(prepared, archive, cache_dir=own)
    assert len(previews) == (0 if partial else 2)
    trusted = read_cached_previews(own, {".png"})
    assert len(trusted) == (0 if partial else 2)


def test_same_content_instances_copy_to_distinct_protected_caches(manager, temp_data_dir, monkeypatch):
    descriptors = []
    for ordinal in range(12):
        archive, digest = archive_at(temp_data_dir / "library", str(ordinal))
        descriptors.append(
            {"path": str(archive), "mod_id": read_source_mod_id(archive), "imported_at": "2026-01-01T00:00:00"}
        )
    root = cache_root(manager, temp_data_dir)
    old_cache = cache_at(root, new_mod_id(digest), digest)
    monkeypatch.setattr(manager._archive, "extract_files", lambda *a, **k: pytest.fail("trusted cache missed"))
    items = manager._hash_rebuild_descriptors(descriptors)
    assert len({item["new_id"] for item in items}) == 12
    assert len({item["content_hash"] for item in items}) == 1
    previews = [Path(item["preview_images"][0]) for item in items]
    assert len(set(previews)) == 12
    assert {item["new_id"] for item in items} <= manager.inflight_mod_ids()
    manager._cleanup_orphaned_managed_artifacts("game_a")
    assert not old_cache.exists()
    assert all(path.read_bytes() == b"current" for path in previews)
    manager._release_rebuild_inflight_ids()
    manager._cleanup_orphaned_managed_artifacts("game_a")
    assert not any(path.exists() for path in previews)


def test_unreadable_exact_candidate_does_not_hide_valid_successor(manager, temp_data_dir, monkeypatch):
    archive, digest = archive_at(temp_data_dir / "library")
    root = cache_root(manager, temp_data_dir)
    own_id = new_mod_id(digest)
    own = root / own_id
    cache_at(root, new_mod_id(digest), digest)
    real_is_dir = Path.is_dir

    def is_dir(path):
        if path == own:
            raise PermissionError("exact candidate is unreadable")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    found = manager._cached_archive_previews(
        own_id,
        manager._res_dir(),
        {".png"},
        content_hash=digest,
        cache_index=__import__(
            "src.core.preview_cache_index", fromlist=["build_preview_cache_index"]
        ).build_preview_cache_index(root),
    )
    assert [Path(path).read_bytes() for path in found] == [b"current"]


def test_cancelled_rebuild_releases_inflight_cache_protection(manager, temp_data_dir, monkeypatch):
    archive, _digest = archive_at(temp_data_dir / "library")

    def cancel(*args):
        raise InterruptedError("cancelled")

    monkeypatch.setattr(manager._archive, "extract_files", cancel)
    with pytest.raises(InterruptedError):
        manager._hash_rebuild_descriptors(
            [{"path": str(archive), "mod_id": read_source_mod_id(archive), "imported_at": "2026-01-01T00:00:00"}]
        )
    assert not manager.inflight_mod_ids()
    manager._cleanup_orphaned_managed_artifacts("game_a")
    root = cache_root(manager, temp_data_dir)
    assert not root.exists() or list(root.iterdir()) == []
