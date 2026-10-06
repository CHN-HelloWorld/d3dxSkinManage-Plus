"""通过真实宿主验证教学插件的导出、页面与异步预设操作。"""

from __future__ import annotations

import csv

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QCheckBox, QFileDialog, QLabel, QTableWidget, QWidget

from tests.plugins.test_mainwindow_service import PluginWindow
from tests.plugins.test_operations import library as library

pytestmark = pytest.mark.usefixtures("trusted_sample_plugins")


@pytest.fixture
def window(library, qtbot):
    """让插件窗口及其操作队列先于隔离资源库退出。"""
    widget = PluginWindow(library[0])
    qtbot.addWidget(widget)
    widget._stack.setCurrentIndex(1)
    widget.show()
    yield widget
    widget.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_inventory_menu_exports_real_metadata_and_cancel_leaves_no_file(window, library, monkeypatch, tmp_path, qtbot):
    from src.ui import plugin_menu

    _, mod = library
    target = tmp_path / "inventory.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(target), "CSV"))
    actions = plugin_menu.menu_actions("character-context", mod.node_id)
    assert len(actions) == 1
    task = actions[0][1]()
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok
    with target.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["mod_id"] == mod.mod_id
    assert rows[0]["name"] == mod.name
    assert rows[0]["category"] == "Character"

    before = target.read_bytes()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: ("", ""))
    assert window._plugin_manager.execute_command("example.mod-inventory.export") is None
    assert target.read_bytes() == before


def test_inventory_validates_context_and_survives_file_errors(window, monkeypatch, tmp_path, qtbot):
    plugins = window._plugin_manager
    target = tmp_path / "unwritable.csv"
    target.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(target), "CSV"))
    assert plugins.execute_command("example.mod-inventory.export", node_id="missing") is None
    task = plugins.execute_command("example.mod-inventory.export")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert plugins.get("example.mod-inventory").state.value == "active"
    assert window._plugin_ui_host._notify_queue[-1][2] == "error"


def test_inventory_does_not_write_after_deactivation_inside_file_dialog(window, monkeypatch, tmp_path):
    plugins = window._plugin_manager
    target = tmp_path / "cancelled.csv"

    def choose_path(*args, **kwargs):
        """模拟文件对话框嵌套事件循环期间停用插件。"""
        assert plugins.disable("example.mod-inventory")
        return str(target), "CSV"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_path)
    assert plugins.execute_command("example.mod-inventory.export") is None
    assert not target.exists()


def test_audit_refreshes_on_events_preserves_settings_and_releases_widgets(window, library, qtbot):
    from src.ui import theme

    manager, mod = library
    plugins = window._plugin_manager
    record = plugins.get("example.preset-audit")
    page = window._stack.findChild(QWidget, "presetAuditPage")
    assert page is not None
    table = page.findChild(QTableWidget)
    checkbox = page.findChild(QCheckBox)
    timer = page.findChild(QTimer)
    preset = manager.preset_manager.create_preset("plugin-test", "待检查", {mod.node_id: [mod.mod_id]})
    preset.character_mods[mod.node_id].append("missing-mod")
    preset.mod_variants[mod.mod_id] = "missing-variant"
    manager.preset_manager.preset_applied.emit([])
    qtbot.waitUntil(lambda: table.rowCount() == 2)
    assert {table.item(row, 1).text() for row in range(2)} == {mod.name, "missing-mod"}

    checkbox.setChecked(False)
    assert record.context.storage.get("include_default") is False
    original_theme = theme.current_theme(), theme.is_background_active()
    try:
        theme.set_theme("dark")
        theme.set_background_active(True)
        window._plugin_ui_host.retheme()
        assert not checkbox.isChecked()
        assert window._stack.findChild(QWidget, "presetAuditPage") is page
    finally:
        theme.set_theme(original_theme[0])
        theme.set_background_active(original_theme[1])

    preset.character_mods[mod.node_id].remove("missing-mod")
    preset.mod_variants[mod.mod_id] = None
    manager.preset_manager.preset_applied.emit([mod.mod_id])
    summary = page.findChild(QLabel, "presetAuditSummary")
    qtbot.waitUntil(lambda: "发现 0 项失效引用" in summary.text())
    assert table.rowCount() == 0
    assert plugins.reload(record.id)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(not shiboken6.isValid(widget) for widget in (page, table, checkbox, timer))
    replacement = window._stack.findChild(QWidget, "presetAuditPage")
    assert not replacement.findChild(QCheckBox).isChecked()
    assert plugins.disable(record.id)
    assert window._stack.count() == 1
    assert window._nav.count() == 0
    assert plugins.get("example.mod-inventory").state.value == "ready"


