"""验证 Mod 内容指纹、文件名规范化和并发复制的文件保护行为。"""

from __future__ import annotations

import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.core.mod_manager import _sanitize_folder_name
from src.core.mod_storage import ModStorage
from src.models.mod import Mod

# 内容指纹稳定性。


def test_compute_sha1_folder_independent_of_topname():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        a = tmp_path / "ModA"
        b = tmp_path / "完全不同的目录名"
        for d in (a, b):
            (d / "sub").mkdir(parents=True)
            (d / "sub" / "f.txt").write_bytes(b"hello world")
            (d / "root.ini").write_bytes(b"[Section]\nkey=value\n")
        h_a = Mod.compute_sha1(str(a))
        h_b = Mod.compute_sha1(str(b))
        assert h_a == h_b, f"重命名顶层目录后 hash 应保持: {h_a} vs {h_b}"


def test_compute_sha1_dirs_sorted_stable():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        a = tmp_path / "A"
        b = tmp_path / "B"
        # A：先建 z 再建 a
        (a / "z").mkdir(parents=True)
        (a / "z" / "f").write_bytes(b"z")
        (a / "a").mkdir(parents=True)
        (a / "a" / "f").write_bytes(b"a")
        # B：先建 a 再建 z
        (b / "a").mkdir(parents=True)
        (b / "a" / "f").write_bytes(b"a")
        (b / "z").mkdir(parents=True)
        (b / "z" / "f").write_bytes(b"z")
        assert Mod.compute_sha1(str(a)) == Mod.compute_sha1(str(b))


def test_compute_sha1_diff_content_diff_hash():
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "A"
        b = Path(tmp) / "B"
        a.mkdir()
        b.mkdir()
        (a / "f.txt").write_bytes(b"content A")
        (b / "f.txt").write_bytes(b"content B")
        assert Mod.compute_sha1(str(a)) != Mod.compute_sha1(str(b))


def test_compute_sha1_zip_equals_folder():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 构造文件夹
        folder = tmp_path / "ModC"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "a.bin").write_bytes(b"AAAA")
        (folder / "root.txt").write_bytes(b"R")
        h_folder = Mod.compute_sha1(str(folder))
        # 构造 ZIP（内容与文件夹一致；条目名是相对路径，正斜杠）
        zip_path = tmp_path / "ModC.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("root.txt", b"R")
            zf.writestr("sub/a.bin", b"AAAA")
        h_zip = Mod.compute_sha1(str(zip_path))
        assert h_zip == h_folder, "ZIP 与等价文件夹的哈希应相等"


# copy_mod 后缀保留


def test_copy_mod_folder_collision_appends_underscore():
    storage = ModStorage()
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        src = parent / "src" / "ModA"
        src.mkdir(parents=True)
        (src / "file.txt").write_bytes(b"x")
        # 在 dst_parent 先放一个同名目录
        (parent / "ModA").mkdir()
        result = storage.copy_mod(str(src), str(parent))
        assert result is not None
        assert result.name == "ModA_1", f"expected ModA_1, got {result.name}"


def test_copy_mod_zip_collision_keeps_extension():
    storage = ModStorage()
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        src_zip = parent / "src.zip"
        with zipfile.ZipFile(str(src_zip), "w") as zf:
            zf.writestr("a.txt", b"hi")
        # 先放一个同名 zip 在 dst_parent
        dst_existing = parent / "out" / "src.zip"
        dst_existing.parent.mkdir()
        shutil.copy(src_zip, dst_existing)
        result = storage.copy_mod(str(src_zip), str(dst_existing.parent))
        assert result is not None
        assert result.suffix.lower() == ".zip", f"ZIP 后缀必须保留: {result}"
        assert result.name == "src_1.zip", f"expected src_1.zip, got {result.name}"


# 文件名规范化。


def test_sanitize_folder_name_removes_invalid_chars():
    assert _sanitize_folder_name('a/b:c?d*e|f"g<h>i') == "a_b_c_d_e_f_g_h_i"


def test_sanitize_folder_name_strips_trailing_dot_space():
    assert _sanitize_folder_name("hello.  ") == "hello"


