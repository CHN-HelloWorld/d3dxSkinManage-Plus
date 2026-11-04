from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtWidgets import QWidget

from src.models.node import Node
from src.services.gamebanana.install_resolver import InstallTargetResolver, ScoredCandidate
from src.services.gamebanana.models import GbModFile
from src.ui.pages.gamebanana_store import GameBananaStorePage
from src.ui.widgets.gb_install_target_dialog import GbInstallTargetDialog
from tests.qt_dialogs import interact
from tests.ui.test_transient_ui_reuse import _flush


@pytest.fixture
def scored():
    return [
        ScoredCandidate(node_id="c1", display_name="示例角色丑", score=0.82),
        ScoredCandidate(node_id="c2", display_name="示例角色寅", score=0.43),
    ]


def test_list_renders(qtbot, scored):
    d = GbInstallTargetDialog("Sample Mod", scored)
    qtbot.addWidget(d)
    assert d.list_widget.count() == 2


def test_same_named_targets_can_be_selected_by_their_category_path(qtbot):
    nodes = [
        Node(id="characters", name="角色"),
        Node(id="weapons", name="武器"),
        Node(id="c1", name="测试", parent_id="characters"),
        Node(id="c2", name="测试", parent_id="weapons"),
    ]
    best, ranked = InstallTargetResolver().resolve("测试", "test.zip", nodes)
    assert best is None
    d = GbInstallTargetDialog("测试", ranked)
    qtbot.addWidget(d)
    d.search.setText("武器 / 测试")
    visible = [d.list_widget.item(i) for i in range(d.list_widget.count()) if not d.list_widget.item(i).isHidden()]
    assert len(visible) == 1
    d.list_widget.setCurrentItem(visible[0])
    assert d.selected_character_id() == "c2"


def test_select_returns_id(qtbot, scored):
    d = GbInstallTargetDialog("x", scored)
    qtbot.addWidget(d)
    d.list_widget.setCurrentRow(0)
    assert d.selected_character_id() == "c1"


def test_install_planning_consumes_target_then_releases_picker(qapp, temp_data_dir, monkeypatch, scored):
    from src.core import mod_manager as mod_manager_module

    manager = SimpleNamespace(current_game_id="game", get_characters=lambda: [])
    monkeypatch.setattr(mod_manager_module, "ModManager", lambda: manager)
    monkeypatch.setattr(InstallTargetResolver, "resolve", lambda *_args, **_kwargs: (None, scored))
    file = GbModFile(
        file_id=11,
        file_name="Example.zip",
        description="",
        download_url="",
        size_bytes=1,
        date_added_ts=1,
        md5_checksum="md5",
        av_result="clean",
    )
    profile = SimpleNamespace(
        name="Example",
        category_name="",
        super_category_id=1,
        mod_id=7,
        page_url="",
        submitter=SimpleNamespace(id=2, name="Author"),
        preview_images=[],
        is_nsfw=False,
    )
    host = QWidget()
    payloads = []
    host._backfill_character_name_en = lambda *_args: None
    host._start_install = payloads.append
    dialogs = []

    def accept(dialog):
        """记录真实目标选择器并确认默认候选，使调用方继续消费选择结果。"""
        dialogs.append(dialog)
        dialog.accept()

    try:
        for _ in range(4):
            with interact(GbInstallTargetDialog, accept):
                GameBananaStorePage._prepare_download_install(host, file, "archive.zip", profile)
            _flush(qapp)

        assert [payload["node_id"] for payload in payloads] == ["c1"] * 4
        assert not host.findChildren(GbInstallTargetDialog)
        assert all(not shiboken6.isValid(dialog) for dialog in dialogs)
    finally:
        host.deleteLater()
        _flush(qapp)
