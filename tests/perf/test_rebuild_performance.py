from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.models.mod import Mod

pytestmark = pytest.mark.skipif(
    os.environ.get("D3DX_REBUILD_PERF") != "1",
    reason="性能基准测试需 D3DX_REBUILD_PERF=1 显式开启，运行时会生成数 GB 数据",
)

_COUNT = int(os.environ.get("D3DX_REBUILD_PERF_COUNT", "300"))
_SIZES_MB = [int(s) for s in os.environ.get("D3DX_REBUILD_PERF_SIZES", "30").split(",")]


# 使用 1×1 RGBA 图片构造包含预览图的归档，以覆盖 preview_cache 的命中路径。
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IENDb`\x82"
)


def _build_body_archive(dest: Path, size_mb: int, members: int = 8) -> None:
    """生成指定大小、固定成员数量且包含预览图的 7z 本体。

    改变包体大小时只调整单个成员的长度，以隔离成员数量对内容身份计算的影响。
    """
    import py7zr

    stem = dest.stem
    tex_count = max(1, members - 3)
    tex_bytes = max(1, size_mb * 1024 * 1024 // tex_count)
    with py7zr.SevenZipFile(
        dest,
        mode="w",
        filters=[{"id": py7zr.FILTER_COPY}],
    ) as sz:
        sz.writestr(b"[Constants]\nglobal $x = 1\n", f"{stem}/mod.ini")
        sz.writestr(os.urandom(64 * 1024), f"{stem}/pos.buf")
        # 在 IEND 之后填充约 120 KB 数据，保留图像可解码性。
        sz.writestr(_PNG_1X1 + os.urandom(120 * 1024), f"{stem}/preview.png")
        for i in range(tex_count):
            sz.writestr(os.urandom(tex_bytes), f"{stem}/tex{i:03d}.dds")


def _make_container(parent: Path, name: str, size_mb: int) -> Path:
    """创建包含归档本体、容器标记和分享元数据的有效容器。"""
    container = parent / name
    container.mkdir(parents=True, exist_ok=True)
    body = container / f"{name}.7z"
    _build_body_archive(body, size_mb)
    from src.core.mod_package import write_container_marker

    assert write_container_marker(container, body.name, Mod.compute_sha1(str(body)))
    meta = container / ".d3dxmeta"
    meta.mkdir(exist_ok=True)
    (meta / "info.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "kind": "d3dxSkinManage-Plus.mod-share",
                "name": name,
                "info": {},
                "previews": [],
            }
        ),
        encoding="utf-8",
    )
    return body


@pytest.fixture
def mm(temp_data_dir):
    from src.core.mod_manager import ModManager
    from tests.helpers import register_games, reset_game_service

    register_games("game_a")
    manager = ModManager()
    manager.set_game("game_a")
    yield manager
    reset_game_service()


def _timed_rebuild(mm, monkeypatch) -> tuple[float, float, dict, object]:
    """执行资源库重建，返回总耗时、身份阶段耗时、分项统计和结果报告。

    身份阶段还包含启用状态查询、预览扫描及元数据读取，应分别记录这些操作。
    """
    from src.core.mod_manager import ModManager

    real_hash_stage = ModManager._hash_rebuild_descriptors
    real_sha1 = Mod.compute_sha1
    real_enrich = ModManager._enrich_rebuild_item
    real_previews = ModManager._find_previews
    real_sidecar_mod = __import__("src.core.mod_manager", fromlist=["read_sidecar"])

    stage = {"total": 0.0}
    # list.append 是原子操作，多线程累计不必加锁（加锁反而会改变被测的并发行为）
    marks: dict[str, list] = {k: [] for k in ("sha1", "enrich", "previews", "sidecar")}

    def _timed_stage(self, descriptors, progress_cb=None, unreadable=None):
        start = time.perf_counter()
        try:
            return real_hash_stage(self, descriptors, progress_cb=progress_cb, unreadable=unreadable)
        finally:
            stage["total"] += time.perf_counter() - start

    def _wrap(bucket, fn):
        def inner(*a, **kw):
            start = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                marks[bucket].append(time.perf_counter() - start)

        return inner

    monkeypatch.setattr(ModManager, "_hash_rebuild_descriptors", _timed_stage)
    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(_wrap("sha1", real_sha1)))
    monkeypatch.setattr(ModManager, "_enrich_rebuild_item", _wrap("enrich", real_enrich))
    monkeypatch.setattr(ModManager, "_find_previews", _wrap("previews", real_previews))
    monkeypatch.setattr(real_sidecar_mod, "read_sidecar", _wrap("sidecar", real_sidecar_mod.read_sidecar))

    begin = time.perf_counter()
    report = mm.rebuild_index_from_filesystem("game_a")
    total = time.perf_counter() - begin
    breakdown = {k: sum(v) for k, v in marks.items()}
    breakdown["calls"] = {k: len(v) for k, v in marks.items()}
    return total, stage["total"], breakdown, report


@pytest.mark.parametrize("size_mb", _SIZES_MB)
def test_rebuild_perf(mm, monkeypatch, size_mb, capsys):
    char = mm.add_character(name="性能测试", node_type="character")
    lib = mm._get_character_library(char.id)

    build_start = time.perf_counter()
    for i in range(_COUNT):
        _make_container(lib, f"PerfMod{i:04d}", size_mb)
    build_elapsed = time.perf_counter() - build_start
    total_bytes = sum(p.stat().st_size for p in lib.rglob("*.7z"))

    # 首次重建包含预览缓存尚未建立时的读取成本。
    first_total, first_hash, first_parts, first_report = _timed_rebuild(mm, monkeypatch)
    # 连续采样三次取最短耗时，减少文件系统调度波动对稳态结果的影响。
    steady = [_timed_rebuild(mm, monkeypatch) for _ in range(3)]
    second_total = min(s[0] for s in steady)
    second_hash = min(s[1] for s in steady)
    second_parts = min(steady, key=lambda s: s[0])[2]
    second_report = steady[-1][3]

    with capsys.disabled():
        print(f"\n{'=' * 72}")
        print(
            f"重建性能  {_COUNT} 个 Mod / {size_mb} MB 7z / "
            f"实际占用 {total_bytes / 2**30:.2f} GiB（创建资源库 {build_elapsed:.1f}s）"
        )
        print(f"  首次重建   总 {first_total:6.3f}s   哈希阶段 {first_hash:6.3f}s   登记 {first_report.registered}")
        print(
            f"  稳态重建   总 {second_total:6.3f}s   哈希阶段 {second_hash:6.3f}s"
            f"   登记 {second_report.registered}   (min-of-3)"
        )
        print("  稳态三次   " + "  ".join(f"{s[0]:.3f}s" for s in steady))
        print(f"  最短样本的哈希阶段细分（{_COUNT} 个 Mod 的累计线程时间，可大于实际耗时）：")
        for key in ("sha1", "enrich", "previews", "sidecar"):
            spent = second_parts[key]
            calls = second_parts["calls"][key]
            per = spent / calls * 1000 if calls else 0.0
            print(f"    {key:9} {spent:6.3f}s  ({calls:4} 次, {per:6.3f} ms/次)")
        print(f"{'=' * 72}")

    assert second_report.registered == _COUNT, f"登记数不符：{second_report.registered} != {_COUNT}，性能数字无意义"

    sha1_spent = second_parts["sha1"]
    enrich_spent = second_parts["enrich"]
    total_target_met = second_total < 3.0

    with capsys.disabled():
        print(
            f"  总耗时目标（小于 3 秒）：{'达标' if total_target_met else '未达标'}"
            f"  实测 {second_total:.3f}s（三次 "
            f"{'/'.join(f'{s[0]:.2f}' for s in steady)}）"
        )
        print(
            f"  哈希阶段目标（小于 0.5 秒）：{'达标' if second_hash < 0.5 else '未达标'}  实测 {second_hash:.3f}s"
            f" —— 其中身份计算只占线程时间 "
            f"{sha1_spent / (sha1_spent + enrich_spent) * 100:.0f}%，其余是预览/元数据 IO"
        )

    # 分别约束内容指纹成本和总体重建耗时。总体包含预览及元数据读取，
    # 需使用较宽裕的时间上限，避免磁盘波动使正常执行随机失败。
    assert second_total < 6.0, f"重建总耗时 {second_total:.3f}s 超出 6 秒上限"
    assert sha1_spent < enrich_spent, f"内容身份成本 {sha1_spent:.3f}s 不应高于预览及元数据补充成本 {enrich_spent:.3f}s"
    assert sha1_spent / max(1, _COUNT) < 0.010, (
        f"单个 Mod 的身份计算 {sha1_spent / _COUNT * 1000:.2f} ms（线程时间）已超 10 ms，"
        f"远离成员表指纹应有的量级（独占时约 0.6 ms）"
    )


def _timeit(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def test_hash_cost_decoupled_from_body_size(tmp_path, capsys):
    """验证成员表指纹的计算成本与包体大小无关，并以读取全部字节的结果作为对照。

    各种大小的归档交错测量多轮并取最短耗时，以减小系统调度波动和时间漂移的影响。
    """
    from src.services.archive import ArchiveService

    sizes = (5, 30, 100)
    bodies = {}
    for size_mb in sizes:
        body = tmp_path / f"m{size_mb}.7z"
        _build_body_archive(body, size_mb)
        bodies[size_mb] = body

    cheap: dict[int, float] = {s: float("inf") for s in sizes}
    for _round in range(25):
        for size_mb in sizes:
            cost = _timeit(lambda: Mod.compute_sha1(str(bodies[size_mb])))
            cheap[size_mb] = min(cheap[size_mb], cost)

    expensive: dict[int, float] = {}
    real = ArchiveService.list_entries
    ArchiveService.list_entries = lambda self, p: None
    try:
        for size_mb in sizes:
            expensive[size_mb] = _timeit(lambda: Mod.compute_sha1(str(bodies[size_mb])))
    finally:
        ArchiveService.list_entries = real

    with capsys.disabled():
        print(f"\n{'=' * 66}")
        print("单归档身份成本（交错测量 25 次，取最短耗时；对照组读取全部字节）")
        print(f"  {'包体':>8} {'廉价(成员表)':>14} {'昂贵(全字节)':>14} {'倍数':>8}")
        for size_mb in sizes:
            print(
                f"  {size_mb:5} MB {cheap[size_mb] * 1000:11.3f} ms "
                f"{expensive[size_mb] * 1000:11.1f} ms "
                f"{expensive[size_mb] / cheap[size_mb]:7.0f}×"
            )

    ratio = cheap[100] / cheap[30]

    # 固定包体大小后改变成员数，分别观察这两个因素对计算成本的影响。
    member_rows = []
    for members in (8, 40, 200):
        body = tmp_path / f"n{members}.7z"
        _build_body_archive(body, 8, members=members)
        cost = min(_timeit(lambda: Mod.compute_sha1(str(body))) for _ in range(25))
        member_rows.append((members, cost))

    with capsys.disabled():
        print(f"  {'成员数':>8} {'廉价(成员表)':>14}   （固定 8 MB 包体）")
        for members, cost in member_rows:
            print(f"  {members:5} 个 {cost * 1000:11.3f} ms")
        print(f"{'=' * 66}")

    assert ratio < 1.2, f"30 MB 至 100 MB 归档的成员表计算耗时变化 {(ratio - 1) * 100:.0f}% 超出 20% 上限"
    # 结构性校验：若成本真与包体耦合，20 倍体积差会带来数倍耗时差
    assert cheap[100] / cheap[5] < 1.5, f"5 MB→100 MB（20 倍体积）耗时涨了 {cheap[100] / cheap[5]:.2f}× → 并未解耦"
