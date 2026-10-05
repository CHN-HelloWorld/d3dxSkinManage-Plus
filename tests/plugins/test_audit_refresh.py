"""验证预设检查仅在可见时读取变体，并在批次之间交还 Qt 事件循环。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QLabel, QTableWidget

from src.plugins.runtime.manager import PluginManager
from tests.plugins.test_examples import example_module
from tests.plugins.test_operations import library as library


@pytest.fixture
def audit_environment(library, trusted_sample_plugins, qtbot, tmp_path):
    """创建包含两个命名变体引用的真实库，并管理独立检查页面的生命周期。"""
    manager, first = library
    character = manager.add_character(name="另一个角色", node_type="character")
    source = tmp_path / "second"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=2\n", encoding="utf-8")
    second = manager.import_mod(source_path=str(source), node_id=character.id)
    preset = manager.preset_manager.create_preset(
        "plugin-test", "检查组合", {first.node_id: [first.mod_id], second.node_id: [second.mod_id]}
    )
    preset.mod_variants.update({first.mod_id: "missing-first", second.mod_id: "missing-second"})
    plugins = PluginManager(manager)
    assert plugins.load_all() == 1
    context = plugins.get("example.preset-audit").context
    page_class = example_module("example.preset-audit")["PresetAuditPage"]
    pages = []

    def create_page():
        """在读取观察器就绪后才创建页面，以覆盖构造期间的隐式磁盘访问。"""
        page = page_class(context)
        qtbot.addWidget(page)
        pages.append(page)
        return page

    yield create_page, context, manager, preset
    for page in pages:
        page.stop()
        page.close()
        page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    plugins.shutdown()


def test_hidden_audit_defers_reads_and_visible_scan_yields_between_mods(audit_environment, monkeypatch, qtbot, qapp):
    create_page, context, _, preset = audit_environment
    timeline = []
    original = context.variants.get_variants

    def read_variants(mod_id):
        """保留真实文件查询，并记录下一轮事件循环能否在后续查询前运行。"""
        timeline.append(mod_id)
        QTimer.singleShot(0, lambda: timeline.append("heartbeat"))
        return original(mod_id)

    monkeypatch.setattr(context.variants, "get_variants", read_variants)
    page = create_page()
    page.schedule_refresh(game_id="plugin-test")
    qapp.processEvents()
    assert timeline == []
    page.show()
    summary = page.findChild(QLabel, "presetAuditSummary")
    qtbot.waitUntil(lambda: "发现 2 项失效引用" in summary.text())
    reads = [item for item in timeline if item != "heartbeat"]
    assert len(reads) == 2
    assert timeline.index("heartbeat") < timeline.index(reads[1])

    page.hide()
    timeline.clear()
    preset.mod_variants = {mod_id: None for mod_id in preset.enabled_mod_ids}
    page.schedule_refresh(game_id="plugin-test")
    qapp.processEvents()
    assert timeline == []
    page.show()
    qtbot.waitUntil(lambda: "发现 0 项失效引用" in summary.text())
    assert page.findChild(QTableWidget).rowCount() == 0


@pytest.mark.parametrize("interruption", ["game", "stop"])
def test_audit_discards_remaining_reads_after_game_change_or_stop(
    audit_environment, monkeypatch, qtbot, qapp, interruption
):
    create_page, context, manager, _ = audit_environment
    reads = []
    interrupted = []
    original = context.variants.get_variants

    def interrupt():
        """在两次文件查询之间切换游戏或停止页面。"""
        if interruption == "game":
            manager.set_game("other")
            page.schedule_refresh(game_id="other")
        else:
            page.stop()
        interrupted.append(True)

    def read_variants(mod_id):
        """查询首个 Mod 后安排真实的 Qt 回调中断后续批次。"""
        reads.append(mod_id)
        if len(reads) == 1:
            QTimer.singleShot(0, interrupt)
        return original(mod_id)

    monkeypatch.setattr(context.variants, "get_variants", read_variants)
    page = create_page()
    page.show()
    qtbot.waitUntil(lambda: bool(interrupted))
    qapp.processEvents()
    assert len(reads) == 1
    if interruption == "game":
        summary = page.findChild(QLabel, "presetAuditSummary")
        qtbot.waitUntil(lambda: "发现 0 项失效引用" in summary.text())
        assert page.findChild(QTableWidget).rowCount() == 0