def test_sanitize_folder_name_fallback_on_empty():
    assert _sanitize_folder_name("", fallback="X") == "X"
    assert _sanitize_folder_name("///", fallback="X") == "___"  # 非法字符替换后非空


def test_sanitize_folder_name_keeps_unicode():
    assert _sanitize_folder_name("中文/名字") == "中文_名字"


# 同名资源的并发复制。


def test_copy_mod_concurrent_no_overwrite():
    """并发复制同名源时生成互不覆盖的目标目录。

    各副本的标记文件必须与对应源文件一致，不能被其他线程覆盖。
    """
    storage = ModStorage()
    N = 8
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dst_parent = tmp_path / "lib"
        dst_parent.mkdir()
        # 准备 N 个源目录：同名 ModR，但内容（标记）各异
        sources = []
        for i in range(N):
            s_root = tmp_path / f"src{i}" / "ModR"
            s_root.mkdir(parents=True)
            (s_root / "common.bin").write_bytes(b"shared")
            (s_root / "mark.txt").write_text(f"marker-{i}", encoding="utf-8")
            sources.append(s_root)

        # 并发执行 copy_mod
        results: list[Path] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(s):
            try:
                r = storage.copy_mod(str(s), str(dst_parent))
                with lock:
                    if r is not None:
                        results.append(r)
                    else:
                        errors.append(RuntimeError(f"copy_mod returned None for {s}"))
            except Exception as e:
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(worker, sources))

        assert not errors, f"并发 copy_mod 出现错误: {errors}"
        assert len(results) == N, f"应得到 {N} 个副本，实得 {len(results)}: {results}"

        # 检查目标路径全部不同
        names = sorted(p.name for p in results)
        assert len(set(names)) == N, f"目标名应全部唯一，实际: {names}"
        # 内容应各自独立 —— 收集所有标记并核对与源一一对应
        markers = sorted((p / "mark.txt").read_text(encoding="utf-8") for p in results)
        expected = sorted(f"marker-{i}" for i in range(N))
        assert markers == expected, f"内容混淆: 期望 {expected}, 实得 {markers}"


# 目录与同名压缩包共用名称冲突检查。


def test_copy_mod_folder_vs_existing_zip_conflict():
    """已有 Skin.zip 时，复制 Skin 目录应添加后缀并保留原压缩包。"""
    storage = ModStorage()
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp)
        # 模拟首次压缩存储产生的 Skin.zip 已在 library 中
        existing_zip = lib / "Skin.zip"
        with zipfile.ZipFile(str(existing_zip), "w") as zf:
            zf.writestr("Skin/mark.txt", "AAA")
        # 准备新的 Skin 文件夹源
        src = Path(tmp) / "src" / "Skin"
        src.mkdir(parents=True)
        (src / "mark.txt").write_text("BBB", encoding="utf-8")

        result = storage.copy_mod(str(src), str(lib))
        assert result is not None, "copy_mod 应成功"
        assert result.name != "Skin", "存在 Skin.zip 时新 Skin 文件夹必须加后缀"
        assert result.name == "Skin_1", f"期望 Skin_1, 实得 {result.name}"
        # 原 zip 保持不变
        with zipfile.ZipFile(str(existing_zip), "r") as zf:
            assert zf.read("Skin/mark.txt").decode() == "AAA", "原 zip 不应被破坏"


def test_copy_mod_zip_vs_existing_folder_conflict():
    storage = ModStorage()
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp)
        # 已有 Skin 文件夹
        (lib / "Skin").mkdir()
        (lib / "Skin" / "mark.txt").write_text("FOLDER", encoding="utf-8")
        # 新源是 Skin.zip
        src_zip = Path(tmp) / "src.zip"
        with zipfile.ZipFile(str(src_zip), "w") as zf:
            zf.writestr("file.txt", "ZIP_CONTENT")
        # rename 让 .name=Skin.zip
        target_src = Path(tmp) / "Skin.zip"
        shutil.move(str(src_zip), str(target_src))

        result = storage.copy_mod(str(target_src), str(lib))
        assert result is not None
        assert result.name == "Skin_1.zip", f"期望 Skin_1.zip, 实得 {result.name}"
        # 原文件夹未被破坏
        assert (lib / "Skin" / "mark.txt").read_text() == "FOLDER"
