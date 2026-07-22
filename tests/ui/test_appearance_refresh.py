from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


@pytest.fixture(autouse=True)
def _background_style_lifetime(qapp):
    """释放本用例安装的外观控制器，防止材质属性跨用例传播。"""
    previous = getattr(qapp, "_background_style", None)
    yield
    current = getattr(qapp, "_background_style", None)
    if previous is None and current is not None:
        current.dispose()


class _Toast:
    def __init__(self):
        self.shown = []
        self.completed = []

    def busy(self, text, on_shown=None):
        self.shown.append(on_shown)

    def done(self, text, **kwargs):
        self.completed.append(text)


def test_appearance_queue_coalesces_pending_settings_without_dropping_other_kinds(qtbot):
    from src.ui.appearance import AppearanceQueue

    parent = QWidget()
    qtbot.addWidget(parent)
    toast = _Toast()
    queue = AppearanceQueue(parent, toast)
    operations = []
    finishes = []

    def operation(name):
        def run(finish):
            operations.append(name)
            finishes.append(finish)

        return run

    queue.submit("font", "Applying font", "Font done", operation("old font"))
    queue.submit("font", "Applying font", "Font done", operation("latest font"))
    queue.submit("background", "Applying background", "Background done", operation("background"))
    assert operations == []
    toast.shown.pop(0)()
    assert operations == ["latest font"]
    assert toast.completed == []
    finishes[0]()
    qtbot.waitUntil(lambda: len(toast.shown) == 1)
    toast.shown.pop(0)()
    assert operations == ["latest font", "background"]
    finishes[0]()  # 过期回调不能结束当前背景操作。
    assert toast.completed == []
    finishes[1]()
    assert toast.completed == ["Background done"]


def test_appearance_queue_keeps_only_the_latest_change_while_applying(qtbot):
    from src.ui.appearance import AppearanceQueue

    parent = QWidget()
    qtbot.addWidget(parent)
    toast = _Toast()
    queue = AppearanceQueue(parent, toast)
    applied = []
    active = []
    queue.submit("font", "busy", "first", lambda finish: active.append(finish))
    toast.shown.pop(0)()
    queue.submit("font", "busy", "old", lambda finish: (applied.append("old"), finish()))
    queue.submit("font", "busy", "latest", lambda finish: (applied.append("latest"), finish()))
    active.pop()()
    qtbot.waitUntil(lambda: len(toast.shown) == 1)
    toast.shown.pop(0)()
    assert applied == ["latest"]
    assert toast.completed == ["latest"]


def test_toast_waits_for_real_entry_completion(qtbot, monkeypatch):
    from src.ui import anim
    from src.ui.widgets.toast import Toast

    monkeypatch.setattr(anim, "_enabled", True)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    parent.resize(900, 650)
    parent.show()
    toast = Toast(parent)
    toast.SLIDE_MS = 60
    states = []
    toast.busy("Applying…", on_shown=lambda: states.append(toast.pos() == toast._target_pos()))
    toast._anim.setDuration(500)

    qtbot.wait(150)
    assert states == [], "A fixed timer must not start heavy work in the middle of entry"
    toast._anim.setCurrentTime(500)
    qtbot.waitUntil(lambda: states == [True])
    toast._spinner.stop()


def test_visible_busy_toast_does_not_jump_back_offscreen(qtbot):
    from src.ui.widgets.toast import Toast

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    parent.resize(900, 650)
    parent.show()
    toast = Toast(parent)
    first = []
    toast.busy("Applying font…", on_shown=lambda: first.append(True))
    qtbot.waitUntil(lambda: first == [True])
    toast.busy("Applying background…")
    assert toast.pos() == toast._target_pos()


def test_reapplying_identical_theme_does_not_repolish_widgets(qapp, temp_data_dir, monkeypatch):
    from src.app import App
    from src.ui import theme
    from src.ui.background_style import install_background_style

    surfaces = install_background_style(qapp)
    old_surfaces = surfaces.enabled
    old_sheet = qapp.styleSheet()
    old_theme = theme.current_theme()
    old_background = theme.is_background_active()
    monkeypatch.setattr(App, "_instance", None)
    controller = App()
    controller._qt_app = qapp
    try:
        controller.apply_theme("dark")
        calls = []
        original = qapp.setStyleSheet
        monkeypatch.setattr(qapp, "setStyleSheet", lambda sheet: (calls.append(sheet), original(sheet)))
        assert controller.apply_theme("dark") is False
        assert calls == []
        assert controller.apply_theme("light") is True
        assert calls == [], "Changing colors must not synchronously repolish the whole application"
    finally:
        surfaces.apply(old_surfaces)
        qapp.setStyleSheet(old_sheet)
        theme.set_theme(old_theme)
        theme.set_background_active(old_background)