def test_audit_reports_read_errors_without_claiming_references_are_valid(window, library, monkeypatch, qtbot):
    manager, mod = library
    preset = manager.preset_manager.create_preset("plugin-test", "需要检查", {mod.node_id: [mod.mod_id]})
    preset.mod_variants[mod.mod_id] = "variant"
    record = window._plugin_manager.get("example.preset-audit")

    def unreadable_variants(mod_id):
        """只阻断变体文件读取，保留真实预设和页面刷新流程。"""
        raise OSError("unreadable variants")

    monkeypatch.setattr(record.context.variants, "get_variants", unreadable_variants)
    manager.preset_manager.preset_applied.emit([])
    label = window._stack.findChild(QLabel, "presetAuditSummary")
    qtbot.waitUntil(lambda: "unreadable variants" in label.text())
    assert "检查未完成" in label.text()


def test_cycle_uses_host_service_ignores_repeated_request_and_refreshes_feedback(window, library, qtbot):
    manager, mod = library
    target = manager.preset_manager.create_preset("plugin-test", "组合甲", {mod.node_id: [mod.mod_id]})
    plugins = window._plugin_manager
    task = plugins.execute_command("example.preset-cycle.next")
    assert task is not None and not task.done
    assert not window._game_page.isEnabled()
    assert plugins.execute_command("example.preset-cycle.next") is task
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.preset_id == target.id
    assert manager.get_mod(mod.mod_id).is_enabled
    assert manager.preset_manager.get_current("plugin-test").id == target.id
    assert window.refreshes == ["plugin-test"]
    assert window._game_page.isEnabled()
    assert window._plugin_progress._title.text() == "插件操作已完成"


def test_cycle_failure_allows_retry_and_does_not_report_success(window, library, qtbot, tmp_path):
    manager, mod = library
    manager.preset_manager.create_preset("plugin-test", "组合甲", {mod.node_id: [mod.mod_id]})
    manager.set_deploy_dir("")
    plugins = window._plugin_manager
    task = plugins.execute_command("example.preset-cycle.next")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert window._plugin_ui_host._notify_queue[-1][2] == "error"
    manager.set_deploy_dir(str(tmp_path / "new-deploy"))
    retried = plugins.execute_command("example.preset-cycle.next")
    qtbot.waitUntil(lambda: retried.done)
    assert retried.result.ok
    assert manager.get_mod(mod.mod_id).is_enabled


def test_cycle_deactivation_cancels_pending_operation(window, library, qtbot):
    manager, mod = library
    manager.preset_manager.create_preset("plugin-test", "组合甲", {mod.node_id: [mod.mod_id]})
    plugins = window._plugin_manager
    task = plugins.execute_command("example.preset-cycle.next")
    assert plugins.disable("example.preset-cycle")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert all(pid != "example.preset-cycle" for pid, _, _ in window._plugin_ui_host._notify_queue)


def test_cycle_without_another_preset_does_not_submit_work(window, library):
    manager, mod = library
    assert window._plugin_manager.execute_command("example.preset-cycle.next") is None
    assert not window._plugin_operations.busy
    assert not manager.get_mod(mod.mod_id).is_enabled


def test_audit_translates_controls_without_changing_stored_boolean(library, qtbot, monkeypatch):
    from src.utils import i18n

    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    widget = PluginWindow(library[0])
    qtbot.addWidget(widget)
    try:
        plugins = widget._plugin_manager
        page = widget._stack.findChild(QWidget, "presetAuditPage")
        checkbox = page.findChild(QCheckBox)
        assert checkbox.text() == "Include default preset"
        checkbox.setChecked(False)
        assert plugins.get("example.preset-audit").context.storage.get("include_default") is False
        assert plugins.reload("example.preset-audit")
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        replacement = widget._stack.findChild(QWidget, "presetAuditPage")
        assert not replacement.findChild(QCheckBox).isChecked()
    finally:
        widget.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
