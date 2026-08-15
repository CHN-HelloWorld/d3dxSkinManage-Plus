from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtWidgets import QLabel

from src.models.tag import TagDefinition, tag_key
from src.ui import theme
from src.ui.widgets.tag_chip import TagChip
from src.ui.widgets.tag_display import TagStrip


def tag(tag_id, name, color="blue"):
    return TagDefinition(tag_id, "game", name, tag_key(name), color)


def visible_chips(strip):
    # 向可见布局插入控件后，Qt 会排队处理其显示状态。
    QCoreApplication.processEvents()
    return [
        strip.layout().itemAt(index).widget()
        for index in range(strip.layout().count())
        if isinstance(strip.layout().itemAt(index).widget(), TagChip)
        and not strip.layout().itemAt(index).widget().isHidden()
    ]


def owned_chips(strip):
    return strip.findChildren(TagChip, options=Qt.FindChildOption.FindDirectChildrenOnly)


def empty_label(strip):
    return next(
        label
        for label in strip.findChildren(QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if label.text() == "暂无标签"
    )


def make_strip(qtbot, tags=(), *, limit=2):
    strip = TagStrip(limit=limit)
    qtbot.addWidget(strip)
    strip.resize(360, 240)
    strip.set_tags(tags)
    strip.show()
    QCoreApplication.processEvents()
    return strip


class EventRecorder(QObject):
    def __init__(self, widgets):
        super().__init__()
        self.events = []
        for widget in widgets:
            widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ParentChange, QEvent.Type.StyleChange):
            self.events.append((watched, event.type()))
        return False


@pytest.mark.parametrize("limit", [1, 2, None])
def test_rebinding_retains_slots_and_emits_only_current_tag_ids(qtbot, limit):
    strip = make_strip(qtbot, [tag("a", "Alpha"), tag("b", "Beta"), tag("c", "Gamma")], limit=limit)
    slots = visible_chips(strip)
    clicked = []
    strip.tag_clicked.connect(clicked.append)
    expected_clicks = []

    for prefix, color in [("next", "mint"), ("third", "purple"), ("last", "amber")]:
        tags = [tag(f"{prefix}-{index}", f"<{prefix}> tag {index}", color) for index in range(3)]
        strip.set_tags(tags)
        assert visible_chips(strip) == slots
        for chip, definition in zip(slots, tags):
            assert chip.parentWidget() is strip
            assert chip.tag.id == definition.id
            assert chip.tag.color == color
            assert chip.text() == definition.name
            assert chip.toolTip() == definition.name
            assert chip.accessibleName() == f"标签 {definition.name}"
            qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
            expected_clicks.append(definition.id)
        assert clicked == expected_clicks, "rebinding must not retain IDs or duplicate signal connections"


def test_same_slot_count_keeps_layout_items_and_does_not_restyle(qtbot):
    strip = make_strip(qtbot, [tag(str(index), f"First {index}") for index in range(4)])
    items = [strip.layout().itemAt(index) for index in range(strip.layout().count())]
    slots = visible_chips(strip)
    recorder = EventRecorder([*slots, strip._more_button])

    strip.set_tags([tag(f"new-{index}", f"Second {index}", "mint") for index in range(4)])

    assert recorder.events == [], "a data rebind must not reparent chips or repolish unchanged styles"
    assert [strip.layout().itemAt(index) for index in range(strip.layout().count())] == items
    assert visible_chips(strip) == slots
    assert strip._more_button.text() == "+2"
    assert strip._more_button.toolTip() == "还有 2 个标签，点击查看全部"