@pytest.mark.parametrize("background", [False, True])
def test_live_theme_change_yields_and_keeps_nested_controls_and_new_windows_current(
    qtbot,
    qapp,
    temp_data_dir,
    monkeypatch,
    background,
):
    from itertools import count

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QVBoxLayout

    from src.app import App
    from src.ui import background_style, theme

    surfaces = background_style.install_background_style(qapp)
    previous = qapp.styleSheet(), theme.current_theme(), theme.is_background_active(), surfaces.enabled
    monkeypatch.setattr(App, "_instance", None)
    controller = App()
    controller._qt_app = qapp
    # 使用实时来源，使材质不依赖磁盘图像解码。
    from types import SimpleNamespace

    controller._main_window = SimpleNamespace(_background_controller=SimpleNamespace(active=background))
    root = QWidget()
    root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    labels = []
    for _ in range(6):
        nested = QWidget()
        label = QLabel("Theme text", nested)
        QVBoxLayout(nested).addWidget(label)
        layout.addWidget(nested)
        labels.append(label)
    combo = QComboBox()
    combo.addItems(["One", "Two"])
    layout.addWidget(combo)
    local = QLabel("Heading")
    local.setStyleSheet("font-size: 19px; font-weight: bold;")
    layout.addWidget(local)
    hidden = QDialog(root)
    hidden_label = QLabel("Hidden", hidden)
    QVBoxLayout(hidden).addWidget(hidden_label)
    controller.apply_theme("dark")
    root.show()
    qapp.processEvents()
    old_sheet = qapp.styleSheet()
    old_family = local.font().family()
    frames, completed = [], []
    heartbeat = QTimer(root)
    light = QColor(theme.color("text.primary", "light"))
    heartbeat.timeout.connect(
        lambda: frames.append(sum(label.palette().color(QPalette.ColorRole.WindowText) == light for label in labels))
    )
    heartbeat.start(0)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(background_style, "perf_counter", count().__next__)
            assert controller.apply_theme("light", on_finished=completed.append)
            assert completed == [], "Live theme application must yield before completing"
            qtbot.waitUntil(lambda: any(frames), timeout=3000)
            # 延迟创建的弹层和隐藏页须采用当前目标外观，即使已有控件尚未更新完毕。
            later = QDialog(root)
            later.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            later_label = QLabel("New window", later)
            QVBoxLayout(later).addWidget(later_label)
            later.show()
            hidden.show()
            assert later_label.palette().color(QPalette.ColorRole.WindowText) == light
            assert hidden_label.palette().color(QPalette.ColorRole.WindowText) == light
            qtbot.waitUntil(lambda: completed == [True], timeout=5000)
        assert qapp.styleSheet() == old_sheet
        assert all(label.palette().color(QPalette.ColorRole.WindowText) == light for label in labels)
        assert any(0 < changed < len(labels) for changed in frames), "GUI input/painting must run during color refresh"
        assert combo.palette().color(QPalette.ColorRole.ButtonText) == light
        assert local.font().family() == old_family
        assert local.font().pixelSize() == 19
        assert local.font().bold()
        assert all(bool(widget.property("_backgroundSurface")) == background for widget in (root, combo, later_label))
    finally:
        heartbeat.stop()
        root.close()
        theme.set_theme(previous[1])
        theme.set_background_active(previous[2])
        surfaces.apply(previous[3])
        qapp.setStyleSheet(previous[0])


@pytest.fixture
def appearance_window(qtbot, qapp, temp_data_dir, monkeypatch):
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QMainWindow

    from src.app import App
    from src.core.config import Config
    from src.ui import theme
    from src.ui.background_style import install_background_style
    from src.ui.fonts import apply_font
    from src.ui.main_window import MainWindow
    from src.ui.widgets.background_overlay import BackgroundOverlay

    surfaces = install_background_style(qapp)
    previous = (
        qapp.styleSheet(),
        QFont(qapp.font()),
        theme.current_theme(),
        theme.is_background_active(),
        surfaces.enabled,
    )
    monkeypatch.setattr(App, "_instance", None)
    controller = App()
    controller._qt_app = qapp
    Config().set("general.theme", "dark")
    controller.apply_theme("dark")

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._config = Config()
            self._central = QWidget()
            self.setCentralWidget(self._central)
            self._bg_overlay = BackgroundOverlay(self._central)
            from src.ui.backgrounds.controller import BackgroundController

            self._background_controller = BackgroundController(self._bg_overlay, self)
            self.rethemes = 0
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            self.resize(900, 650)
            self.show()

        def _post_appearance_refresh(self, text, *, on_finished=None, **kwargs):
            self.rethemes += 1
            self._when_appearance_ready(on_finished)

        def closeEvent(self, event):
            self._background_controller.dispose()
            self._bg_overlay.dispose()
            if getattr(self, "_appearance_queue", None) is not None:
                self._appearance_queue.dispose()
            QMainWindow.closeEvent(self, event)

    window = Window()
    qtbot.addWidget(window)
    yield window, controller
    window.close()
    qapp.setStyleSheet("")
    apply_font(qapp, previous[1])
    theme.set_theme(previous[2])
    theme.set_background_active(previous[3])
    surfaces.apply(previous[4])
    qapp.setStyleSheet(previous[0])


