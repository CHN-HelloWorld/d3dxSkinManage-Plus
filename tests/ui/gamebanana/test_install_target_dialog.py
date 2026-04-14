import pytest

from src.models.node import Node
from src.services.gamebanana.install_resolver import InstallTargetResolver, ScoredCandidate
from src.ui.widgets.gb_install_target_dialog import GbInstallTargetDialog


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
