from __future__ import annotations

from itertools import count
from threading import get_ident

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from src.ui import background_style

_QSS = """
QWidget#canvas { background: #123456; }
QFrame#card[enabled] { background: #cc4422; border: none; }
QFrame#card[_backgroundSurface="true"] { background: transparent; }
QPushButton#button[enabled] {
    background: #ddeeff; color: #334455; border: 2px solid #778899;
}
QPushButton#button[_backgroundSurface="true"] { background: transparent; }
"""


@pytest.fixture
def controller(qapp, monkeypatch):
    # 设置测试时钟前清空前一用例的 DeferredDelete，避免其影响本轮分批预算。
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    previous = getattr(qapp, "_background_style", None)
    if previous is not None and shiboken6.isValid(previous):
        qapp.removeEventFilter(previous)
    monkeypatch.setattr(qapp, "_background_style", None, raising=False)
    old_sheet, old_font = qapp.styleSheet(), QFont(qapp.font())
    old_inheritance = getattr(qapp, "_font_inheritance", None)
    qapp.setStyleSheet(_QSS)
    current = background_style.install_background_style(qapp)
    yield current
    current.apply(False)
    current.dispose()
    inheritance = getattr(qapp, "_font_inheritance", None)
    if old_inheritance is None and inheritance is not None:
        qapp.removeEventFilter(inheritance)
        inheritance.deleteLater()
        qapp._font_inheritance = None
    qapp.setStyleSheet("")
    qapp.setFont(old_font)
    qapp.setStyleSheet(old_sheet)
    if previous is not None and shiboken6.isValid(previous):
        qapp.installEventFilter(previous)
        previous.apply(previous.enabled)


def _surface(qtbot):
    canvas = QWidget()
    canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    canvas.setObjectName("canvas")
    canvas.resize(220, 110)
    qtbot.addWidget(canvas)
    card = QFrame(canvas)
    card.setObjectName("card")
    card.setGeometry(10, 10, 90, 90)
    button = QPushButton("", canvas)
    button.setObjectName("button")
    button.setGeometry(120, 10, 90, 90)
    canvas.show()
    return canvas, card, button


def _colors(canvas):
    image = canvas.grab().toImage()
    ratio = canvas.devicePixelRatioF()
    return [image.pixelColor(round(x * ratio), round(50 * ratio)) for x in (50, 160)]


class _ObservedLabel(QLabel):
    def __init__(self, parent=None):
        self.style_changes = []
        super().__init__("Styled text", parent)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.StyleChange:
            self.style_changes.append(get_ident())
        super().changeEvent(event)


def _batch_widgets(qtbot, qapp):
    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(parent)
    labels = [_ObservedLabel(parent) for _ in range(20)]
    parent.ensurePolished()
    for label in labels:
        label.ensurePolished()
    qapp.processEvents()
    return parent, labels


def _one_widget_per_batch(monkeypatch):
    clock = count()
    monkeypatch.setattr(background_style, "perf_counter", lambda: next(clock) / 100)


def test_toggle_restyles_real_surfaces_without_replacing_stylesheet(qtbot, qapp, controller, monkeypatch):
    canvas, card, button = _surface(qtbot)
    qapp.processEvents()
    assert _colors(canvas) == [QColor("#cc4422"), QColor("#ddeeff")]

    def forbid_global_sheet(_sheet):
        raise AssertionError("background toggles must not reset the application stylesheet")

    with monkeypatch.context() as patch:
        patch.setattr(qapp, "setStyleSheet", forbid_global_sheet)
        assert controller.apply(True)
        assert controller.enabled is True
        assert card.property("_backgroundSurface") is True
        assert button.property("_backgroundSurface") is True
        assert _colors(canvas) == [QColor("#123456"), QColor("#123456")]
        assert controller.apply(False)
        assert controller.enabled is False
        assert _colors(canvas) == [QColor("#cc4422"), QColor("#ddeeff")]
    assert qapp.styleSheet() == _QSS


def test_default_false_does_not_set_properties_or_repolish(qtbot, qapp, controller):
    label = _ObservedLabel()
    label.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(label)
    label.ensurePolished()
    qapp.processEvents()
    label.style_changes.clear()
    assert controller.apply(False) is False
    assert label.property("_backgroundSurface") is None
    assert label.style_changes == []


def test_widgets_created_after_toggle_inherit_current_state(qtbot, qapp, controller):
    controller.apply(True)
    canvas, card, button = _surface(qtbot)
    qapp.processEvents()
    assert card.property("_backgroundSurface") is True
    assert button.property("_backgroundSurface") is True
    assert _colors(canvas) == [QColor("#123456"), QColor("#123456")]
    controller.apply(False)
    later = _ObservedLabel(canvas)
    later.ensurePolished()
    later.show()
    assert later.property("_backgroundSurface") is None


def test_first_polish_inherits_theme_without_synthetic_style_change(qtbot, qapp, controller):
    qapp.setStyleSheet(_QSS + '\nQLabel[_appearanceTheme="dark"] { color: #aabbcc; }')
    controller.apply(True, theme_name="dark")
    label = _ObservedLabel()
    label.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(label)
    label.style_changes.clear()
    label.ensurePolished()
    assert label.property("_appearanceTheme") == "dark"
    assert label.palette().color(label.foregroundRole()) == QColor("#aabbcc")
    assert label.style_changes == []
    canvas, _card, _button = _surface(qtbot)
    assert _colors(canvas) == [QColor("#123456"), QColor("#123456")]


