"""用真实字体和光栅绘制验证标签在分数宽度下不丢字，并保留合理省略。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QPainter
from PySide6.QtWidgets import QWidget

from src.models.tag import TagDefinition
from src.ui.widgets import tag_chip
from src.ui.widgets.tag_chip import TagChip, TagFlowLayout


@pytest.fixture
def chip_rendering(qapp, qtbot, monkeypatch):
    """使用实际微软雅黑和流式布局，记录绘制参数的同时仍由 QPainter 生成真实像素。"""
    family = "Microsoft YaHei UI"
    if family not in QFontDatabase.families():
        pytest.skip("此字体回归要求实际安装 Microsoft YaHei UI")
    host = QWidget()
    qtbot.addWidget(host)
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    font = QFont(family)
    font.setPixelSize(11)
    host.setFont(font)
    host.resize(420, 80)
    layout = TagFlowLayout(host)
    records = []
    state = {"hide_name": False}

    class RecordingPainter(QPainter):
        """仅观察文本绘制边界，保留正式绘制路径、字体、裁剪及设备缩放。"""

        def __init__(self, device):
            """记录本轮是否已绘制名称，数量文字不受空白对照图影响。"""
            super().__init__(device)
            self._first_text = True

        def drawText(self, *args):
            """保存名称或数量的最终绘制文本，并为像素对照按需隐藏名称。"""
            records.append((QRect(args[0]), args[-1]))
            hide = self._first_text and state["hide_name"]
            self._first_text = False
            if not hide:
                return super().drawText(*args)
            return None

    monkeypatch.setattr(tag_chip, "QPainter", RecordingPainter)

    def create(name, *, font_size=11, **options):
        """把标签交给真实流式布局按推荐尺寸分配，避免顶层窗口最小宽度掩盖缺陷。"""
        chip = TagChip(TagDefinition("tag", "game", name, name, "blue"), host, font_size=font_size, **options)
        layout.addWidget(chip)
        host.show()
        qapp.processEvents()
        assert QFontInfo(chip.font()).family() == family
        return chip

    def render(chip, *, hide_name=False):
        """捕获实际像素和本次绘制文本，不依赖生产代码的省略计算生成预期值。"""
        records.clear()
        state["hide_name"] = hide_name
        image = chip.grab().toImage()
        assert not image.isNull()
        return bytes(image.constBits()), list(records)

    return create, render


@pytest.mark.parametrize("name", ["LL", "sd", "LLLl", "ls", "li", "中文"])
@pytest.mark.parametrize("font_size", [10, 11])
@pytest.mark.parametrize("options", [{}, {"count": 1234}, {"removable": True}, {"count": 1234, "removable": True}])
def test_short_tag_draws_complete_name_at_its_layout_width(chip_rendering, name, font_size, options):
    """推荐尺寸必须完整显示短名称；计数和移除入口不能挤掉名称或退化为空白。"""
    create, render = chip_rendering
    chip = create(name, font_size=font_size, **options)
    pixels, calls = render(chip)
    assert calls[0][1] == name
    assert [text for _rect, text in calls[1:]] == (["1234"] if "count" in options else [])
    blank_pixels, _ = render(chip, hide_name=True)
    assert pixels != blank_pixels
    if chip._remove_button is not None:
        assert calls[-1][0].right() < chip._remove_button.geometry().left()


@pytest.mark.parametrize("options", [{}, {"count": 1234}, {"removable": True}, {"count": 1234, "removable": True}])
def test_long_tag_still_elides_with_count_and_remove_space_preserved(chip_rendering, qapp, options):
    """短标签修复不能取消窄区域中长名称的省略，也不能覆盖计数和删除按钮。"""
    create, render = chip_rendering
    name = "LongTag" * 9
    chip = create(name, **options)
    chip.setMaximumWidth(100)
    chip.updateGeometry()
    qapp.processEvents()
    _pixels, calls = render(chip)
    text = calls[0][1]
    assert text and text != name and text.endswith("…")
    assert chip.width() <= 100
    assert [text for _rect, text in calls[1:]] == (["1234"] if "count" in options else [])
    if chip._remove_button is not None:
        assert calls[-1][0].right() < chip._remove_button.geometry().left()


def test_tag_layout_remeasures_after_font_style_change(chip_rendering, qapp):
    """复用标签改字体样式后重新分配足够宽度，不沿用旧字体的整数尺寸。"""
    create, render = chip_rendering
    chip = create("LL", count=1234, removable=True)
    initial_width = chip.width()
    chip.setStyleSheet("TagChip { font-size: 16px; font-weight: bold; }")
    qapp.processEvents()
    _pixels, calls = render(chip)
    assert chip.width() > initial_width
    assert [text for _rect, text in calls] == ["LL", "1234"]
