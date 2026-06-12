import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QLabel, QLineEdit

from src.ui.widgets.cache_cap_slider import CacheCapSlider


@pytest.fixture
def cap(qtbot):
    widget = CacheCapSlider()
    qtbot.addWidget(widget)
    return widget


def signals(cap):
    values, ranges = [], []
    cap.value_changed.connect(values.append)
    cap.range_changed.connect(ranges.append)
    return values, ranges


def label(cap, text):
    matches = [item for item in cap.findChildren(QLabel) if item.text() == text]
    assert matches, f"Missing visible label: {text}"
    return matches[0]


def bounds(cap, widget):
    return QRect(widget.mapTo(cap, QPoint()), widget.size())


def test_loading_configuration_is_silent_and_clamps_value(cap):
    values, ranges = signals(cap)
    cap.set_range_max_mb(8192)
    cap.set_value_mb(16384)
    assert cap.value_mb() == cap._slider.value() == 8192
    cap.set_range_max_mb(1024)
    assert cap.value_mb() == 1024
    assert values == ranges == []


def test_slider_changes_quota_only_and_zero_is_unlimited(cap, qtbot):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(1024)
    values, ranges = signals(cap)
    qtbot.keyClick(cap._slider, Qt.Key.Key_Right)
    assert cap.value_mb() == 1088
    assert cap.range_max_mb() == 4096
    qtbot.keyClick(cap._slider, Qt.Key.Key_Home)
    assert cap.value_mb() == 0
    assert values == [1088, 0]
    assert ranges == []
    assert label(cap, "不限制")


@pytest.mark.parametrize("old_value, expected_values", [(3072, [2048]), (1024, [])])
def test_committing_range_notifies_once_and_clamps_only_if_needed(cap, old_value, expected_values):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(old_value)
    values, ranges = signals(cap)
    cap._edit.setText("2")
    cap._edit.editingFinished.emit()
    assert cap.range_max_mb() == cap._slider.maximum() == 2048
    assert cap.value_mb() == min(old_value, 2048)
    assert ranges == [2048]
    assert values == expected_values


def test_keyboard_submission_accepts_validator_grouped_number(cap, qtbot):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(2048)
    cap._unit_combo.setCurrentIndex(cap._unit_combo.findData("MB"))
    values, ranges = signals(cap)
    cap.show()
    cap._edit.setFocus()
    cap._edit.selectAll()
    qtbot.keyClicks(cap._edit, "1,024")
    assert cap._edit.text() == "1,024"
    assert cap._edit.hasAcceptableInput()
    qtbot.keyClick(cap._edit, Qt.Key.Key_Return)
    assert cap.range_max_mb() == 1024
    assert cap.value_mb() == 1024
    assert cap._edit.text() == "1024"
    assert ranges == [1024]
    assert values == [1024]


@pytest.mark.parametrize("text, expected", [("0", 64), ("-3", 64), ("", 64), ("oops", 64), ("2000000000", 1073741824)])
def test_range_input_clamps_to_supported_limits(cap, text, expected):
    cap._unit_combo.setCurrentIndex(cap._unit_combo.findData("MB"))
    cap._edit.setText(text)
    cap._on_edit_committed()
    assert cap.range_max_mb() == expected


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "1e999"])
def test_non_finite_input_is_rejected_without_losing_configuration(cap, text):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(1024)
    values, ranges = signals(cap)
    cap._edit.setText(text)
    cap._on_edit_committed()
    assert cap.range_max_mb() == 4096
    assert cap.value_mb() == 1024
    assert cap._edit.text() == "4"
    assert values == ranges == []


@pytest.mark.parametrize("range_mb, displayed_tb", [(2048, "0.002"), (64, "0"), (1048577, "1")])
def test_unit_switch_and_unedited_focus_loss_do_not_save_rounded_display(cap, range_mb, displayed_tb):
    cap.set_range_max_mb(range_mb)
    cap.set_value_mb(32)
    values, ranges = signals(cap)
    cap._unit_combo.setCurrentIndex(cap._unit_combo.findData("TB"))
    assert cap._edit.text() == displayed_tb
    cap._edit.editingFinished.emit()
    assert cap.range_max_mb() == range_mb
    assert cap.value_mb() == 32
    assert values == ranges == []


def test_usage_updates_notify_parent_without_mutating_quota_or_displaying_total(cap):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(2048)
    values, ranges = signals(cap)
    usage = []
    cap.usage_changed.connect(usage.append)
    for amount in (-1.0, 12345.5):
        cap.set_current_usage_mb(amount)
    assert usage == [-1.0, 12345.5]
    assert cap.range_max_mb() == 4096
    assert cap.value_mb() == 2048
    assert values == ranges == []
    assert all("占用：" not in item.text() for item in cap.findChildren(QLabel))


@pytest.mark.parametrize("width", [260, 360, 449, 450, 710])
def test_quota_controls_remain_visible_and_range_row_reflows(cap, qtbot, width):
    cap.set_range_max_mb(4096)
    cap.set_value_mb(2048)
    cap.resize(width, 360)
    cap.show()
    qtbot.wait(10)
    assert cap.width() == width
    assert isinstance(cap._edit, QLineEdit)
    title = label(cap, "在线图片缓存上限")
    range_title = label(cap, "滑块最大值")
    readout = label(cap, "2 GB")
    for widget in [title, range_title, readout, cap._slider, cap._edit, cap._unit_combo]:
        assert widget.isVisible()
        assert widget.width() > 0
        assert cap.rect().contains(bounds(cap, widget)), (width, widget, bounds(cap, widget))
    assert cap._slider.width() >= width - 8
    assert bounds(cap, readout).bottom() < bounds(cap, cap._slider).top()
    assert bounds(cap, cap._slider).bottom() < bounds(cap, range_title).top()
    if width < 450:
        assert bounds(cap, cap._edit).top() > bounds(cap, range_title).bottom()
    else:
        assert bounds(cap, cap._edit).left() > bounds(cap, range_title).right()
    cap._slider.setValue(3072)
    qtbot.wait(1)
    assert bounds(cap, label(cap, "3 GB")).right() == bounds(cap, readout).right()


def test_retheme_preserves_configuration_and_layout_size_hint(cap):
    cap.set_range_max_mb(8192)
    cap.set_value_mb(2048)
    values, ranges = signals(cap)
    cap.retheme()
    assert cap.sizeHint().width() >= 260
    assert cap.minimumSizeHint().width() >= 260
    assert cap.sizeHint().height() > 0
    assert cap.range_max_mb() == 8192
    assert cap.value_mb() == 2048
    assert values == ranges == []
