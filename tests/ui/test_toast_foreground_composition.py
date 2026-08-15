from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMainWindow, QWidget

from src.app import App
from src.ui import theme
from src.ui.background_style import install_background_style
from src.ui.backgrounds import video_presenter
from src.ui.main_window import MainWindow
from src.ui.widgets.background_overlay import BackgroundOverlay


@pytest.mark.parametrize("start,end", [("light", "dark"), ("dark", "light")])
def test_live_notification_and_rounded_corners_reach_video_foreground(
    qtbot,
    qapp,
    temp_data_dir,
    monkeypatch,
    start,
    end,
):
    surfaces = install_background_style(qapp)
    previous = qapp.styleSheet(), theme.current_theme(), theme.is_background_active(), surfaces.enabled
    monkeypatch.setattr(App, "_instance", None)
    app = App()
    app._qt_app = qapp
    # 保留真实显示器和捕获效果，仅隔离独立解码进程，像素断言检查实际发布帧。
    monkeypatch.setattr(
        video_presenter.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(poll=lambda: None, wait=lambda **kwargs: 0),
    )
    window = QMainWindow()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(window)
    root = QWidget()
    root.setObjectName("notificationForeground")
    root.setStyleSheet("QWidget#notificationForeground { background: transparent; }")
    window.setCentralWidget(root)
    window._background_controller = SimpleNamespace(active=True)
    app._main_window = window
    window.resize(620, 240)
    overlay = BackgroundOverlay(root)
    overlay.setGeometry(root.rect())
    app.apply_theme(start)
    window.show()
    presenter = video_presenter.NativeVideoPresenter(overlay, window)
    frames = []
    presenter._capture_effect.frame_ready.connect(frames.append)
    toast = MainWindow._ensure_toast(window)
    applied = []
    toast.busy("Applying theme", on_shown=lambda: app.apply_theme(end, on_finished=applied.append))
    try:
        qtbot.waitUntil(lambda: applied == [True])
        for completed in (False, True):
            if completed:
                toast.done("Theme applied", hold_ms=10000)
            frames.clear()
            root.update()
            qtbot.waitUntil(lambda: bool(frames))
            foreground = frames[-1]
            ratio = foreground.devicePixelRatio()
            origin = root.mapFrom(window, toast.mapTo(window, QPoint(0, 0)))

            def pixel(x, y):
                return foreground.pixelColor(round((origin.x() + x) * ratio), round((origin.y() + y) * ratio))

            assert pixel(toast.width() // 2, 4) == QColor(theme.color("bg.card")), (
                "The video foreground must contain the notification, not just the page beneath it"
            )
            for x, y in (
                (0, 0),
                (toast.width() - 1, 0),
                (0, toast.height() - 1),
                (toast.width() - 1, toast.height() - 1),
            ):
                assert pixel(x, y).alpha() == 0, "The compositor must receive clear rounded corners"
        assert MainWindow._ensure_toast(window) is toast, "Appearance operations reuse one notification"
        toast.hide()
        frames.clear()
        root.update()
        qtbot.waitUntil(lambda: bool(frames))
        foreground = frames[-1]
        assert pixel(toast.width() // 2, 4).alpha() == 0, "A dismissed notification must leave no foreground residue"
    finally:
        presenter.stop()
        presenter._finish()
        overlay.dispose()
        window.close()
        theme.set_theme(previous[1])
        theme.set_background_active(previous[2])
        surfaces.apply(previous[3], theme_name=previous[1])
        qapp.setStyleSheet(previous[0])