@pytest.mark.parametrize("cancel", ["clear", "disable"])
def test_cancelling_background_releases_queued_font_before_old_read_finishes(
    qtbot,
    qapp,
    appearance_window,
    monkeypatch,
    tmp_path,
    cancel,
):
    from threading import Event

    from PySide6.QtCore import QThreadPool
    from PySide6.QtGui import QFontDatabase, QImage, QImageReader

    from src.ui.widgets import background_overlay

    if "Arial" not in QFontDatabase.families():
        pytest.skip("This native interaction uses an installed Windows font")
    window, controller = appearance_window
    release = Event()
    started = Event()

    class SlowReader(QImageReader):
        def read(self):
            started.set()
            release.wait(10)
            return QImage()

    monkeypatch.setattr(background_overlay, "QImageReader", SlowReader)
    config = window._config
    config.set("appearance.background.enabled", True)
    config.set("appearance.background.path", str(tmp_path / "slow.png"))
    try:
        window._on_background_changed()
        qtbot.waitUntil(lambda: started.is_set() and window._appearance_queue._current is not None)
        if cancel == "clear":
            config.set("appearance.background.path", "")
        else:
            config.set("appearance.background.enabled", False)
        window._on_background_changed()

        assert not window._bg_overlay.is_loading(), "Cancellation must reach the loader before queued work finishes"
        window._on_font_changed("Arial")
        qtbot.waitUntil(lambda: not window._appearance_queue._active, timeout=8000)
        assert qapp.font().family() == "Arial"
        assert not release.is_set()
        assert window._bg_overlay._orig is None
    finally:
        release.set()
        assert QThreadPool.globalInstance().waitForDone(3000)
        qapp.processEvents()