def test_limit_and_empty_transitions_reuse_a_bounded_pool_without_reparenting(qtbot):
    tags = [tag(str(index), f"Tag {index}") for index in range(30)]
    strip = make_strip(qtbot, tags, limit=1)
    first = visible_chips(strip)[0]
    assert len(owned_chips(strip)) == 1, "compact rows must not allocate the overflow tags"
    strip.set_limit(3)
    slots = visible_chips(strip)
    assert slots[0] is first
    assert len(owned_chips(strip)) == 3
    recorder = EventRecorder(slots)

    strip.set_limit(1)
    assert visible_chips(strip) == slots[:1]
    assert strip._more_button.text() == "+29"
    assert len(owned_chips(strip)) == 3
    assert all(chip.isHidden() for chip in slots[1:])

    strip.set_tags([])
    placeholder = empty_label(strip)
    assert not placeholder.isHidden()
    assert visible_chips(strip) == []
    assert strip._more_button.isHidden()
    assert all(chip.isHidden() and chip.parentWidget() is strip for chip in slots)

    replacement = [tag("new-a", "New Alpha"), tag("new-b", "New Beta", "purple")]
    strip.set_tags(replacement)
    strip.set_limit(2)
    assert visible_chips(strip) == slots[:2]
    assert placeholder.isHidden()
    assert strip._more_button.isHidden()
    assert [chip.tag.id for chip in visible_chips(strip)] == ["new-a", "new-b"]

    strip.set_limit(0)
    assert not placeholder.isHidden()
    assert visible_chips(strip) == []
    assert not strip._more_button.isHidden()
    assert strip._more_button.text() == "+2"
    strip.set_limit(3)
    assert visible_chips(strip) == slots[:2]
    strip.set_tags([])
    assert empty_label(strip) is placeholder
    assert len(owned_chips(strip)) == 3
    assert recorder.events == []


def test_unrestricted_wrapping_ignores_hidden_pool_slots_and_reuses_them(qtbot):
    tags = [tag(str(index), f"Long tag {index}") for index in range(8)]
    strip = make_strip(qtbot, tags, limit=None)
    strip.resize(130, 400)
    strip.layout().setGeometry(strip.rect())
    slots = visible_chips(strip)
    full_height = strip.layout().heightForWidth(130)
    assert len(slots) == 8
    assert len({chip.y() for chip in slots}) > 1
    assert strip._more_button.isHidden()

    strip.set_tags([tag("one", "One"), tag("two", "Two")])
    strip.layout().setGeometry(strip.rect())
    assert visible_chips(strip) == slots[:2]
    assert len(owned_chips(strip)) == 8
    assert strip.layout().heightForWidth(130) < full_height
    assert all(chip.isHidden() for chip in slots[2:])

    strip.set_tags(tags[:6])
    strip.layout().setGeometry(strip.rect())
    assert visible_chips(strip) == slots[:6]
    assert len(owned_chips(strip)) == 8
    assert all(chip.maximumWidth() == 260 for chip in visible_chips(strip))


