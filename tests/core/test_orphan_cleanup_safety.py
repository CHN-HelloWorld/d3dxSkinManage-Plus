from pathlib import Path

from src.core.library_snapshot import ProbeToken, probe_library
from tests.core.test_library_reconcile import mm as mm
from tests.core.test_mod_package import _link_directory


def test_synchronous_cleanup_never_traverses_junction_target(mm, tmp_path):
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    root = Path(request.cache_root)
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    image = outside / "preview.png"
    image.write_bytes(b"outside")
    link = root / "orphan-link"
    _link_directory(link, outside)

    # rmtree 可能拒绝顶层目录联接并保留待重试；无论返回值如何，都不能删除联接目标。
    mm._cleanup_orphaned_managed_artifacts("game_a")
    assert image.read_bytes() == b"outside"


def test_worker_cleanup_preserves_replaced_file(mm):
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    orphan = Path(request.cache_root) / "orphan"
    orphan.mkdir(parents=True)
    image = orphan / "preview.png"
    image.write_bytes(b"old")
    result = probe_library(request)
    # 保持旧文件节点存活，确保新旧身份可区分。
    image.rename(orphan / "old.png")
    image.write_bytes(b"new")

    list(mm.apply_library_probe_result(result))
    assert image.read_bytes() == b"new"
    assert (orphan / "old.png").read_bytes() == b"old"
    assert mm.has_pending_source_cleanup()


def test_worker_cleanup_preserves_target_of_replaced_ancestor(mm, tmp_path):
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    orphan = Path(request.cache_root) / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "preview.png").write_bytes(b"old")
    result = probe_library(request)
    original = tmp_path / "original"
    orphan.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    image = outside / "preview.png"
    image.write_bytes(b"outside")
    _link_directory(orphan, outside)

    list(mm.apply_library_probe_result(result))
    assert image.read_bytes() == b"outside"
    assert (original / "preview.png").read_bytes() == b"old"
    assert mm.has_pending_source_cleanup()
