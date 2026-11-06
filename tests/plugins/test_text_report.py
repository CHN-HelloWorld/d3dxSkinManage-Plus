"""验证宿主管理的临时文本报告及插件调用边界。"""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QDesktopServices

from src.plugin_sdk.errors import PermissionDenied, PluginError
from src.plugin_sdk.ui_ext import UIExtensionPoints
from src.ui.plugin_ui_host import PluginUIHost


@pytest.fixture
def report_host(tmp_path, monkeypatch):
    """隔离报告临时目录，记录文件关闭、桌面打开及失败通知。"""
    state = SimpleNamespace(opened=[], files=[], notifications=[], open_result=True)
    create_file = tempfile.NamedTemporaryFile

    def named_file(*args, **kwargs):
        """保留报告文件对象，以验证桌面打开前已关闭句柄。"""
        report = create_file(*args, **kwargs)
        state.files.append(report)
        return report

    def open_report(url):
        """记录桌面打开请求及文件关闭状态，返回预设的打开结果。"""
        state.opened.append((url, state.files[-1].closed))
        return state.open_result

    def notify(message, *, ok):
        """收集宿主通知内容与成功状态，避免创建真实提示窗口。"""
        state.notifications.append((message, ok))

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named_file)
    monkeypatch.setattr(QDesktopServices, "openUrl", open_report)
    window = SimpleNamespace(isVisible=lambda: True, _ensure_toast=lambda: SimpleNamespace(done=notify))
    state.host = PluginUIHost(window)
    state.host._notify_ready = True
    return state


@pytest.mark.parametrize("text", ["旧版迁移报告\r\n角色：角色甲\n皮肤✓\r结尾", ""])
def test_report_preserves_exact_utf8_bom_content_and_closes_before_opening(report_host, tmp_path, text):
    ui = UIExtensionPoints("test.report", None, report_host.host, granted=frozenset({"ui:views"}))

    result = ui.show_text_report(text, title="迁移报告")

    assert isinstance(result, str)
    path = Path(result)
    assert path.is_absolute()
    assert path.parent == tmp_path
    assert path.suffix == ".txt"
    assert path.read_bytes() == b"\xef\xbb\xbf" + text.encode("utf-8")
    assert len(report_host.opened) == 1
    url, closed = report_host.opened[0]
    assert url.isLocalFile()
    assert Path(url.toLocalFile()) == path
    assert closed
    assert report_host.notifications == []


@pytest.mark.parametrize("title", ["", "中文报告"])
def test_facade_delegates_plugin_identity_text_title_and_return_value(title):
    calls = []

    def show_report(plugin_id, text, report_title):
        """记录插件身份与报告参数，返回模拟的宿主文件路径。"""
        calls.append((plugin_id, text, report_title))
        return "host-owned-report.txt"

    host = SimpleNamespace(show_text_report=show_report)
    ui = UIExtensionPoints("test.report", None, host, granted=frozenset({"ui:views"}))

    result = ui.show_text_report("完整中文内容", title=title)

    assert result == "host-owned-report.txt"
    assert calls == [("test.report", "完整中文内容", title)]


def test_host_creates_unique_names_without_using_plugin_or_title_as_paths(report_host, tmp_path):
    first = Path(report_host.host.show_text_report("../plugin", "第一次", "../报告.txt"))
    second = Path(report_host.host.show_text_report("../plugin", "第二次", "../报告.txt"))

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.suffix == second.suffix == ".txt"
    assert first.read_bytes() == b"\xef\xbb\xbf" + "第一次".encode()
    assert second.read_bytes() == b"\xef\xbb\xbf" + "第二次".encode()


def test_failed_desktop_open_retains_output_and_notifies_with_its_path(report_host):
    report_host.open_result = False
    ui = UIExtensionPoints("test.report", None, report_host.host, granted=frozenset({"ui:views"}))

    result = ui.show_text_report("保留的报告", title="迁移结果")

    assert Path(result).read_bytes() == b"\xef\xbb\xbf" + "保留的报告".encode()
    assert len(report_host.notifications) == 1
    message, ok = report_host.notifications[0]
    assert result in message
    assert "迁移结果" in message
    assert not ok


def test_plugin_and_host_cleanup_do_not_delete_report(report_host):
    ui = UIExtensionPoints("test.report", None, report_host.host, granted=frozenset({"ui:views"}))
    path = Path(ui.show_text_report("清理后仍可阅读"))

    ui._close()
    report_host.host.cleanup_plugin("test.report")
    del ui
    del report_host.host

    assert path.read_bytes() == b"\xef\xbb\xbf" + "清理后仍可阅读".encode()


def test_creation_failure_propagates_without_opening_or_notifying(report_host, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "missing"))
    ui = UIExtensionPoints("test.report", None, report_host.host, granted=frozenset({"ui:views"}))

    with pytest.raises(OSError):
        ui.show_text_report("无法创建")

    assert report_host.opened == []
    assert report_host.notifications == []


def test_headless_report_returns_empty_string_without_side_effects(report_host, tmp_path):
    ui = UIExtensionPoints("test.report", None, granted=frozenset({"ui:views"}))

    assert ui.show_text_report("无界面报告") == ""
    assert report_host.opened == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("argument", ["text", "title"])
@pytest.mark.parametrize("value", [None, 42, b"report", [], {}])
def test_report_rejects_non_string_arguments_without_side_effects(report_host, tmp_path, headless, argument, value):
    host = None if headless else report_host.host
    ui = UIExtensionPoints("test.report", None, host, granted=frozenset({"ui:views"}))
    arguments = {"text": "报告", "title": "标题", argument: value}

    with pytest.raises(TypeError):
        ui.show_text_report(**arguments)

    assert report_host.opened == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headless", [False, True])
def test_report_requires_ui_views_permission(report_host, tmp_path, headless):
    host = None if headless else report_host.host
    ui = UIExtensionPoints("test.report", None, host, granted=frozenset({"mods:read", "fs:read"}))

    with pytest.raises(PermissionDenied):
        ui.show_text_report("未授权")

    assert report_host.opened == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headless", [False, True])
def test_report_rejects_retired_facade(report_host, tmp_path, headless):
    host = None if headless else report_host.host
    ui = UIExtensionPoints("test.report", None, host, granted=frozenset({"ui:views"}))
    ui._close()

    with pytest.raises(PluginError):
        ui.show_text_report("已停用")

    assert report_host.opened == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("headless", [False, True])
def test_report_rejects_worker_thread_but_allows_main_owner_thread(report_host, tmp_path, headless):
    host = None if headless else report_host.host
    ui = UIExtensionPoints("test.report", None, host, granted=frozenset({"ui:views"}))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(PluginError):
            pool.submit(ui.show_text_report, "后台报告").result(timeout=3)

    assert report_host.opened == []
    assert list(tmp_path.iterdir()) == []
    result = ui.show_text_report("主线程报告")
    if headless:
        assert result == ""
    else:
        assert Path(result).is_file()
