from __future__ import annotations

import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import src.core.mod_exporter as export_module
from src.core.mod_exporter import ExportItem, ModExporter


@pytest.mark.parametrize(
    "source_archive,fmt",
    [
        (True, "zip"),
        (False, "zip"),
        (True, "folder"),
        (False, "folder"),
    ],
)
def test_parallel_same_name_exports_never_overwrite(
    tmp_path,
    monkeypatch,
    source_archive,
    fmt,
):
    output = tmp_path / "out"
    output.mkdir()
    original = output / ("shared.zip" if fmt == "zip" else "shared")
    if fmt == "zip":
        original.write_bytes(b"leave untouched")
    else:
        original.mkdir()
        (original / "original.txt").write_bytes(b"leave untouched")
    items = []
    for index in range(2):
        source = tmp_path / f"source{index}"
        source.mkdir()
        (source / "payload.txt").write_text(str(index), encoding="utf-8")
        if source_archive:
            archive = source.with_suffix(".zip")
            with zipfile.ZipFile(archive, "w") as stream:
                stream.write(source / "payload.txt", "body/payload.txt")
            source = archive
        items.append(ExportItem("shared", str(source)))

    # 两个写入操作选定目标名后同时暂停，以复现目标竞争。
    barrier = threading.Barrier(2)
    operation = (
        "copy2"
        if source_archive and fmt == "zip"
        else ("copytree" if not source_archive and fmt == "folder" else "move")
    )
    original_operation = getattr(export_module.shutil, operation)

    def simultaneous_write(source, destination, *args, **kwargs):
        if Path(destination).parent == output:
            barrier.wait(timeout=5)
        return original_operation(source, destination, *args, **kwargs)

    monkeypatch.setattr(export_module.shutil, operation, simultaneous_write)
    exporter = ModExporter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(exporter.export_one, item, output, fmt, False) for item in items]
        results = [future.result(timeout=10) for future in futures]

    assert all(result.ok for result in results), [result.error for result in results]
    assert len({result.out_path for result in results}) == 2
    assert len(list(output.iterdir())) == 3
    for index, result in enumerate(results):
        if fmt == "zip":
            with zipfile.ZipFile(result.out_path) as stream:
                payload = next(name for name in stream.namelist() if name.endswith("payload.txt"))
                assert stream.read(payload) == str(index).encode()
        else:
            assert (Path(result.out_path) / "payload.txt").read_text(encoding="utf-8") == str(index)
    assert (original if fmt == "zip" else original / "original.txt").read_bytes() == b"leave untouched"


@pytest.mark.parametrize("product_first", [True, False])
@pytest.mark.parametrize(
    "source_archive,fmt",
    [
        (True, "zip"),
        (False, "zip"),
        (True, "folder"),
        (False, "folder"),
    ],
)
def test_output_name_and_category_hierarchy_cannot_share_ownership(
    tmp_path,
    monkeypatch,
    source_archive,
    fmt,
    product_first,
):
    output = tmp_path / "out"
    output.mkdir()
    sources = []
    for index in range(2):
        source = tmp_path / f"source{index}"
        source.mkdir()
        (source / "payload.txt").write_text(str(index), encoding="utf-8")
        if source_archive:
            archive = source.with_suffix(".zip")
            with zipfile.ZipFile(archive, "w") as stream:
                stream.write(source / "payload.txt", "body/payload.txt")
            source = archive
        sources.append(source)
    category = "shared.zip" if fmt == "zip" else "shared"
    items = [
        ExportItem("shared", str(sources[0]), rel_dir=""),
        ExportItem("child", str(sources[1]), rel_dir=f"{category}/nested"),
    ]
    entered = threading.Event()
    release = threading.Event()
    operation = (
        "copy2"
        if source_archive and fmt == "zip"
        else ("copytree" if not source_archive and fmt == "folder" else "move")
    )
    original_operation = getattr(export_module.shutil, operation)

    def controlled_write(source, destination, *args, **kwargs):
        target = Path(destination)
        is_product = target.parent == output
        is_child = target.name == ("child.zip" if fmt == "zip" else "child")
        if (product_first and is_product) or (not product_first and is_child):
            entered.set()
            assert release.wait(5)
        return original_operation(source, destination, *args, **kwargs)

    monkeypatch.setattr(export_module.shutil, operation, controlled_write)
    exporter = ModExporter()
    first, second = (0, 1) if product_first else (1, 0)
    results = [None, None]
    with ThreadPoolExecutor(max_workers=2) as executor:
        initial = executor.submit(exporter.export_one, items[first], output, fmt, False)
        try:
            assert entered.wait(3)
            later = executor.submit(exporter.export_one, items[second], output, fmt, False)
            results[second] = later.result(timeout=3)
        finally:
            release.set()
        results[first] = initial.result(timeout=3)

    # 无论目标预留顺序如何，两个完成项都必须保留。
    assert all(result.ok for result in results), [result.error for result in results]
    product, child = (Path(result.out_path) for result in results)
    assert product not in child.parents
    for index, path in enumerate((product, child)):
        if fmt == "zip":
            assert path.is_file()
            with zipfile.ZipFile(path) as stream:
                member = next(name for name in stream.namelist() if name.endswith("payload.txt"))
                assert stream.read(member) == str(index).encode()
        else:
            assert (path / "payload.txt").read_text(encoding="utf-8") == str(index)

    # 同类别的后续成员须沿用相同的名称冲突映射。
    followup = exporter.export_one(
        ExportItem("another", str(sources[1]), rel_dir=f"{category}/nested"), output, fmt, False
    )
    assert followup.ok
    assert Path(followup.out_path).parent == child.parent


def test_copytree_collision_does_not_delete_a_directory_it_did_not_create(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("export", encoding="utf-8")
    output = tmp_path / "out"
    original_copytree = export_module.shutil.copytree
    foreign = output / "shared" / "foreign.txt"

    def directory_appears_before_copy(source, destination, *args, **kwargs):
        Path(destination).mkdir()
        foreign.write_text("must survive", encoding="utf-8")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(export_module.shutil, "copytree", directory_appears_before_copy)
    result = ModExporter().export_one(ExportItem("shared", str(source)), output, "folder", False)

    assert not result.ok
    assert foreign.read_text(encoding="utf-8") == "must survive"


def test_category_alias_does_not_merge_a_different_logical_category(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("export", encoding="utf-8")
    output = tmp_path / "out"
    exporter = ModExporter()
    product = exporter.export_one(ExportItem("shared", str(source), rel_dir=""), output, "folder")
    nested = exporter.export_one(ExportItem("first", str(source), rel_dir="shared"), output, "folder")
    other_category = exporter.export_one(ExportItem("second", str(source), rel_dir="shared_1"), output, "folder")

    assert product.ok and nested.ok and other_category.ok
    assert Path(nested.out_path).parent != Path(other_category.out_path).parent
    assert Path(product.out_path) not in Path(nested.out_path).parents
