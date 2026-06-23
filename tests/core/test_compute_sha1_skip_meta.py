"""Task1: compute_sha1 跳过 .d3dxmeta（sidecar 不影响内容哈希）

起内容身份改为「成员表指纹」（rel + 解压后大小 + 解压后字节的 CRC-32），
故本文件里手算期望摘要的那条已按新公式重写 —— 见该测试的 docstring。
"""

from __future__ import annotations

import hashlib
import zipfile
import zlib
from pathlib import Path

import pytest

from src.models.mod import Mod


def _write_mod_folder(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    (root / "tex.dds").write_bytes(b"\x00\x01\x02\x03")
    sub = root / "sub"
    sub.mkdir()
    (sub / "more.buf").write_bytes(b"\x04\x05")
    return root


def test_folder_hash_unchanged_when_sidecar_added(tmp_path):
    mod = _write_mod_folder(tmp_path / "mod")
    before = Mod.compute_sha1(str(mod))

    meta = mod / ".d3dxmeta"
    (meta / "previews").mkdir(parents=True)
    (meta / "info.json").write_text('{"schema": 2, "name": "示例角色丁"}', encoding="utf-8")
    (meta / "previews" / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    after = Mod.compute_sha1(str(mod))
    assert before == after, "加入 .d3dxmeta 后内容哈希必须不变"


def test_nested_sidecar_also_skipped(tmp_path):
    mod = _write_mod_folder(tmp_path / "mod")
    before = Mod.compute_sha1(str(mod))
    nested = mod / "sub" / ".d3dxmeta"
    nested.mkdir(parents=True)
    (nested / "info.json").write_text("{}", encoding="utf-8")
    assert Mod.compute_sha1(str(mod)) == before


def test_zip_hash_ignores_sidecar(tmp_path):
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("M/mod.ini", "[Constants]\nx=1\n")
        zf.writestr("M/tex.dds", b"\x00\x01\x02\x03")

    withmeta = tmp_path / "withmeta.zip"
    with zipfile.ZipFile(withmeta, "w") as zf:
        zf.writestr("M/mod.ini", "[Constants]\nx=1\n")
        zf.writestr("M/tex.dds", b"\x00\x01\x02\x03")
        zf.writestr("M/.d3dxmeta/info.json", '{"schema": 2}')
        zf.writestr("M/.d3dxmeta/previews/01.png", b"\x89PNG")

    assert Mod.compute_sha1(str(plain)) == Mod.compute_sha1(str(withmeta))


def test_7z_hash_reads_member_table_and_ignores_sidecar(
    tmp_path,
    monkeypatch,
):
    """7z 内容身份仅由成员表计算，不读取成员内容且排除 .d3dxmeta。

    按 ``rel \\0 size \\0 %08x(crc32) \\0`` 独立计算预期摘要，
    并禁止读取和解压成员，验证成员表指纹的定义及读取边界。
    """
    py7zr = pytest.importorskip("py7zr")
    entries = {
        "M/mod.ini": b"[Constants]\nx=1\n",
        "M/tex.dds": b"\x00\x01\x02\x03",
        "M/sub/more.buf": b"\x04\x05",
        "M/.d3dxmeta/info.json": b'{"schema": 2}',
        "M/.d3dxmeta/previews/01.png": b"\x89PNG",
    }
    source = tmp_path / "source"
    for name, data in entries.items():
        member = source / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(data)
    archive = tmp_path / "mod.7z"
    with py7zr.SevenZipFile(archive, mode="w") as seven_zip:
        for name in entries:
            seven_zip.write(source / name, arcname=name)

    # 用调用计数验证成员表路径不读取归档成员，避免替身异常被读取回退捕获。
    from src.services.archive import ArchiveService

    read_calls: list[str] = []
    monkeypatch.setattr(
        ArchiveService,
        "read_file",
        lambda self, ap, ip: read_calls.append(ip) or b"",
    )
    # 同时验证该路径不触发整包解压。
    expensive_calls: list[str] = []
    real_expensive = Mod._7z_entries_by_bytes
    monkeypatch.setattr(
        Mod,
        "_7z_entries_by_bytes",
        staticmethod(lambda *a, **kw: expensive_calls.append("hit") or real_expensive(*a, **kw)),
    )

    expected = hashlib.sha1()
    effective = {name: data for name, data in entries.items() if "/.d3dxmeta/" not in f"/{name}"}
    # 期望值按「剥掉单顶层后的相对路径」计算：归档身份必须等于它解压出来的那个文件夹的
    # 身份，而 ArchiveService.extract 会把单一顶层目录当作本体返回。用完整成员名手算
    # 会让「文件夹」与「它压出的归档」得到不同 hash。
    for name in sorted(effective, key=lambda n: n.split("/", 1)[1]):
        data = effective[name]
        expected.update(name.split("/", 1)[1].encode("utf-8"))
        expected.update(b"\x00")
        expected.update(str(len(data)).encode("ascii"))
        expected.update(b"\x00")
        expected.update(("%08x" % (zlib.crc32(data) & 0xFFFFFFFF)).encode("ascii"))
        expected.update(b"\x00")

    assert Mod.compute_sha1(str(archive)) == expected.hexdigest()
    assert read_calls == [], f"廉价途径不该读成员字节，却读了: {read_calls}"
    assert expensive_calls == [], "廉价途径不该触发整包解压回退"


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_archive_hash_equals_extracted_folder_hash(tmp_path, fmt):
    """归档 hash 必须等于同内容文件夹 hash —— 查重/重定位/更新检测的地基。

    曾不成立：压缩产物成员带一层与容器同名的顶层前缀，而文件夹哈希是相对本体的，两者
    差一层前缀 → 同一份内容算出两个 hash。实测后果是库内 7z 的 content_hash 记的是压缩前
    文件夹的值、与磁盘归档真实值不符，用户在库内压缩容器会被当成新 Mod 重复入库。
    """
    from src.services.archive import ArchiveService

    body = _write_mod_folder(tmp_path / "示例角色戊皮肤")
    folder_hash = Mod.compute_sha1(str(body))

    produced = ArchiveService().compress(str(body), str(tmp_path / "out"), fmt)
    if produced is None:
        pytest.skip(f"{fmt} 压缩后端不可用")
    produced = Path(produced)

    # 归档必须包含顶层目录，使直接复制导出的文件在解压时保持独立目录。
    members = [n.replace("\\", "/") for n in ArchiveService().list_contents(produced)]
    assert members, "压缩产物为空"
    assert all("/" in n for n in members), f"产物缺顶层目录（会炸包）: {members}"
    assert len({n.split("/")[0] for n in members}) == 1, f"产物非单顶层: {members}"

    assert Mod.compute_sha1(str(produced)) == folder_hash, (
        f"{fmt} 归档 hash 与文件夹 hash 不等 → 查重会把同一 Mod 当两份"
    )