def test_repolish_preserves_local_styles_and_selected_font_attributes(qtbot, qapp, controller):
    from src.ui.fonts import apply_font

    families = QFontDatabase.families()
    family = next(
        (name for name in ("Times New Roman", "DejaVu Serif", "Arial") if name in families), qapp.font().family()
    )
    selected = QFont(qapp.font())
    selected.setFamily(family)
    apply_font(qapp, selected)
    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(parent)
    styled = _ObservedLabel(parent)
    local_sheet = "font-size: 19px; font-weight: bold; color: #334455;"
    styled.setStyleSheet(local_sheet)
    explicit = _ObservedLabel(parent)
    title_font = QFont(selected)
    title_font.setPixelSize(24)
    title_font.setItalic(True)
    explicit.setFont(title_font)
    for label in (styled, explicit):
        label.ensurePolished()
        label.style_changes.clear()
    for enabled in (True, False):
        controller.apply(enabled)
        assert styled.styleSheet() == local_sheet
        assert styled.font().family() == explicit.font().family() == family
        assert styled.font().pixelSize() == 19
        assert styled.font().bold()
        assert explicit.font().pixelSize() == 24
        assert explicit.font().italic()
    assert styled.style_changes == [get_ident(), get_ident()]
    assert explicit.style_changes == [get_ident(), get_ident()]


def test_async_application_yields_between_batches_and_finishes_on_gui(qtbot, qapp, controller, monkeypatch):
    _parent, labels = _batch_widgets(qtbot, qapp)
    _one_widget_per_batch(monkeypatch)
    completed, heartbeats = [], []
    assert controller.apply(True, lambda: completed.append(get_ident()))
    assert controller.enabled is True
    assert all(label.property("_backgroundSurface") is None for label in labels)
    QTimer.singleShot(0, lambda: heartbeats.append(True))
    qtbot.waitUntil(lambda: any(label.property("_backgroundSurface") for label in labels))
    assert not all(label.property("_backgroundSurface") for label in labels)
    assert not completed
    qtbot.waitUntil(lambda: bool(completed), timeout=3000)
    assert heartbeats == [True]
    assert completed == [get_ident()]
    assert all(label.property("_backgroundSurface") is True for label in labels)


def test_async_application_refreshes_visible_widgets_before_hidden_ones(qtbot, qapp, controller, monkeypatch):
    hidden, visible = _ObservedLabel(), _ObservedLabel()
    for widget in (hidden, visible):
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        qtbot.addWidget(widget)
        widget.ensurePolished()
    visible.show()
    qapp.processEvents()
    order, completed = [], []

    class Changes(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.StyleChange:
                order.append(obj)
            return False

    observer = Changes()
    hidden.installEventFilter(observer)
    visible.installEventFilter(observer)
    _one_widget_per_batch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(qapp, "allWidgets", lambda: [hidden, visible])
        controller.apply(True, lambda: completed.append(True))
        qtbot.waitUntil(lambda: bool(completed))
    assert order == [visible, hidden]


@pytest.mark.parametrize("latest", [False, True])
def test_latest_direct_request_supersedes_pending_work_and_callback(
    qtbot,
    qapp,
    controller,
    monkeypatch,
    latest,
):
    _parent, labels = _batch_widgets(qtbot, qapp)
    _one_widget_per_batch(monkeypatch)
    completed = []
    controller.apply(True, lambda: completed.append("old"))
    qtbot.waitUntil(lambda: any(label.property("_backgroundSurface") for label in labels))
    assert controller.apply(latest, lambda: completed.append("latest")) is (not latest)
    assert controller.enabled is latest
    qtbot.waitUntil(lambda: bool(completed), timeout=3000)
    assert completed == ["latest"]
    assert all(bool(label.property("_backgroundSurface")) is latest for label in labels)


def test_deleted_widget_is_skipped_without_losing_batch_completion(qtbot, qapp, controller, monkeypatch):
    _parent, labels = _batch_widgets(qtbot, qapp)
    _one_widget_per_batch(monkeypatch)
    completed = []
    controller.apply(True, lambda: completed.append(True))
    qtbot.waitUntil(lambda: any(label.property("_backgroundSurface") for label in labels))
    deleted = next(label for label in labels if not label.property("_backgroundSurface"))
    shiboken6.delete(deleted)
    qtbot.waitUntil(lambda: bool(completed), timeout=3000)
    assert completed == [True]
    assert all(label.property("_backgroundSurface") is True for label in labels if label is not deleted)


def test_accessor_is_idempotent_and_disposal_cancels_callbacks_and_inheritance(qtbot, qapp, controller):
    assert background_style.install_background_style(qapp) is controller
    assert controller.parent() is qapp
    completed = []
    controller.apply(True, lambda: completed.append(True))
    controller.dispose()
    label = QWidget()
    label.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(label)
    label.ensurePolished()
    label.show()
    qapp.processEvents()
    assert label.property("_backgroundSurface") is None
    assert not completed
