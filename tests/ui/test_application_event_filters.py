import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject

FIRST = QEvent.Type(QEvent.registerEventType())
SECOND = QEvent.Type(QEvent.registerEventType())


class Receiver(QObject):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def event(self, event):
        if event.type() in (FIRST, SECOND):
            self.calls.append("receiver")
            return True
        return super().event(event)


class Filter(QObject):
    def __init__(self, calls, name, action=None):
        super().__init__()
        self.calls, self.name, self.action = calls, name, action

    def eventFilter(self, obj, event):
        if event.type() in (FIRST, SECOND):
            self.calls.append(self.name)
            return bool(self.action and self.action(obj, event))
        return False


@pytest.fixture
def routing(qapp):
    from src.ui.application_event_filters import (
        group_application_event_filters,
        install_application_event_filter,
        remove_application_event_filter,
    )

    filters = []

    def install(calls, name, kinds=(FIRST,), action=None, *, grouped=True):
        handler = Filter(calls, name, action)
        filters.append(handler)
        if grouped:
            install_application_event_filter(qapp, handler, kinds)
        else:
            qapp.installEventFilter(handler)
        return handler

    try:
        yield (
            lambda: group_application_event_filters(qapp),
            install,
            lambda handler: install_application_event_filter(qapp, handler, (FIRST,)),
            lambda handler: remove_application_event_filter(qapp, handler),
        )
    finally:
        for handler in filters:
            if shiboken6.isValid(handler):
                remove_application_event_filter(qapp, handler)
                shiboken6.delete(handler)


def test_only_subscribed_types_reach_handlers_in_reverse_install_order(routing):
    group, install, _, _ = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        install(calls, "first", (FIRST,))
        install(calls, "second", (SECOND,))
        install(calls, "both", (FIRST, SECOND))
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["both", "first", "receiver"]
    calls.clear()
    QCoreApplication.sendEvent(receiver, QEvent(SECOND))
    assert calls == ["both", "second", "receiver"]


def test_independent_filter_stays_between_explicit_groups(routing):
    group, install, _, _ = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        install(calls, "combo")
        install(calls, "acrylic")
        install(calls, "chrome")
    install(calls, "dnd", grouped=False)
    with group():
        install(calls, "font")
        install(calls, "background")
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["background", "font", "dnd", "chrome", "acrylic", "combo", "receiver"]


def test_consumed_event_stops_group_and_native_delivery(routing):
    group, install, _, _ = routing
    calls = []
    receiver = Receiver(calls)
    install(calls, "outside", grouped=False)
    with group():
        install(calls, "earlier")
        install(calls, "consume", action=lambda *_: True)
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["consume"]


def test_repeated_install_within_batch_moves_filter_to_front_once(routing):
    group, install, reinstall, _ = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        first = install(calls, "first")
        install(calls, "second")
        reinstall(first)
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["first", "second", "receiver"]


def test_reinstall_moves_one_handler_ahead_of_later_independent_filters(routing):
    group, install, reinstall, remove = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        first = install(calls, "first")
        install(calls, "second")
    install(calls, "outside", grouped=False)
    reinstall(first)
    reinstall(first)
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["first", "outside", "second", "receiver"]
    remove(first)
    calls.clear()
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["outside", "second", "receiver"]


@pytest.mark.parametrize("destroy", [False, True])
def test_removing_later_handler_during_delivery_skips_it(routing, destroy):
    group, install, _, remove = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        later = install(calls, "later")
        install(calls, "remove", action=lambda *_: shiboken6.delete(later) if destroy else remove(later))
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["remove", "receiver"]


def test_nested_event_sees_updated_registration_without_replaying_outer_callbacks(routing):
    group, install, _, remove = routing
    calls = []
    receiver = Receiver(calls)
    nested = False

    def dispatch_nested(obj, event):
        nonlocal nested
        if not nested:
            nested = True
            remove(earlier)
            QCoreApplication.sendEvent(obj, QEvent(FIRST))

    with group():
        earlier = install(calls, "earlier")
        install(calls, "nested", action=dispatch_nested)
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["nested", "nested", "receiver", "receiver"]


def test_receiver_deleted_by_handler_is_not_delivered_to_remaining_filters(routing):
    group, install, _, _ = routing
    calls = []
    receiver = Receiver(calls)
    with group():
        install(calls, "later")
        install(calls, "delete", action=lambda obj, _: shiboken6.delete(obj))
    QCoreApplication.sendEvent(receiver, QEvent(FIRST))
    assert calls == ["delete"]


def test_production_installers_register_their_actual_event_types(qapp, monkeypatch):
    from src.ui import acrylic, combo_wheel, dialog_chrome, fonts
    from src.ui.application_event_filters import (
        group_application_event_filters,
        remove_application_event_filter,
    )
    from src.ui.background_style import install_background_style

    for name in ("_combo_wheel_filter", "_font_inheritance", "_background_style"):
        monkeypatch.setattr(qapp, name, None, raising=False)
    monkeypatch.setattr(acrylic, "_filter_singleton", None)
    with group_application_event_filters(qapp):
        combo = combo_wheel.install(qapp)
        acrylic.install(qapp)
        chrome = dialog_chrome.install(qapp)
    with group_application_event_filters(qapp):
        fonts.apply_font(qapp, qapp.font())
        background = install_background_style(qapp)
    handlers = [
        (combo, "combo"),
        (acrylic._filter_singleton, "acrylic"),
        (chrome, "chrome"),
        (qapp._font_inheritance, "font"),
        (background, "background"),
    ]
    calls = []
    for handler, name in handlers:
        original = handler.eventFilter

        def observe(obj, event, original=original, name=name):
            calls.append(name)
            return original(obj, event)

        monkeypatch.setattr(handler, "eventFilter", observe)
    receiver = QObject()
    try:
        for kind, expected in (
            (FIRST, []),
            (QEvent.Type.Polish, ["background", "font", "chrome", "acrylic"]),
            (QEvent.Type.Show, ["background", "font", "chrome", "acrylic"]),
            (QEvent.Type.StyleChange, ["font", "acrylic"]),
            (QEvent.Type.Wheel, ["combo"]),
        ):
            calls.clear()
            QCoreApplication.sendEvent(receiver, QEvent(kind))
            assert calls == expected
    finally:
        background.dispose()
        for handler, _ in handlers:
            remove_application_event_filter(qapp, handler)
            handler.deleteLater()