def test_background_image_replacement_does_not_refresh_page_styles(qtbot, appearance_window, tmp_path):
    from PySide6.QtGui import QColor, QImage

    window, controller = appearance_window
    config = window._config
    config.set("appearance.background.enabled", True)
    # 先准备已解码图像，再验证替换已有表面。
    initial = QImage(8, 8, QImage.Format.Format_RGB32)
    initial.fill(QColor("blue"))
    initial_path = tmp_path / "initial.png"
    initial.save(str(initial_path))
    config.set("appearance.background.path", str(initial_path))
    controller.apply_theme("dark")
    image = QImage(40, 40, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    path = tmp_path / "replacement.png"
    assert image.save(str(path))
    config.set("appearance.background.path", str(path))

    window._on_background_changed()

    qtbot.waitUntil(lambda: not window._appearance_queue._active)
    assert window.rethemes == 0
    assert window._bg_overlay._orig.toImage().pixelColor(0, 0) == QColor("red")


@pytest.mark.parametrize("load_succeeds", [True, False])
def test_background_frame_transition_does_not_strand_appearance_queue(
    qtbot,
    qapp,
    appearance_window,
    monkeypatch,
    tmp_path,
    load_succeeds,
):
    from types import SimpleNamespace

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QImage

    from src.ui.appearance import AppearanceQueue
    from src.ui.background_style import install_background_style

    window, app = appearance_window
    app._main_window = window
    window._settings_page = SimpleNamespace(set_background_status=lambda *args: None)
    overlay = window._bg_overlay
    # 显式完成解码，使首帧或错误到达两个批次之间。
    monkeypatch.setattr(overlay, "_start_pending_read", lambda: None)
    config = window._config
    config.set("appearance.background.enabled", True)
    config.set("appearance.background.path", str(tmp_path / "first.png"))
    config.set("appearance.background.blur", 0)
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("blue"))
    if not load_succeeds:
        window._refresh_bg_overlay()
        overlay._image_read(overlay._generation, image)
        app.apply_theme("dark")
        config.set("appearance.background.path", str(tmp_path / "missing.png"))

    class ImmediateToast(_Toast):
        def busy(self, text, on_shown=None):
            QTimer.singleShot(0, on_shown)

    toast = ImmediateToast()
    queue = window._appearance_queue = AppearanceQueue(window, toast)
    window._background_controller.active_changed.connect(window._on_bg_active_changed)
    surfaces = install_background_style(qapp)
    monkeypatch.setattr(surfaces, "BUDGET_SECONDS", 0)
    queue.submit("background", "busy", "done", window._do_background_change)
    queue._run_next()
    assert surfaces._on_finished is not None
    overlay._image_read(overlay._generation, image if load_succeeds else QImage())
    subsequent = []
    queue.submit("font", "busy", "done", lambda finish: (subsequent.append(True), finish()))

    qtbot.waitUntil(lambda: not queue._active, timeout=3000)

    assert subsequent == [True], "A material transition must preserve earlier completion callbacks"
    assert surfaces.enabled is load_succeeds


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("both_themes", [False, True])
def test_conditional_background_rules_preserve_original_style_priority(qtbot, qapp, mode, background, both_themes):
    from PySide6.QtWidgets import QFrame, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    from src.ui import theme
    from src.ui.background_style import appearance_stylesheet, install_background_style

    surfaces = install_background_style(qapp)
    previous_surfaces = surfaces.enabled
    previous_sheet = qapp.styleSheet()
    panel = QWidget()
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(panel)
    row = QHBoxLayout(panel)
    for name in ("modCard", "previewPanel", "navContainer"):
        card = QFrame()
        card.setObjectName(name)
        QVBoxLayout(card).addWidget(QLabel(name))
        row.addWidget(card)
    group = QGroupBox("Group")
    buttons = QVBoxLayout(group)
    for accent, enabled in ((False, True), (True, True), (True, False)):
        button = QPushButton("Action")
        button.setProperty("accent", accent)
        button.setEnabled(enabled)
        buttons.addWidget(button)
    row.addWidget(group)
    panel.resize(850, 180)
    try:
        original = theme.render_qss(mode)
        if background:
            original += theme.background_overlay_qss(mode)
        qapp.setStyleSheet(original)
        panel.show()
        qapp.processEvents()
        expected = panel.grab().toImage()
        surfaces.apply(background, theme_name=mode if both_themes else None)
        qapp.setStyleSheet(appearance_stylesheet() if both_themes else theme.appearance_qss(mode))
        qapp.processEvents()
        actual = panel.grab().toImage()
        assert actual == expected, "Conditional materials must retain accent, disabled, card and panel precedence"
    finally:
        surfaces.apply(previous_surfaces)
        qapp.setStyleSheet(previous_sheet)


def test_live_theme_completion_includes_current_icon_colors(qtbot, qapp, temp_data_dir, monkeypatch):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QMainWindow, QPushButton, QStackedWidget, QVBoxLayout

    from src.app import App
    from src.ui import icons, theme
    from src.ui.background_style import install_background_style
    from src.ui.main_window import MainWindow

    surfaces = install_background_style(qapp)
    previous = qapp.styleSheet(), theme.current_theme(), theme.is_background_active(), surfaces.enabled
    monkeypatch.setattr(App, "_instance", None)
    controller = App()
    controller._qt_app = qapp
    controller.apply_theme("dark")

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._stack = QStackedWidget()
            self.setCentralWidget(self._stack)
            for _ in range(self.PAGE_SETTINGS + 1):
                self._stack.addWidget(QWidget())
            self._stack.setCurrentIndex(self.PAGE_SETTINGS)
            self.button = QPushButton("Action")
            QVBoxLayout(self._stack.currentWidget()).addWidget(self.button)
            icons.apply_icon(self.button, "navigation/settings")

        def _refresh_themed_icons(self):
            pass

        def _refresh_bg_overlay(self):
            pass

        def closeEvent(self, event):
            self._appearance_disposed = True
            QMainWindow.closeEvent(self, event)

    window = Window()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(window)
    window.show()
    old_icon = window.button.icon().pixmap(32, 32).toImage()
    completed = []
    try:
        window._do_theme_change("light", lambda: completed.append(window.button.icon().pixmap(32, 32).toImage()))
        assert completed == []
        qtbot.waitUntil(lambda: bool(completed), timeout=3000)
        assert completed[0] != old_icon
        colors = {
            completed[0].pixelColor(x, y).name()
            for x in range(32)
            for y in range(32)
            if completed[0].pixelColor(x, y).alpha() == 255
        }
        assert QColor("#1a1a1a").name() in colors
        assert "#ffffff" not in colors
    finally:
        window.close()
        theme.set_theme(previous[1])
        theme.set_background_active(previous[2])
        surfaces.apply(previous[3])
        qapp.setStyleSheet(previous[0])


