"""慢归档扫描期间的导入确认、列表变化与窗口销毁必须保持响应和线程安全。"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from threading import Event, get_ident

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor, QImage

from src.services.archive import ArchiveService
from src.ui.dialogs.mod_import import ModImportDialog, _detect_keys_from_archive


def _archive(directory: Path, color: str = "red") -> Path:
    """创建有两个变体的小归档，真实提取用于验证预览身份与缓存隔离。"""
    directory.mkdir(parents=True, exist_ok=True)
    preview = directory / "preview.png"
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(preview))
    archive = directory / "variants.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for variant in ("variant_a", "variant_b"):
            output.writestr(f"{variant}/mod.ini", "[KeySwap]\nkey = p\n")
            output.write(preview, f"{variant}/preview.png")
    return archive


@pytest.fixture
def slow_preview(monkeypatch):
    """仅延迟真实归档提取，主线程误入提取会被记录而不会锁死测试。"""
    gui_thread = get_ident()
    started, release = Event(), Event()
    calls = []
    destinations = []
    original = ArchiveService.extract_files

    def extract(service, source, names, destination):
        """保持 INI 批量提取正常，只延迟待观察的预览图片读取。"""
        if all(name.lower().endswith(".ini") for name in names):
            return original(service, source, names, destination)
        calls.append((Path(source), get_ident()))
        destinations.append(Path(destination))
        (Path(destination) / "in-progress.tmp").write_text("active", encoding="utf-8")
        if len(calls) == 1 and get_ident() != gui_thread:
            started.set()
            assert release.wait(5)
        return original(service, source, names, destination)

    monkeypatch.setattr(ArchiveService, "extract_files", extract)
    yield started, release, calls, destinations, gui_thread
    release.set()


def _finish_workers(workers):
    """失败断言后仍让真实原生线程结束，避免一次回归中止整个 pytest 进程。"""
    for worker in workers:
        if worker is not None and shiboken6.isValid(worker):
            assert worker.wait(3000)
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 对 ZIP 落盘文件名的改写")
def test_zip_key_scan_reads_member_names_invalid_on_windows(tmp_path):
    """ZIP 中合法的成员名不必能在 Windows 落盘，按键检测须保留流式读取语义。"""
    archive = tmp_path / "variants.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("variant/bad:name.ini", "[KeySwap]\nkey = p\n")
    assert _detect_keys_from_archive(archive) == ["p"]


def test_import_during_preview_scan_waits_asynchronously_and_submits_once(qtbot, temp_data_dir, slow_preview):
    source = _archive(temp_data_dir / "source")
    started, release, calls, _destinations, gui_thread = slow_preview
    dialog = ModImportDialog(node_id="target", initial_paths=[str(source)])
    qtbot.addWidget(dialog)
    # 此处保留测试引用以便失败时安全收尾；无外部引用的析构由子进程用例覆盖。
    worker = dialog._scan_worker
    confirmed = []
    dialog.import_confirmed.connect(lambda items, node: confirmed.append((items, node)))
    try:
        qtbot.waitUntil(started.is_set)
        dialog._on_import()
        dialog._on_import()
        assert confirmed == [], "扫描未结束时不得提交导入或在界面线程补扫"
        assert all(thread != gui_thread for _source, thread in calls)
        release.set()
        qtbot.waitUntil(lambda: len(confirmed) == 1)
        assert confirmed == [([(str(source), "variants", "variant_a/preview.png", "G")], "target")]
        assert len(calls) == 1, "确认导入必须复用在途扫描，不能重新读取同一个归档"
        dialog._on_import()
        assert len(confirmed) == 1
    finally:
        release.set()
        _finish_workers([worker, dialog._scan_worker])


def test_cancel_pending_import_keeps_active_cache_until_scan_finishes(qtbot, temp_data_dir, slow_preview):
    source = _archive(temp_data_dir / "source")
    started, release, _calls, destinations, _gui_thread = slow_preview
    unrelated = temp_data_dir / "data" / "temp" / "preview_scan" / "other-window" / "keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    dialog = ModImportDialog(initial_paths=[str(source)])
    qtbot.addWidget(dialog)
    worker = dialog._scan_worker
    confirmed = []
    dialog.import_confirmed.connect(lambda *args: confirmed.append(args))
    try:
        qtbot.waitUntil(started.is_set)
        dialog._on_import()
        dialog.reject()
        assert confirmed == []
        assert (destinations[0] / "in-progress.tmp").exists(), "不能删除尚在写入的扫描目录"
        assert unrelated.read_text(encoding="utf-8") == "keep"
        release.set()
        _finish_workers([worker])
        qtbot.waitUntil(lambda: not destinations[0].exists())
        assert confirmed == [], "已取消的待导入操作不得由迟到结果重新提交"
        assert unrelated.exists()
    finally:
        release.set()
        _finish_workers([worker, dialog._scan_worker])


def test_adding_archive_during_scan_serializes_reads_and_isolates_same_names(qtbot, temp_data_dir, slow_preview):
    first = _archive(temp_data_dir / "first", "red")
    second = _archive(temp_data_dir / "second", "blue")
    started, release, calls, _destinations, _gui_thread = slow_preview
    dialog = ModImportDialog(initial_paths=[str(first)])
    qtbot.addWidget(dialog)
    workers = [dialog._scan_worker]
    try:
        qtbot.waitUntil(started.is_set)
        dialog._add_entries_batch([str(second)])
        workers.append(dialog._scan_worker)
        qtbot.wait(30)
        assert [source for source, _thread in calls] == [first], "在途扫描结束前不得叠加另一次解压"
        release.set()
        qtbot.waitUntil(lambda: all(entry._scanned for entry in dialog._entries))
        assert Counter(source for source, _thread in calls) == {first: 1, second: 1}
        previews = [entry.previews[0] for entry in dialog._entries]
        assert QImage(previews[0]).pixelColor(0, 0) == QColor("red")
        assert QImage(previews[1]).pixelColor(0, 0) == QColor("blue")
        dialog.reject()
    finally:
        release.set()
        _finish_workers([*workers, dialog._scan_worker])


def test_removed_entry_result_cannot_replace_new_row_preview(qtbot, temp_data_dir, slow_preview):
    first = _archive(temp_data_dir / "first", "red")
    second = _archive(temp_data_dir / "second", "blue")
    started, release, _calls, _destinations, _gui_thread = slow_preview
    dialog = ModImportDialog(initial_paths=[str(first)])
    qtbot.addWidget(dialog)
    workers = [dialog._scan_worker]
    try:
        qtbot.waitUntil(started.is_set)
        dialog._table.selectRow(0)
        dialog._remove_selected()
        dialog._add_entries_batch([str(second)])
        workers.append(dialog._scan_worker)
        release.set()
        qtbot.waitUntil(lambda: bool(dialog._entries) and dialog._entries[0]._scanned)
        assert len(dialog._entries) == 1
        assert dialog._entries[0].source_path == str(second)
        assert QImage(dialog._entries[0].previews[0]).pixelColor(0, 0) == QColor("blue")
        dialog.reject()
    finally:
        release.set()
        _finish_workers([*workers, dialog._scan_worker])


@pytest.mark.parametrize("action", ["reject", "refresh", "destroy"])
def test_slow_scan_survives_owner_changes_without_external_thread_reference(tmp_path, action):
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    probe = Path(__file__).with_name("_import_scan_lifetime_probe.py")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(probe), action, str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scan released safely" in result.stdout
