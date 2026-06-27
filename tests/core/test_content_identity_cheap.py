"""验证归档成员摘要和实际内容读取使用同一指纹公式，并与文件夹内容一致。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.models.mod import Mod
from src.services.archive import ArchiveService
from tests.helpers import registered_games as registered_games

_BODY = {
    "mod.ini": b"[Constants]\nx=1\n",
    "tex.dds": b"\x00\x01\x02\x03" * 500,
    "empty.txt": b"",  # CRC32 天然为 0，不得被误判成「缺 CRC」
    "sub/more.buf": b"\x04\x05",
}


def _write_body(root: Path, overrides: dict[str, bytes] | None = None) -> Path:
    """按 _BODY 写出一个 Mod 本体目录；overrides 可替换/新增成员。"""
    payload = dict(_BODY)
    payload.update(overrides or {})
    for rel, data in payload.items():
        member = root / rel
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(data)
    return root


def _make_archive(tmp_path: Path, fmt: str, *, tag: str = "a", overrides: dict[str, bytes] | None = None) -> Path:
    body = _write_body(tmp_path / tag / "示例角色戊皮肤", overrides)
    produced = ArchiveService().compress(str(body), str(tmp_path / f"out_{tag}"), fmt)
    if produced is None:
        pytest.skip(f"{fmt} 压缩后端不可用")
    return Path(produced)


@pytest.fixture
def force_expensive(monkeypatch):
    """让廉价途径整体不可用，迫使 compute_sha1 走「读字节现算 CRC」。"""
    monkeypatch.setattr(ArchiveService, "list_entries", lambda self, path: None)


# ① 两条途径必须产出相同摘要


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_cheap_and_expensive_paths_agree(tmp_path, fmt, monkeypatch):
    archive = _make_archive(tmp_path, fmt)

    cheap = Mod.compute_sha1(str(archive))

    monkeypatch.setattr(ArchiveService, "list_entries", lambda self, path: None)
    expensive = Mod.compute_sha1(str(archive))

    assert cheap == expensive, f"{fmt}: 廉价与昂贵途径摘要不等 → 同一份内容有两个身份，查重/认领必失准"
    assert len(cheap) == 40 and cheap == cheap.lower()


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_expensive_path_still_equals_folder_hash(tmp_path, fmt, force_expensive):
    body = _write_body(tmp_path / "src" / "示例角色戊皮肤")
    produced = ArchiveService().compress(str(body), str(tmp_path / "out"), fmt)
    if produced is None:
        pytest.skip(f"{fmt} 压缩后端不可用")

    assert Mod.compute_sha1(str(produced)) == Mod.compute_sha1(str(body))


# ② 成员缺 CRC → 正确回退（不是跳过该成员、不是换公式）


def test_falls_back_when_backend_reports_no_crc(tmp_path, monkeypatch):
    """成员表缺少 CRC 时，应读取归档内容并得到相同的完整摘要。

    仅模拟 SevenZipFile.list 缺少 CRC，保留成员读取能力以验证回退结果。
    """
    py7zr = pytest.importorskip("py7zr")
    archive = _make_archive(tmp_path, "7z")
    baseline = Mod.compute_sha1(str(archive))
    folder_hash = Mod.compute_sha1(str(tmp_path / "a" / "示例角色戊皮肤"))
    assert baseline == folder_hash, "前置条件：廉价途径本应等于文件夹哈希"

    real_list = py7zr.SevenZipFile.list

    def _no_crc(self):
        infos = real_list(self)
        for info in infos:
            if not info.is_directory:
                info.crc32 = None
        return infos

    monkeypatch.setattr(py7zr.SevenZipFile, "list", _no_crc)
    assert ArchiveService().list_entries(archive) is None, "缺 CRC 必须整包判不可用"
    assert Mod.compute_sha1(str(archive)) == baseline, "缺 CRC 回退后摘要变了 → 回退路径用了另一个公式"


def test_zero_crc_on_nonempty_member_is_treated_as_unavailable(tmp_path):
    import struct

    payload = b"\x01\x02\x03\x04\x05"
    archive = tmp_path / "nocrc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("M/tex.dds", payload)
        zf.writestr("M/empty.txt", b"")
    raw = archive.read_bytes()
    archive.write_bytes(raw.replace(struct.pack("<I", zipfile.crc32(payload) & 0xFFFFFFFF), b"\x00\x00\x00\x00"))

    assert ArchiveService().list_entries(archive) is None

    # 空文件单独成包时 CRC 0 合法 → 廉价途径照常可用
    only_empty = tmp_path / "onlyempty.zip"
    with zipfile.ZipFile(only_empty, "w") as zf:
        zf.writestr("M/empty.txt", b"")
    entries = ArchiveService().list_entries(only_empty)
    assert entries == [("M/empty.txt", 0, 0)]


# ③ 内容变了摘要必须变


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_member_content_change_changes_digest(tmp_path, fmt):
    base = _make_archive(tmp_path, fmt, tag="base")
    # 同名、**同长度**、内容不同 —— 只看 (名, 大小) 的公式会漏掉，必须靠 CRC 抓住
    changed = _make_archive(tmp_path, fmt, tag="changed", overrides={"tex.dds": b"\x00\x01\x02\xff" * 500})

    assert Mod.compute_sha1(str(base)) != Mod.compute_sha1(str(changed))


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_member_rename_changes_digest(tmp_path, fmt):
    base = _make_archive(tmp_path, fmt, tag="base")
    renamed = _write_body(tmp_path / "ren" / "示例角色戊皮肤")
    (renamed / "mod.ini").rename(renamed / "renamed.ini")
    produced = ArchiveService().compress(str(renamed), str(tmp_path / "out_ren"), fmt)
    if produced is None:
        pytest.skip(f"{fmt} 压缩后端不可用")

    assert Mod.compute_sha1(str(base)) != Mod.compute_sha1(str(produced))


def test_extra_member_changes_digest(tmp_path):
    base = _make_archive(tmp_path, "zip", tag="base")
    more = _make_archive(tmp_path, "zip", tag="more", overrides={"extra.buf": b"\x09"})
    assert Mod.compute_sha1(str(base)) != Mod.compute_sha1(str(more))


# ④ 只改元数据摘要不变（不变量）


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_sidecar_members_excluded(tmp_path, fmt):
    plain = _make_archive(tmp_path, fmt, tag="plain")
    withmeta = _make_archive(
        tmp_path,
        fmt,
        tag="withmeta",
        overrides={
            ".d3dxmeta/info.json": '{"schema": 2, "name": "示例角色丁"}'.encode(),
            ".d3dxmeta/previews/01.png": b"\x89PNG\r\n\x1a\n",
            ".d3dxmod": b'{"schema": 1}',
        },
    )

    assert Mod.compute_sha1(str(plain)) == Mod.compute_sha1(str(withmeta))


def test_sidecar_excluded_on_expensive_path_too(tmp_path, force_expensive):
    plain = _make_archive(tmp_path, "zip", tag="plain")
    withmeta = _make_archive(
        tmp_path, "zip", tag="withmeta", overrides={".d3dxmeta/info.json": b"{}", ".d3dxmod": b"{}"}
    )

    assert Mod.compute_sha1(str(plain)) == Mod.compute_sha1(str(withmeta))


def test_folder_body_ignores_stray_marker(tmp_path):
    body = _write_body(tmp_path / "b" / "M")
    before = Mod.compute_sha1(str(body))
    (body / ".d3dxmod").write_bytes(b'{"schema": 1}')
    assert Mod.compute_sha1(str(body)) == before


# 契约：不返回空串 / 可取消


def test_never_returns_empty_string(tmp_path):
    assert len(Mod.compute_sha1(str(tmp_path / "missing.7z"))) == 40
    broken = tmp_path / "broken.7z"
    broken.write_bytes(b"garbage, not an archive")
    assert len(Mod.compute_sha1(str(broken))) == 40


def test_checkpoint_cancellation_raises(tmp_path):
    archive = _make_archive(tmp_path, "zip")
    with pytest.raises(InterruptedError):
        Mod.compute_sha1(str(archive), checkpoint=lambda: False)


def test_digest_is_stable_across_repeated_calls(tmp_path):
    archive = _make_archive(tmp_path, "7z")
    values = {Mod.compute_sha1(str(archive)) for _ in range(3)}
    assert len(values) == 1


def test_preview_lookup_uses_explicit_fingerprint_without_rehashing(temp_data_dir, monkeypatch):
    from src.core.mod_identity import new_mod_id
    from src.core.mod_manager import ModManager
    from src.core.preview_cache_index import capture_source_version

    archive = _make_archive(temp_data_dir, "zip", overrides={"preview.png": b"current"})
    source_version = capture_source_version(archive)
    digest = Mod.compute_sha1(str(archive))
    manager = ModManager()
    manager.set_game("game_a")

    def rehash(*args, **kwargs):
        pytest.fail("preview lookup recomputed the hash already calculated by rebuild")

    monkeypatch.setattr(Mod, "compute_sha1", rehash)
    mod_id = new_mod_id()
    found = manager._find_previews(str(archive), mod_id=mod_id, content_hash=digest, source_version=source_version)
    assert [Path(path).read_bytes() for path in found] == [b"current"]
    root = temp_data_dir / "data" / manager._res_dir() / "preview_cache" / mod_id
    assert not (root / ".preview-content.json").exists()
