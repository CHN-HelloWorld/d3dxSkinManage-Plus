"""补充测量目录、大量预览、多个成员文件及固实 7z 等实际数据结构。"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
import traceback
from pathlib import Path

import perf_mod_library as bench
import py7zr


def build_tree(root: Path, ordinal: int, bulk_mib: int) -> dict:
    """创建带多张预览、纹理及确定性缓冲数据的测试目录，并返回成员数和未压缩大小。"""
    root.mkdir(parents=True)
    (root / "mod.ini").write_text(f"[Constants]\nglobal $shape = {ordinal}\n", encoding="utf-8")
    for number in range(4):
        (root / f"preview{number}.png").write_bytes(bench.PREVIEW)
    randomizer = random.Random(20261003 + ordinal)
    for number in range(40):
        (root / f"texture{number}.dds").write_bytes(randomizer.randbytes(4096))
    for number in range(bulk_mib):
        (root / f"buffer{number}.buf").write_bytes(randomizer.randbytes(1024 * 1024))
    files = list(root.iterdir())
    return {
        "members": len(files),
        "unpacked_bytes": sum(path.stat().st_size for path in files),
        "previews": 4,
        "texture_members": 40,
        "bulk_mib": bulk_mib,
    }


def main():
    """在隔离库中测量目录及固实归档的导入和重建，验证身份、预览及启用状态并保存结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bulk-mib", type=int, default=16)
    args = parser.parse_args()
    if args.bulk_mib < 1:
        parser.error("bulk-mib must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output / "results.json"
    if output.exists():
        raise FileExistsError(output)
    result = {
        "completed": False,
        "version": bench.source_version(),
        "items": [],
        "notes": [
            "two directory inputs and two solid LZMA2 7z inputs",
            "four previews and 40 texture members per Mod; two tags",
            "deterministic incompressible buffer bytes; no OS cache flush",
            "fixture construction excluded from prepare/commit wall times",
        ],
    }

    def save():
        """把当前测量结果写入指定 JSON 文件，使中途失败也保留已完成的样本。"""
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    with tempfile.TemporaryDirectory(prefix="d3dx-shapes-") as temporary:
        anchor = Path(temporary)
        case = anchor / "case"
        try:
            with bench.isolated_manager(case) as manager:
                from PySide6.QtGui import QImage

                from src.core.mod_intake import ModIntakeService

                service = ModIntakeService(manager)
                imported = []
                for ordinal in range(4):
                    shape = "directory" if ordinal < 2 else "solid_7z"
                    char = manager.add_character(name=f"Shape{ordinal}", node_type="character")
                    source = case / "inputs" / f"Shape{ordinal}"
                    item = {"shape": shape, **build_tree(source, ordinal, args.bulk_mib)}
                    if shape == "solid_7z":
                        archive_path = source.with_suffix(".7z")
                        start = time.perf_counter()
                        with py7zr.SevenZipFile(archive_path, "w") as archive:
                            archive.writeall(source, arcname=source.name)
                        item["fixture_compression_s"] = time.perf_counter() - start
                        item["archive_bytes"] = archive_path.stat().st_size
                        with py7zr.SevenZipFile(archive_path, "r") as archive:
                            item["solid"] = archive.archiveinfo().solid
                        assert item["solid"]
                        source = archive_path
                    request = service.plan_request(
                        source_path=source, game_id=bench.GAME, node_id=char.id, target_format="7z"
                    )
                    with bench.Meter(manager, case) as meter:
                        start = time.perf_counter()
                        prepared = service.prepare(request)
                        item["prepare_wall_s"] = time.perf_counter() - start
                        prepared.mod.info.tags = ["shape", shape]
                        assert manager._write_mod_sidecar(prepared.mod)
                        start = time.perf_counter()
                        mod = service.commit(prepared)
                        item["commit_wall_s"] = time.perf_counter() - start
                    assert len(mod.preview_images) == 4
                    assert all(not QImage(path).isNull() for path in mod.preview_images)
                    item.update(meter.result())
                    item["decoded_previews"] = len(mod.preview_images)
                    imported.append(mod)
                    result["items"].append(item)
                    bench.emit("shape", ordinal=ordinal, shape=shape, prepare_s=item["prepare_wall_s"])
                    save()
                identities = {mod.mod_id for mod in imported}
                enabled = manager.toggle_mod(imported[0].mod_id)
                assert enabled is not None and enabled.is_enabled
                enabled_identity = imported[0].mod_id
                start = time.perf_counter()
                report = manager.rebuild_index_from_filesystem(bench.GAME)
                result["warm_rebuild_wall_s"] = time.perf_counter() - start
                assert report.registered == 4
                mods = manager._index.get_all_mods(bench.GAME)
                assert {mod.mod_id for mod in mods} == identities
                assert {mod.mod_id for mod in mods if mod.is_enabled} == {enabled_identity}
                assert all(Path(mod.deployed_path).is_dir() for mod in mods if mod.is_enabled)
                assert all(len(mod.info.tags) == 2 for mod in mods)
                assert manager._cleanup_orphaned_managed_artifacts(bench.GAME)
                assert all(
                    len(mod.preview_images) == 4 and all(not QImage(path).isNull() for path in mod.preview_images)
                    for mod in mods
                )
                result["correctness"] = {
                    "registered": 4,
                    "persistent_ids_preserved": True,
                    "valid_previews_after_cleanup": 16,
                    "enabled_after_rebuild": 1,
                    "tags_verified": True,
                }
                result["completed"] = True
        except BaseException as error:
            result["error"] = {"message": str(error), "traceback": traceback.format_exc()}
        finally:
            if case.exists():
                bench.owned_remove(case, anchor)
            result["isolation_cleaned"] = not case.exists()
            save()
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
