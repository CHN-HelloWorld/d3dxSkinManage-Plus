from __future__ import annotations

import os
import subprocess
import sys
import weakref
from pathlib import Path
from textwrap import dedent
from threading import Event, Lock, get_ident

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QWidget

from src.ui.widgets import background_overlay


@pytest.fixture
def image_paths(tmp_path):
    paths = {}
    for name, color in (("first.png", "red"), ("middle.png", "green"), ("last.png", "blue")):
        image = QImage(80, 40, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        path = tmp_path / name
        assert image.save(str(path))
        paths[name] = str(path)
    return paths


@pytest.fixture
def overlay(qtbot):
    widget = background_overlay.BackgroundOverlay()
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(widget)
    return widget


class _ReadControl:
    """控制真实原生读取器的继续执行，使竞争不依赖图像大小或调度速度。"""

    def __init__(self):
        self.releases: dict[str, Event] = {}
        self.started: set[str] = set()
        self.finished: set[str] = set()
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.max_active = 0
        self.lock = Lock()

    def block(self, name):
        release = Event()
        self.releases[name] = release
        return release


@pytest.fixture
def reads(monkeypatch, qapp):
    control = _ReadControl()

    class ControlledReader(QImageReader):
        def read(self):
            name = Path(self.fileName()).name
            with control.lock:
                control.calls.append((name, get_ident()))
                control.active += 1
                control.max_active = max(control.max_active, control.active)
                control.started.add(name)
            try:
                release = control.releases.get(name)
                if release is not None and not release.wait(5):
                    raise TimeoutError(f"test did not release {name}")
                return super().read()
            finally:
                with control.lock:
                    control.active -= 1
                    control.finished.add(name)

    monkeypatch.setattr(background_overlay, "QImageReader", ControlledReader, raising=False)
    yield control
    for release in control.releases.values():
        release.set()
    # 仅测试清理等待线程；应用释放操作必须在读取解除暂停前返回。
    for _ in range(2):
        assert QThreadPool.globalInstance().waitForDone(3000)
        qapp.processEvents()


def _wait_ready(qtbot, overlay):
    qtbot.waitUntil(lambda: not overlay.is_loading(), timeout=3000)


def _finish_reads(qtbot, qapp):
    # 完成通知可能在界面线程调度唯一待处理读取。
    for _ in range(2):
        qtbot.waitUntil(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=3000)
        qapp.processEvents()


def test_decode_leaves_gui_responsive_and_installs_before_ready(
    qtbot,
    qapp,
    overlay,
    image_paths,
    reads,
    monkeypatch,
):
    release = reads.block("first.png")
    main_thread = get_ident()
    blur_threads = []
    make_blurred = overlay._make_blurred

    def check_blur_thread(pixmap, amount):
        blur_threads.append(get_ident())
        return make_blurred(pixmap, amount)

    monkeypatch.setattr(overlay, "_make_blurred", check_blur_thread)
    ready = []
    heartbeat = []
    overlay.configure(image_paths["first.png"], 30, 15, "cover")
    overlay.when_ready(lambda: ready.append((get_ident(), overlay._orig.toImage().pixelColor(40, 20))))
    assert overlay.is_loading()
    assert overlay._orig is None
    qtbot.waitUntil(lambda: "first.png" in reads.started)
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qtbot.waitUntil(lambda: bool(heartbeat))
    assert not reads.finished
    assert not ready

    release.set()
    _wait_ready(qtbot, overlay)
    assert [name for name, _ in reads.calls] == ["first.png"]
    assert reads.calls[0][1] != main_thread
    assert blur_threads and set(blur_threads) == {main_thread}
    assert ready == [(main_thread, QColor("red"))]
    assert overlay._render is not None
    assert (overlay._orig.width(), overlay._orig.height()) == (80, 40)
    # 原生读取器在发送就绪通知前释放文件句柄。
    Path(image_paths["first.png"]).unlink()


def test_replacement_reads_only_latest_pending_path_and_ignores_stale_result(
    qtbot,
    overlay,
    image_paths,
    reads,
):
    first_release = reads.block("first.png")
    last_release = reads.block("last.png")
    ready = []
    overlay.configure(image_paths["first.png"], 20, 0, "cover")
    overlay.when_ready(lambda: ready.append("first waiter"))
    qtbot.waitUntil(lambda: "first.png" in reads.started)
    overlay.configure(image_paths["middle.png"], 20, 0, "cover")
    overlay.configure(image_paths["last.png"], 40, 0, "contain")
    overlay.when_ready(lambda: ready.append("last waiter"))

    first_release.set()
    qtbot.waitUntil(lambda: "last.png" in reads.started)
    assert overlay.is_loading()
    assert overlay._orig is None
    assert not ready
    last_release.set()
    _wait_ready(qtbot, overlay)
    assert [name for name, _ in reads.calls] == ["first.png", "last.png"]
    assert reads.max_active == 1
    assert overlay._orig.toImage().pixelColor(0, 0) == QColor("blue")
    assert ready == ["first waiter", "last waiter"]


def test_clear_settles_immediately_and_cancels_pending_image(
    qtbot,
    qapp,
    overlay,
    image_paths,
    reads,
):
    release = reads.block("first.png")
    ready = []
    overlay.configure(image_paths["first.png"], 20, 0, "cover")
    overlay.when_ready(lambda: ready.append(True))
    qtbot.waitUntil(lambda: "first.png" in reads.started)
    overlay.configure(image_paths["middle.png"], 20, 0, "cover")
    overlay.configure("", 20, 0, "cover")

    assert not overlay.is_loading()
    assert not reads.finished
    assert overlay._orig is None
    assert overlay._render is None
    assert ready == [True]
    release.set()
    _finish_reads(qtbot, qapp)
    assert [name for name, _ in reads.calls] == ["first.png"]
    assert overlay._orig is None
    assert ready == [True]


@pytest.mark.parametrize("invalid", ["missing.png", "corrupt.png"])
def test_invalid_image_settles_waiters_and_removes_old_image(qtbot, overlay, image_paths, tmp_path, invalid):
    overlay.configure(image_paths["first.png"], 20, 0, "cover")
    _wait_ready(qtbot, overlay)
    path = tmp_path / invalid
    if invalid == "corrupt.png":
        path.write_text("not an image", encoding="utf-8")
    ready = []
    overlay.configure(str(path), 20, 0, "cover")
    assert overlay._orig.toImage().pixelColor(0, 0) == QColor("red")
    overlay.when_ready(lambda: ready.append(True))
    _wait_ready(qtbot, overlay)
    assert overlay._orig is None
    assert overlay._render is None
    assert ready == [True]
    overlay.when_ready(lambda: ready.append(True))
    assert ready == [True, True]


@pytest.mark.parametrize("action", ["dispose", "delete_parent"])
def test_disposal_during_read_does_not_wait_or_retain_callbacks(
    qtbot,
    qapp,
    image_paths,
    reads,
    action,
):
    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(parent)
    widget = background_overlay.BackgroundOverlay(parent)
    release = reads.block("first.png")
    called = []

    class Receiver:
        def ready(self):
            called.append(True)

    receiver = Receiver()
    receiver_ref = weakref.ref(receiver)
    widget.configure(image_paths["first.png"], 20, 0, "cover")
    widget.when_ready(receiver.ready)
    del receiver
    qtbot.waitUntil(lambda: "first.png" in reads.started)
    widget.configure(image_paths["last.png"], 20, 0, "cover")
    if action == "dispose":
        widget.dispose()
        assert not widget.is_loading()
    else:
        shiboken6.delete(parent)
        assert not shiboken6.isValid(widget)
        # Qt 完成控件析构后释放保留的回调。
        qapp.processEvents()
    assert not reads.finished, "disposing must return while the decoder is still blocked"
    assert receiver_ref() is None
    release.set()
    _finish_reads(qtbot, qapp)
    assert [name for name, _ in reads.calls] == ["first.png"]
    assert not called


@pytest.mark.parametrize("change", ["identical", "scrim", "live_scrim"])
def test_scaled_pixels_are_reused_for_configuration_without_pixel_changes(qtbot, overlay, image_paths, change):
    path = image_paths["first.png"]
    overlay.configure(path, 20, 0, "cover")
    qtbot.waitUntil(lambda: overlay._render is not None)
    scaled = overlay._scaled_for(overlay._render, 120, 100)
    if change == "live_scrim":
        overlay.set_scrim_live(80)
    else:
        overlay.configure(path, 80 if change == "scrim" else 20, 0, "cover")
    _wait_ready(qtbot, overlay)
    assert overlay._scaled_for(overlay._render, 120, 100) is scaled


@pytest.mark.parametrize("change", ["blur", "fit"])
def test_pixel_changes_invalidate_scaled_pixels(qtbot, overlay, image_paths, change):
    path = image_paths["first.png"]
    overlay.configure(path, 20, 0, "cover")
    qtbot.waitUntil(lambda: overlay._render is not None)
    scaled = overlay._scaled_for(overlay._render, 120, 100)
    if change == "blur":
        overlay.set_blur_live(30)
    else:
        overlay.configure(path, 20, 0, "contain")
    updated = overlay._scaled_for(overlay._render, 120, 100)
    assert updated is not scaled
    if change == "fit":
        assert (updated.width(), updated.height()) == (120, 60)


@pytest.mark.parametrize("mode, expected", [("light", (151, 171, 191)), ("dark", (24, 44, 64))])
def test_scrim_uses_theme_tint_and_preserves_cached_image(overlay, monkeypatch, mode, expected):
    from src.ui import theme

    monkeypatch.setattr(theme, "_current_theme", mode)
    overlay.resize(80, 40)
    overlay._orig = QPixmap(80, 40)
    overlay._orig.fill(QColor(48, 88, 128))
    overlay._render = overlay._orig
    overlay.set_enabled(True)
    scaled = overlay._scaled_for(overlay._render, 80, 40)
    overlay.set_scrim_live(50)
    color = overlay.grab().toImage().pixelColor(40, 20)
    assert color.getRgb()[:3] == expected
    assert overlay._scaled_for(overlay._render, 80, 40) is scaled


@pytest.mark.parametrize("blur", [20, 60, 100])
def test_blur_keeps_opaque_image_edges_opaque_and_uniform(qapp, blur):
    source = QPixmap(160, 100)
    source.fill(QColor(80, 140, 200))
    result = background_overlay.BackgroundOverlay._make_blurred(source, blur).toImage()
    assert result.size() == source.size()
    for x, y in ((0, 0), (159, 0), (0, 99), (159, 99), (0, 50), (80, 50)):
        color = result.pixelColor(x, y)
        assert color.alpha() >= 254, (x, y, color.getRgb())
        assert all(abs(actual - expected) <= 2 for actual, expected in zip(color.getRgb()[:3], (80, 140, 200)))


def test_theme_switch_retints_existing_overlay_without_reblur(overlay, monkeypatch):
    from src.ui import theme

    original_theme = theme.current_theme()
    monkeypatch.setattr(theme, "_current_theme", original_theme)
    overlay.resize(80, 40)
    overlay._orig = QPixmap(80, 40)
    overlay._orig.fill(QColor(48, 88, 128))
    overlay._render = overlay._orig
    overlay.set_enabled(True)
    overlay.set_scrim_live(50)
    render = overlay._render
    scaled = overlay._scaled_for(render, 80, 40)
    for mode, expected in (("dark", (24, 44, 64)), ("light", (151, 171, 191)), ("dark", (24, 44, 64))):
        theme.set_theme(mode)
        assert overlay.grab().toImage().pixelColor(40, 20).getRgb()[:3] == expected
        assert overlay._render is render
        assert overlay._scaled_for(render, 80, 40) is scaled


def test_blur_keeps_large_image_processing_bounded(qapp):
    source = QPixmap(3840, 2160)
    source.fill(QColor(80, 140, 200))
    result = background_overlay.BackgroundOverlay._make_blurred(source, 100).toImage()
    assert (result.width(), result.height()) == (1280, 720)
    for x, y in ((0, 0), (1279, 719), (640, 360)):
        assert result.pixelColor(x, y).alpha() == 255


def test_blur_retains_intentional_image_transparency(qapp):
    source = QPixmap(160, 100)
    source.fill(QColor(80, 140, 200, 128))
    result = background_overlay.BackgroundOverlay._make_blurred(source, 60).toImage()
    for x, y in ((0, 0), (159, 99), (80, 50)):
        assert 123 <= result.pixelColor(x, y).alpha() <= 128


@pytest.mark.parametrize("deferred", [False, True])
def test_destroying_overlay_cannot_reenter_destruction_through_retained_parent(deferred):
    # 在独立子进程内验证潜在访问异常，避免中止整个测试套件。
    script = dedent("""
        import ctypes
        import faulthandler
        import gc
        import sys
        import weakref
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        faulthandler.enable()
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication, QWidget
        from src.ui.widgets.background_overlay import BackgroundOverlay
        app = QApplication([])
        parent = QWidget()
        destroyed = []
        parent.destroyed.connect(lambda: destroyed.append(True))
        overlay = BackgroundOverlay(parent)
        overlay._loading = True
        overlay.when_ready(parent.close)
        reference = weakref.ref(parent)
        del parent
        gc.collect()
        assert reference() is not None
        if sys.argv[1] == "deferred":
            overlay.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        else:
            shiboken6.delete(overlay)
        assert not shiboken6.isValid(overlay)
        app.processEvents()
        gc.collect()
        assert reference() is None
        assert destroyed == [True]
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, "deferred" if deferred else "direct"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_collecting_window_cycles_does_not_call_a_collected_destroyed_slot():
    script = dedent("""
        import ctypes
        import faulthandler
        import gc
        import sys
        import weakref
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        faulthandler.enable()
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication, QWidget
        from src.ui.widgets.background_overlay import BackgroundOverlay
        app = QApplication([])
        references = []
        def make_cycle():
            parent = QWidget()
            overlay = BackgroundOverlay(parent)
            parent.overlay = overlay
            parent.cycle = parent
            overlay._loading = True
            overlay.when_ready(parent.close)
            references.extend((weakref.ref(parent), weakref.ref(overlay),
                               weakref.ref(overlay._ready_callbacks)))
        for _ in range(100):
            make_cycle()
            gc.collect()
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
        assert not any(reference() is not None for reference in references)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
