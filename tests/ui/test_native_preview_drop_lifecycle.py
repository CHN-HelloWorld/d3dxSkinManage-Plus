"""在 Windows 验证重复图片投放、查看器和菜单与真实 OLE 目标的生命周期。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from src.ui import acrylic, dialog_chrome, win_dnd_admin
from src.ui.widgets.preview import ImageViewerDialog, PreviewWidget


def test_repeated_preview_drops_preserve_and_release_native_targets(qapp, qtlog, monkeypatch, temp_data_dir):
    """保留原生窗口和 COM 注册，仅固定落点控件，避免测试期间其他桌面窗口遮挡落点。"""
    if qapp.platformName() != "windows":
        pytest.skip("此用例要求 Windows Qt 平台和真实 OLE 注册")
    image_path = temp_data_dir / "preview.png"
    image = QImage(48, 64, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(image_path))
    monkeypatch.setattr(win_dnd_admin, "_saved_targets", {})
    monkeypatch.setattr(win_dnd_admin, "_last_mime", None)
    native_dialogs = qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    host = QWidget()
    preview = PreviewWidget(host)
    preview.set_single_image_mode(True)
    QVBoxLayout(host).addWidget(preview)
    host.resize(400, 320)
    delivered = []
    preview.image_dropped.connect(delivered.append)
    arm_filter = win_dnd_admin._ArmOnShow(qapp)
    popup_filter = acrylic.AcrylicPopupFilter(qapp)
    chrome = dialog_chrome.install(qapp)
    qapp.installEventFilter(popup_filter)
    qapp.installEventFilter(arm_filter)
    try:
        host.show()
        qapp.processEvents()
        hwnd = int(host.winId())
        win_dnd_admin._arm(hwnd)
        assert hwnd in win_dnd_admin._saved_targets
        monkeypatch.setattr(QApplication, "widgetAt", lambda _point: preview)
        for _ in range(12):
            local = preview.mapTo(host, preview.rect().center())
            dpr = host.devicePixelRatioF()
            assert win_dnd_admin._deliver(
                host, [str(image_path)], QPoint(round(local.x() * dpr), round(local.y() * dpr))
            )
            assert preview.current_image_path() == image_path.as_posix()
            assert win_dnd_admin._restore_ole_target(hwnd)
            win_dnd_admin._arm(hwnd)

            viewer = ImageViewerDialog(str(image_path), host)
            viewer.show()
            qapp.processEvents()
            viewer_hwnd = int(viewer.winId())
            menu = QMenu(preview)
            menu.addAction("操作")
            menu.popup(preview.mapToGlobal(QPoint(20, 20)))
            qapp.processEvents()
            menu.close()
            menu.deleteLater()
            viewer.close()
            viewer.deleteLater()
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
            assert viewer_hwnd not in win_dnd_admin._saved_targets
        assert delivered == [image_path.as_posix()] * 12
        assert not [record.message for record in qtlog.records if "must be a top level window" in record.message]
    finally:
        qapp.removeEventFilter(arm_filter)
        qapp.removeEventFilter(popup_filter)
        qapp.removeEventFilter(chrome)
        host.hide()
        host.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        remaining = list(win_dnd_admin._saved_targets.values())
        for saved in remaining:
            saved.release()
        arm_filter.deleteLater()
        popup_filter.deleteLater()
        chrome.deleteLater()
        win_dnd_admin._last_mime = None
        qapp.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, native_dialogs)
    assert remaining == []
