import time
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QPainter, QPixmapCache
from PySide6.QtWidgets import QListWidget, QStackedWidget, QWidget

from src.ui import anim, theme
from src.ui.backgrounds.foreground import capture_foreground
from src.ui.widgets.background_overlay import BackgroundOverlay


class _Marker(QWidget):
    def __init__(self, color, parent):
        super().__init__(parent)
        self.color = QColor(color)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)


@pytest.fixture
def pages(qtbot, qapp, monkeypatch):
    monkeypatch.setattr(anim, "_enabled", True)
    previous_sheet = qapp.styleSheet()
    qapp.setStyleSheet(theme.render_qss("light") + theme.background_overlay_qss("light"))
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(700, 500)
    background = BackgroundOverlay(root)
    background.setGeometry(root.rect())
    stack = QStackedWidget(root)
    stack.setGeometry(70, 50, 560, 390)
    for color, x, y in [("red", 20, 30), ("green", 310, 210), ("blue", 420, 40)]:
        page = QWidget()
        marker = _Marker(color, page)
        marker.setGeometry(x, y, 40, 40)
        stack.addWidget(page)
    root.show()
    qtbot.wait(20)
    yield root, background, stack
    root.close()
    qapp.setStyleSheet(previous_sheet)


@pytest.mark.parametrize("native_root", [False, True])
def test_transition_snapshot_keeps_page_coordinates(pages, native_root):
    root, background, stack = pages
    if native_root:
        root.winId()
    QPixmapCache.clear()
    animation = anim.cross_fade(stack, lambda: stack.setCurrentIndex(1))
    animation.pause()
    image = capture_foreground(background)
    ratio = image.devicePixelRatio()
    assert image.pixelColor(round(100 * ratio), round(90 * ratio)) == QColor("red"), (
        "the first transition frame must draw the old page at its original coordinates"
    )
    assert image.pixelColor(round(170 * ratio), round(140 * ratio)).alpha() == 0, (
        "the snapshot must not repeat the stack position offset"
    )


@pytest.mark.parametrize("disable_animation", [False, True])
def test_interrupted_transition_hides_old_snapshot_before_switch(pages, qtbot, disable_animation):
    _root, background, stack = pages
    anim.cross_fade(stack, lambda: stack.setCurrentIndex(1))
    previous = stack._cross_fade_overlay
    qtbot.wait(20)
    if disable_animation:
        anim.set_enabled(False)
    hidden_at_switch = []

    def switch():
        hidden_at_switch.append(previous.isHidden())
        stack.setCurrentIndex(2)

    anim.cross_fade(stack, switch)
    assert hidden_at_switch == [True], "deleteLater alone leaves the old page in the next capture"
    image = capture_foreground(background)
    ratio = image.devicePixelRatio()
    assert image.pixelColor(round(100 * ratio), round(90 * ratio)).alpha() == 0


def test_expired_transition_does_not_reappear_after_gui_stall(pages):
    _root, background, stack = pages
    anim.cross_fade(stack, lambda: stack.setCurrentIndex(1), duration=80)
    # 暂不分发 Qt 事件，动画时钟尚未推进。
    time.sleep(0.12)
    image = capture_foreground(background)
    ratio = image.devicePixelRatio()
    for x, y in ((100, 90), (170, 140)):
        assert image.pixelColor(round(x * ratio), round(y * ratio)).alpha() == 0, (
            "an expired snapshot must not be replayed when the GUI resumes"
        )


def test_transition_end_hides_snapshot_synchronously(pages):
    _root, background, stack = pages
    animation = anim.cross_fade(stack, lambda: stack.setCurrentIndex(1))
    snapshot = stack._cross_fade_overlay
    animation.setCurrentTime(animation.duration())
    assert snapshot.isHidden()
    assert stack._cross_fade_overlay is None
    image = capture_foreground(background)
    ratio = image.devicePixelRatio()
    assert image.pixelColor(round(100 * ratio), round(90 * ratio)).alpha() == 0


@pytest.mark.parametrize("change", ["resize", "hide", "direct_switch"])
def test_obsolete_container_retires_transition(pages, change):
    _root, _background, stack = pages
    anim.cross_fade(stack, lambda: stack.setCurrentIndex(1))
    snapshot = stack._cross_fade_overlay
    if change == "resize":
        stack.resize(570, 400)
    elif change == "hide":
        stack.hide()
    else:
        stack.setCurrentIndex(2)
    assert snapshot.isHidden(), "a snapshot must not outlive its page or layout"
    assert stack._cross_fade_overlay is None


@pytest.mark.parametrize("change", ["rapid_return", "restore_selection", "hide_then_return"])
def test_navigation_indicator_follows_latest_selection(qtbot, monkeypatch, change):
    from src.ui.main_window import MainWindow

    monkeypatch.setattr(anim, "_enabled", True)
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(240, 400)
    nav = QListWidget(root)
    nav.setGeometry(0, 0, 220, 380)
    nav.addItems(["Game", "Overview", "Gallery", "Store", "Settings"])
    indicator = QWidget(nav.viewport())
    indicator.hide()
    owner = SimpleNamespace(_nav=nav, _nav_indicator=indicator)
    nav.currentRowChanged.connect(lambda _row: MainWindow._update_nav_indicator(owner))
    root.show()
    qtbot.wait(20)
    nav.setCurrentRow(2)
    nav.setCurrentRow(4)
    if change == "rapid_return":
        nav.setCurrentRow(1)
        nav.setCurrentRow(2)
    elif change == "restore_selection":
        nav.blockSignals(True)
        nav.setCurrentRow(2)
        nav.blockSignals(False)
        MainWindow._update_nav_indicator(owner, animate=False)
    else:
        nav.setCurrentRow(-1)
        nav.setCurrentRow(2)
    expected_y = nav.visualItemRect(nav.item(2)).center().y()
    qtbot.wait(anim.DURATION["fast"] + 60)
    assert abs(indicator.geometry().center().y() - expected_y) <= 1, (
        "a previous animation must not move the indicator away from the selected page"
    )