def test_reused_chip_updates_mutated_data_and_repaints_color_and_theme(qtbot):
    previous_theme = theme.current_theme()
    try:
        theme.set_theme("light")
        definition = tag("original", "Original", "blue")
        strip = make_strip(qtbot, [definition, tag("hidden", "Overflow")], limit=1)
        chip = visible_chips(strip)[0]
        more_style = strip._more_button.styleSheet()

        def background():
            pixmap = chip.grab()
            image = pixmap.toImage()
            return image.pixelColor(image.width() // 2, round(2 * pixmap.devicePixelRatio())).name()

        assert background() == "#e7f1fa"
        original_signature = strip._render_signature
        definition.id, definition.name, definition.color = "changed", "Renamed", "purple"
        definition.normalized_name = "renamed"
        strip.set_tags([definition, tag("hidden", "Overflow")])
        assert visible_chips(strip)[0] is chip
        assert strip._render_signature != original_signature
        assert chip.text() == "Renamed"
        assert chip.toolTip() == "Renamed"
        assert chip.accessibleName() == "标签 Renamed"
        assert background() == "#f0eafa"

        theme.set_theme("dark")
        strip.set_tags([definition, tag("hidden", "Overflow")])
        assert visible_chips(strip)[0] is chip
        assert background() == "#393143"
        assert strip._more_button.styleSheet() != more_style
    finally:
        theme.set_theme(previous_theme)


def test_empty_state_rethemes_without_replacing_its_label(qtbot):
    previous_theme = theme.current_theme()
    try:
        theme.set_theme("light")
        strip = make_strip(qtbot)
        placeholder = empty_label(strip)
        light_style = placeholder.styleSheet()
        theme.set_theme("dark")
        strip.set_tags([])
        assert empty_label(strip) is placeholder
        assert placeholder.styleSheet() != light_style
        recorder = EventRecorder([placeholder])
        strip.set_limit(3)
        assert recorder.events == []
        assert empty_label(strip) is placeholder
    finally:
        theme.set_theme(previous_theme)


def test_rebinding_still_closes_popup_even_when_the_signature_is_unchanged(qtbot):
    tags = [tag("a", "Alpha"), tag("b", "Beta"), tag("c", "Gamma")]
    strip = make_strip(qtbot, tags, limit=1)
    chip = visible_chips(strip)[0]
    strip._more_button.click()
    popup = strip._popup
    assert popup.isVisible()
    destroyed = []
    popup.destroyed.connect(lambda: destroyed.append(True))

    strip.set_tags(tags)
    assert not popup.isVisible()
    assert strip._popup is None
    assert visible_chips(strip)[0] is chip
    qtbot.waitUntil(lambda: bool(destroyed))

    replacement = [tag("new-a", "Changed Alpha"), tag("new-b", "Changed Beta")]
    strip.set_tags(replacement)
    clicked = []
    strip.tag_clicked.connect(clicked.append)
    strip._more_button.click()
    popup = strip._popup
    popup_chips = popup.findChildren(TagChip)
    assert [item.tag.id for item in popup_chips] == ["new-a", "new-b"]
    qtbot.mouseClick(popup_chips[-1], Qt.MouseButton.LeftButton)
    assert clicked == ["new-b"]
    assert not popup.isVisible()


def test_card_rebinding_and_resizing_keep_slots_and_current_tag_actions(qtbot, temp_data_dir):
    from src.models.mod import Mod, ModInfo
    from src.ui.widgets.mod_card import ModCard

    first_tags = [tag("first-a", "Alpha"), tag("first-b", "Beta"), tag("first-c", "Gamma")]
    card = ModCard(card_width=160, card_height=200)
    qtbot.addWidget(card)
    card.set_tag_catalog({item.normalized_name: item for item in first_tags})
    card.set_mod_deferred(Mod(mod_id="first-mod", name="First", info=ModInfo(tags=[item.name for item in first_tags])))
    card.show()
    first_slot = visible_chips(card._tag_row)[0]
    tag_actions, card_actions = [], []
    card.tag_clicked.connect(tag_actions.append)
    card.clicked.connect(card_actions.append)
    card.double_clicked.connect(card_actions.append)
    card.toggle_requested.connect(card_actions.append)

    next_tags = [tag("next-a", "Changed Alpha", "mint"), tag("next-b", "Changed Beta", "purple")]
    card.set_tag_catalog({item.normalized_name: item for item in next_tags})
    card.set_mod_deferred(Mod(mod_id="next-mod", name="Next", info=ModInfo(tags=[item.name for item in next_tags])))
    assert visible_chips(card._tag_row) == [first_slot]
    assert first_slot.accessibleName() == "标签 Changed Alpha"
    assert card._tag_row._more_button.text() == "+1"
    qtbot.mouseClick(first_slot, Qt.MouseButton.LeftButton)
    qtbot.mouseDClick(first_slot, Qt.MouseButton.LeftButton)
    assert tag_actions == ["next-a"]
    assert card_actions == []

    card.resize_card_fast(240, 300)
    expanded_slots = visible_chips(card._tag_row)
    assert expanded_slots[0] is first_slot
    assert [chip.tag.id for chip in expanded_slots] == ["next-a", "next-b"]
    assert card._tag_row._more_button.isHidden()
    assert card.height() == 300
    card.resize_card_fast(160, 200)
    assert visible_chips(card._tag_row) == [first_slot]
    assert len(owned_chips(card._tag_row)) == 2
    assert card.height() == 200
