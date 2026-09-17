import json
import sqlite3
from threading import Event
from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from src.ui.main_window import MainWindow


class UsagePage(QWidget):
    def __init__(self):
        super().__init__()
        self.results = []

    def set_download_usage(self, *values):
        self.results.append(values)


def test_settings_request_does_not_scan_on_owner_thread(qtbot, temp_data_dir, monkeypatch):
    from src.services.gamebanana import download_store
    from src.utils.paths import get_data_dir

    db = get_data_dir() / "mod_index.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mods (info_json TEXT)")
        conn.execute("INSERT INTO mods VALUES (?)", (json.dumps({"custom_data": {"gamebanana": {"file_id": 42}}}),))
    entered, release = Event(), Event()

    def scan(ids, md5s, **kwargs):
        entered.set()
        assert release.wait(3)
        assert ids == {42}
        return download_store.DownloadUsage(100, 2, 40, 1)

    monkeypatch.setattr(download_store, "scan_downloads", scan)

    def forbidden():
        raise AssertionError("settings entry synchronously scans the entire library")

    host = SimpleNamespace(_scan_download_usage=forbidden)
    page = UsagePage()
    qtbot.addWidget(page)
    try:
        MainWindow._on_download_usage_requested(host, page)
        qtbot.waitUntil(entered.is_set)
        assert not page.results
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(page.results))
    assert page.results == [(100, 2, 40, 1)]


def test_background_metadata_is_cross_game_and_read_only(tmp_path):
    from src.services.gamebanana.download_store import scan_library_download_usage

    db = tmp_path / "index.db"
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    digest = "abcdef0123456789abcdef0123456789"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mods (game_id TEXT, info_json TEXT)")
        for game, metadata in [
            ("a", {"file_id": "42"}),
            ("b", {"file_md5": digest.upper()}),
            ("b", {"file_id": "invalid"}),
            ("c", None),
        ]:
            conn.execute(
                "INSERT INTO mods VALUES (?, ?)", (game, json.dumps({"custom_data": {"gamebanana": metadata}}))
            )
    (downloads / "nomd5__42__a.zip").write_bytes(b"123")
    (downloads / f"{digest}__0__b.zip").write_bytes(b"45")
    (downloads / "nomd5__77__c.zip").write_bytes(b"6")
    before = db.read_bytes()
    result = scan_library_download_usage(db, downloads)
    assert (result.total_bytes, result.total_count, result.purgeable_bytes, result.purgeable_count) == (6, 3, 5, 2)
    assert db.read_bytes() == before
    missing = tmp_path / "missing.db"
    import pytest

    with pytest.raises(sqlite3.OperationalError):
        scan_library_download_usage(missing, downloads)
    assert not missing.exists()


def test_loader_coalesces_refresh_and_preserves_large_sizes(qtbot, monkeypatch):
    from src.services.gamebanana import download_store
    from src.ui.download_usage_loader import DownloadUsageLoader

    entered, release = Event(), Event()
    calls = []

    def scan(*paths):
        calls.append(paths)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return download_store.DownloadUsage(9_000_000_000, 4, 5_000_000_000, 2)

    monkeypatch.setattr(download_store, "scan_library_download_usage", scan)
    page = UsagePage()
    qtbot.addWidget(page)
    loader = DownloadUsageLoader(page)
    loader.ready.connect(page.set_download_usage)
    try:
        loader.request("first", "downloads")
        qtbot.waitUntil(entered.is_set)
        for _ in range(10):
            loader.request("latest", "downloads")
    finally:
        release.set()
    qtbot.waitUntil(lambda: bool(page.results))
    assert calls == [("first", "downloads"), ("latest", "downloads")]
    assert page.results == [(9_000_000_000, 4, 5_000_000_000, 2)]


def test_destroyed_page_does_not_receive_background_result(qtbot, monkeypatch):
    import shiboken6
    from PySide6.QtCore import QThreadPool

    from src.services.gamebanana import download_store
    from src.ui.download_usage_loader import DownloadUsageLoader

    entered, release = Event(), Event()

    def scan(*paths):
        entered.set()
        assert release.wait(3)
        return download_store.DownloadUsage()

    monkeypatch.setattr(download_store, "scan_library_download_usage", scan)
    page = UsagePage()
    loader = DownloadUsageLoader(page)
    results = []
    loader.ready.connect(lambda *values: results.append(values))
    try:
        loader.request("index", "downloads")
        qtbot.waitUntil(entered.is_set)
        page.deleteLater()
        qtbot.waitUntil(lambda: not shiboken6.isValid(page))
    finally:
        release.set()
    qtbot.waitUntil(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
    assert results == []
