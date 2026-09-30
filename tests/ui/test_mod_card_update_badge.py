from src.models.mod import Mod
from src.ui.widgets.mod_card import ModCard


def _mod():
    return Mod(mod_id="mid1", name="示例角色丁旗袍", folder_name="delta", path="", content_hash="h", node_id="c1")


def test_badge_hidden_by_default(qtbot):
    c = ModCard(mod=None, card_width=200, card_height=280)
    qtbot.addWidget(c)
    c.set_mod(_mod())
    assert not c._update_badge.isVisible()


def test_badge_shows_text_on_large_card(qtbot):
    c = ModCard(mod=None, card_width=200, card_height=280)
    qtbot.addWidget(c)
    c.set_mod(_mod())
    c.show()
    qtbot.waitExposed(c)
    c.set_update_available(True)
    assert c._update_badge.isVisible()
    assert "可更新" in c._update_badge.text()


def test_badge_degrades_to_arrow_then_dot(qtbot):
    c = ModCard(mod=None, card_width=200, card_height=280)
    qtbot.addWidget(c)
    c.set_mod(_mod())
    c.set_update_available(True)
    c.resize_card(150, 160)  # 140<=h<180 → 仅箭头
    assert c._update_badge.text() == ""
    assert not c._update_badge.pixmap().isNull()  # 箭头由 SVG 图标绘制。
    c.resize_card(100, 130)  # h<140 → 退化为纯色圆点（无文字）
    assert c._update_badge.text() == ""
    assert c._update_badge.width() <= 10