def test_settings_live_retheme_yields_until_all_styles_are_applied(qtbot, temp_data_dir, monkeypatch):
    from itertools import count

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel

    from src.ui.pages import settings

    page = settings.SettingsPage()
    qtbot.addWidget(page)
    page._reappliers.clear()
    labels = [QLabel("Preview", page) for _ in range(5)]
    color = ["red"]
    for label in labels:
        page._restyle(label, lambda: f"color: {color[0]};")
    monkeypatch.setattr(page._shortcuts_page, "retheme", lambda: None)
    monkeypatch.setattr(settings, "perf_counter", count().__next__, raising=False)
    color[0] = "blue"
    finished = []
    frames = []
    heartbeat = QTimer(page)
    heartbeat.timeout.connect(lambda: frames.append(sum(label.styleSheet() == "color: blue;" for label in labels)))
    heartbeat.start(0)

    try:
        page.retheme(on_finished=lambda: finished.append([label.styleSheet() for label in labels]))

        assert finished == []
        qtbot.waitUntil(lambda: bool(finished))
    finally:
        heartbeat.stop()
    assert finished == [["color: blue;"] * len(labels)]
    assert any(0 < updated < len(labels) for updated in frames), "Other GUI events must run between style batches"


def test_synchronous_settings_retheme_supersedes_an_incomplete_refresh(qtbot, temp_data_dir, monkeypatch):
    from itertools import count

    from src.ui.pages import settings

    page = settings.SettingsPage()
    qtbot.addWidget(page)
    applied = []
    callbacks = []
    page._reappliers = [lambda: applied.append(True) for _ in range(5)]
    monkeypatch.setattr(page._shortcuts_page, "retheme", lambda: None)
    monkeypatch.setattr(settings, "perf_counter", count().__next__, raising=False)
    page.retheme(on_finished=lambda: callbacks.append("old"))
    qtbot.waitUntil(lambda: bool(applied))
    assert len(applied) < 5
    applied.clear()

    page.retheme()

    assert len(applied) == 5
    qtbot.wait(30)
    assert callbacks == []
    assert len(applied) == 5


def test_appearance_completion_waits_for_settings_refresh(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMainWindow, QStackedWidget

    from src.ui import acrylic
    from src.ui.main_window import MainWindow

    callbacks = []
    completed = []

    class DeferredSettings:
        def retheme(self, *, on_finished=None):
            callbacks.append(on_finished)

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._stack = QStackedWidget(self)
            self._settings_page = DeferredSettings()

        def _retheme_one_page(self, index):
            pass

        def closeEvent(self, event):
            QMainWindow.closeEvent(self, event)

    window = Window()
    qtbot.addWidget(window)
    monkeypatch.setattr(acrylic, "reset_for_theme_change", lambda: None)

    window._post_appearance_refresh(
        "Done",
        on_finished=lambda: completed.append(True),
        colors_changed=False,
        refresh_background=False,
    )

    qtbot.waitUntil(lambda: bool(callbacks))
    assert completed == []
    assert callbacks[0] is not None, "The final notification must wait for asynchronous settings styles"
    callbacks.pop()()
    qtbot.waitUntil(lambda: completed == [True])


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("operation", ["background", "theme"])
def test_appearance_completion_ignores_closed_or_deleted_window(qtbot, monkeypatch, deleted, changed, operation):
    from types import SimpleNamespace

    import shiboken6
    from PySide6.QtWidgets import QMainWindow, QStackedWidget

    from src.app import App
    from src.ui.main_window import MainWindow

    callbacks = []
    completed = []

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._stack = QStackedWidget(self)

        def apply_background(self, *, on_applied=None):
            callbacks.append(on_applied)

        def closeEvent(self, event):
            self._appearance_disposed = True
            QMainWindow.closeEvent(self, event)

    window = Window()
    if not deleted:
        qtbot.addWidget(window)
    if operation == "theme":
        app = SimpleNamespace(apply_theme=lambda mode, *, on_finished: callbacks.append(on_finished))
        monkeypatch.setattr(App, "instance", lambda: app)
        window._do_theme_change("light", lambda: completed.append(True))
    else:
        window._do_background_change(lambda: completed.append(True))
    if deleted:
        shiboken6.delete(window)
    else:
        window.close()

    callbacks.pop()(changed)

    assert completed == []
    assert "_pending_retheme" not in window.__dict__
