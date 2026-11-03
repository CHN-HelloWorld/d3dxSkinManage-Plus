"""验证共享微光驱动不会保留已销毁的子控件。"""

import gc
import weakref

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget

from src.ui import anim
from src.ui.widgets import spinner


@pytest.mark.parametrize("enabled", [False, True])
def test_deleted_loading_labels_leave_shared_driver(qtbot, monkeypatch, enabled):
    monkeypatch.setattr(anim, "_enabled", enabled)
    driver = spinner._ShimmerDriver()
    monkeypatch.setattr(spinner, "_driver", driver)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    refs = []
    try:
        for _ in range(12):
            label = spinner.ShimmerLabel(parent)
            label.resize(80, 60)
            label.start_loading()
            label.show()
            assert len(driver._widgets) == 1
            refs.append(weakref.ref(label._shimmer))
            label.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            del label
            gc.collect()
            assert not driver._widgets
            assert not driver._timer.isActive()
        assert all(reference() is None for reference in refs)
    finally:
        driver._timer.stop()
        driver._timer.deleteLater()


def test_repeated_show_hide_reuses_one_driver_timer(qtbot, monkeypatch):
    monkeypatch.setattr(anim, "_enabled", True)
    driver = spinner._ShimmerDriver()
    monkeypatch.setattr(spinner, "_driver", driver)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    label = spinner.ShimmerLabel(parent)
    label.resize(80, 60)
    timer = driver._timer
    try:
        for _ in range(12):
            label.show()
            label.start_loading()
            assert len(driver._widgets) == 1 and driver._timer is timer
            label.hide()
            assert not driver._widgets and not timer.isActive()
    finally:
        label.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        timer.stop()
        timer.deleteLater()
