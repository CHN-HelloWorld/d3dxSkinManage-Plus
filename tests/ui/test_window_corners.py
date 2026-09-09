"""验证系统圆角不可用时，窗口与亚克力背景共用真实圆角边界。"""

from __future__ import annotations

import ctypes
import gc
from ctypes import wintypes

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QMenu, QToolTip, QVBoxLayout, QWidget

from src.ui import acrylic, theme, win_effects


@pytest.fixture
def interactive_desktop(qapp):
    """原生像素验证要求测试线程位于当前可访问的 Windows 输入桌面。"""
    if qapp.platformName() != "windows":
        pytest.skip("需要 Windows 交互桌面")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    def desktop_name(handle):
        """读取桌面名称，避免比较同一桌面的不同句柄值。"""
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            return name.value
        return None

    def ensure_accessible():
        """每次截图前复核，避免测试途中输入桌面切换后继续读取无效画面。"""
        desktop = user.OpenInputDesktop(0, False, 1)
        if not desktop:
            pytest.skip(f"Windows 输入桌面不可访问，无法验证桌面像素（错误 {ctypes.get_last_error()}）")
        try:
            current = user.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
            input_name = desktop_name(desktop)
            if not input_name or input_name != desktop_name(current):
                pytest.skip("测试窗口不在当前输入桌面，无法验证桌面像素")
        finally:
            user.CloseDesktop(desktop)

    ensure_accessible()
    return ensure_accessible


@pytest.fixture
def rounded_ui(qapp, monkeypatch, temp_data_dir):
    """隔离主题，并模拟系统不支持 DWM 圆角；保留真实 Qt 窗口及原生模糊调用。"""
    previous = qapp.styleSheet(), theme.current_theme()
    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: False)
    theme.set_theme("light")
    qapp.setStyleSheet(theme.appearance_qss("light"))
    effects = {
        effect: qapp.isEffectEnabled(effect)
        for effect in (
            Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateMenu,
            Qt.UIEffect.UI_FadeMenu,
            Qt.UIEffect.UI_AnimateTooltip,
            Qt.UIEffect.UI_FadeTooltip,
        )
    }
    # 应用启动时禁用 Qt 的弹窗快照动画，直接显示保留原生材质的窗口。
    for effect in effects:
        qapp.setEffectEnabled(effect, False)
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    yield
    for widget in qapp.topLevelWidgets():
        if widget.metaObject().className() == "QTipLabel":
            shiboken6.delete(widget)
    qapp.removeEventFilter(popup_filter)
    for effect, enabled in effects.items():
        qapp.setEffectEnabled(effect, enabled)
    theme.set_theme(previous[1])
    qapp.setStyleSheet(previous[0])


def assert_clipped_window(window):
    """四角位于窗口之外，直边中点和中心保留可见、可交互区域。"""
    width, height = window.width(), window.height()
    region = window.mask()
    assert not region.isEmpty(), "系统不支持圆角时必须裁切整窗，样式圆角无法限制亚克力层"
    for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert not region.contains(QPoint(*point))
    for point in (
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
        (width // 2, height // 2),
    ):
        assert region.contains(QPoint(*point))
    if QApplication.platformName() == "windows":
        assert_native_region(window)


def assert_native_region(window):
    """直接查询 HWND 区域，确认裁剪已传递给 Windows 合成器。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.PtInRegion.argtypes = [wintypes.HRGN, ctypes.c_int, ctypes.c_int]
    gdi32.PtInRegion.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    user32.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]
    user32.GetWindowRgn.restype = ctypes.c_int
    region = gdi32.CreateRectRgn(0, 0, 0, 0)
    try:
        assert user32.GetWindowRgn(int(window.winId()), region) == 3
        dpr = window.devicePixelRatioF()
        width, height = round(window.width() * dpr), round(window.height() * dpr)
        assert not gdi32.PtInRegion(region, 0, 0)
        assert not gdi32.PtInRegion(region, width - 1, height - 1)
        assert gdi32.PtInRegion(region, width // 2, height // 2)
    finally:
        gdi32.DeleteObject(region)


@pytest.mark.parametrize("kind", ["dialog", "menu", "combo", "editable", "tooltip"])
def test_popup_clips_window_and_acrylic_when_dwm_corners_are_unavailable(rounded_ui, qtbot, qapp, kind):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(400, 260)
    owner.show()
    if kind == "dialog":
        popup = QDialog(owner)
        popup.resize(240, 160)
        acrylic.apply_dialog_acrylic(popup, animate=False)
        popup.show()
    elif kind == "menu":
        popup = QMenu(owner)
        popup.addAction("菜单项目")
        popup.popup(owner.mapToGlobal(QPoint(20, 30)))
    elif kind == "tooltip":
        QToolTip.showText(owner.mapToGlobal(QPoint(20, 30)), "操作说明", owner)
        popup = next(w for w in qapp.topLevelWidgets() if w.metaObject().className() == "QTipLabel")
    else:
        combo = QComboBox(owner)
        combo.setEditable(kind == "editable")
        combo.addItems(["选项一", "选项二"])
        combo.show()
        combo.showPopup()
        popup = combo.view().window()
    qapp.processEvents()
    assert_clipped_window(popup)
    popup.hide()


@pytest.mark.parametrize("small", [False, True])
def test_window_clip_tracks_resize_reopen_and_state(rounded_ui, qtbot, qapp, small):
    window = QWidget(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(window)
    window.resize(240, 160)
    assert win_effects.apply_rounded_corners(window, small=small)
    window.show()
    qapp.processEvents()
    assert_clipped_window(window)
    hwnd = int(window.winId())
    window.resize(360, 220)
    qapp.processEvents()
    assert_clipped_window(window)
    window.hide()
    window.resize(180, 120)
    window.show()
    qapp.processEvents()
    assert_clipped_window(window)
    assert int(window.winId()) == hwnd
    window.showMaximized()
    qapp.processEvents()
    assert window.mask().isEmpty(), "最大化窗口应填满可用区域"
    window.showNormal()
    qapp.processEvents()
    assert_clipped_window(window)
    window.showFullScreen()
    qapp.processEvents()
    assert window.mask().isEmpty(), "全屏窗口应覆盖整个屏幕"
    window.showNormal()
    qapp.processEvents()
    assert_clipped_window(window)


def test_cached_combo_clip_survives_wrapper_collection_and_theme_change(rounded_ui, qtbot, qapp):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(520, 300)
    combo = QComboBox(owner)
    combo.addItems(["选项一", "选项二"])
    owner.show()
    combo.showPopup()
    popup = combo.view().window()
    qapp.processEvents()
    assert_clipped_window(popup)
    combo.hidePopup()
    del popup
    gc.collect()
    theme.set_theme("dark")
    qapp.setStyleSheet(theme.appearance_qss("dark"))
    acrylic.reset_for_theme_change()
    combo.resize(420, 32)
    qapp.processEvents()
    combo.showPopup()
    qapp.processEvents()
    assert combo.view().window().isVisible()
    assert_clipped_window(combo.view().window())
    combo.view().setCurrentIndex(combo.model().index(1, 0))
    qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
    assert combo.currentIndex() == 1
    assert not combo.view().window().isVisible()


def test_system_rounding_does_not_install_a_software_clip(rounded_ui, qtbot, monkeypatch):
    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: True)
    window = QWidget(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(window)
    assert win_effects.apply_rounded_corners(window)
    window.show()
    window.resize(300, 180)
    assert window.mask().isEmpty()


def test_system_rounding_replaces_a_previous_software_clip(rounded_ui, qtbot, monkeypatch, qapp):
    window = QWidget(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(window)
    win_effects.apply_rounded_corners(window)
    window.show()
    qapp.processEvents()
    assert_clipped_window(window)
    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: True)
    assert win_effects.apply_rounded_corners(window)
    window.resize(300, 180)
    qapp.processEvents()
    assert window.mask().isEmpty()


@pytest.mark.parametrize("system_corners", [False, True])
def test_preset_popup_preserves_its_larger_corner_shape(rounded_ui, qtbot, qapp, monkeypatch, system_corners):
    from src.ui.widgets.preset_selector import PresetSelector

    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: system_corners)
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(520, 400)
    selector = PresetSelector(owner)
    owner.show()
    selector._show_popup()
    qapp.processEvents()
    popup = selector._popup
    for width in (400, 440):
        popup.resize(width, 320)
        popup.repaint()
        qapp.processEvents()
        assert not popup.mask().contains(QPoint(6, 1)), "预设面板应保留与背景一致的十二像素圆角"
        assert popup.mask().contains(QPoint(12, 0))


def test_native_blur_leaves_rounded_corners_identical_to_the_background(rounded_ui, qtbot, qapp, interactive_desktop):
    owner = QWidget(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(owner)
    owner.setObjectName("cornerTestBackground")
    owner.setStyleSheet("QWidget#cornerTestBackground { background: #59bda9; }")
    owner.setGeometry(160, 120, 600, 400)
    owner.show()
    owner.raise_()
    owner.activateWindow()
    qtbot.waitUntil(owner.isActiveWindow)
    popup = QDialog(owner)
    popup.resize(240, 160)
    acrylic.apply_dialog_acrylic(popup, animate=False)
    popup.move(owner.mapToGlobal(QPoint(100, 100)))
    popup.show()
    popup.raise_()
    popup.activateWindow()
    qtbot.waitUntil(popup.isActiveWindow)
    expected = QColor("#59bda9")

    def assert_composited_corners():
        """等待窗口真实合成后检查透明四角，避免把首次曝光前的截图作为结果。"""
        interactive_desktop()
        assert popup.isVisible()
        ctypes.windll.dwmapi.DwmFlush()
        for point in (
            popup.rect().topLeft(),
            popup.rect().topRight(),
            popup.rect().bottomLeft(),
            popup.rect().bottomRight(),
        ):
            screen_point = popup.mapToGlobal(point)
            pixel = popup.screen().grabWindow(0, screen_point.x(), screen_point.y(), 1, 1).toImage().pixelColor(0, 0)
            assert pixel == expected, "窗口区域之外必须直接显示下层内容，不能残留亚克力叠色"

    qtbot.waitUntil(assert_composited_corners)


def test_dropdown_in_translucent_dialog_keeps_composited_color_and_live_background(
    rounded_ui, qtbot, qapp, interactive_desktop
):
    """透明对话框内的下拉框须采样完整下层合成，不能只模糊对话框的半透明叠色。"""
    if qapp.platformName() != "windows" or not win_effects.needs_popup_paint_before_blur():
        pytest.skip("需要 Windows 10 桌面合成器")
    owner = QWidget(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(owner)
    owner.setObjectName("nestedBackdropOwner")
    owner.setStyleSheet("QWidget#nestedBackdropOwner { background: #2070c0; }")
    owner.setGeometry(120, 100, 760, 500)
    dialog = QDialog(owner)
    layout = QVBoxLayout(dialog)
    layout.addSpacing(50)
    combo = QComboBox(dialog)
    combo.addItems(["选项一", "选项二", "选项三", "选项四"])
    layout.addWidget(combo)
    layout.addStretch()
    acrylic.apply_dialog_acrylic(dialog, tint_alpha=0.5, animate=False)
    dialog.setGeometry(180, 150, 560, 380)
    owner.show()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    qtbot.waitUntil(dialog.isActiveWindow)
    qtbot.wait(80)
    before = dialog.screen().grabWindow(0).toImage()
    combo.showPopup()
    popup = combo.view().window()
    qtbot.waitUntil(popup.isVisible)
    sample = QPoint(popup.width() - 25, popup.height() - 14)
    position = popup.mapToGlobal(sample)
    screen_position = position - popup.screen().geometry().topLeft()
    underlay = image_color(before, screen_position.x(), screen_position.y())

    def actual_pixel():
        """读取最终桌面像素，保持弹窗可见性断言以排除收起造成的假阳性。"""
        interactive_desktop()
        assert popup.isVisible()
        ctypes.windll.dwmapi.DwmFlush()
        return popup.screen().grabWindow(0, position.x(), position.y(), 1, 1).toImage().pixelColor(0, 0)

    def assert_composited_color():
        """白色叠色应与实际看到的对话框背景混合，不重复叠加透明对话框底色。"""
        actual = actual_pixel()
        expected = [round(255 * 0.94 + channel * 0.06) for channel in underlay.getRgb()[:3]]
        assert all(abs(a - b) <= 3 for a, b in zip(actual.getRgb()[:3], expected))

    qtbot.waitUntil(assert_composited_color)
    initial = actual_pixel()
    owner.setStyleSheet("QWidget#nestedBackdropOwner { background: #cf783f; }")
    owner.update()

    def assert_background_updated():
        """两层加厚底色仅透出微弱变化，但背景更新仍须到达下拉磨砂。"""
        actual = actual_pixel()
        assert 0 < sum(abs(a - b) for a, b in zip(initial.getRgb()[:3], actual.getRgb()[:3])) < 5

    qtbot.waitUntil(assert_background_updated)
    combo.hidePopup()
    dialog.close()


class DropdownBackdrop(QWidget):
    """用彩色区域、细条纹和背景变化检验磨砂，避免纯色背景掩盖材质退化。"""

    def __init__(self):
        """保存背景状态及实际抓取次数，以检查浮层关闭后的采样生命周期。"""
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self.changed = False
        self.grabs = 0

    def grab(self, *args, **kwargs):
        """记录真实背景采样，区分正常窗口重绘与浮层主动抓取。"""
        self.grabs += 1
        return super().grab(*args, **kwargs)

    def paintEvent(self, event):
        """绘制可区分真实模糊、普通半透明和不透明填充的背景。"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#59bda9"))
        painter.fillRect(180, 100, 130, 170, QColor("#e8aa48" if self.changed else "#357bdd"))
        painter.fillRect(310, 100, 140, 170, QColor("#70c883" if self.changed else "#ce69d4"))
        for x in range(180, 440, 4):
            painter.fillRect(x, 130, 4, 50, QColor("#202020" if (x - 180) % 8 else "#e4e4e4"))


def image_color(image, x, y):
    """按逻辑坐标读取截图，保留高 DPI 下的原生像素采样位置。"""
    ratio = image.devicePixelRatio()
    return image.pixelColor(round(x * ratio), round(y * ratio))


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", ["combo", "editable", "icon"])
def test_native_dropdown_preserves_corners_live_blur_and_theme(
    qapp, qtbot, monkeypatch, temp_data_dir, interactive_desktop, mode, kind
):
    """真实点击与重开须同时保留圆角、背景适配和动态磨砂，选项仍可正常提交。"""
    if qapp.platformName() != "windows" or not win_effects.needs_popup_paint_before_blur():
        pytest.skip("需要 Windows 10 桌面合成器")

    from src.ui import combo_wheel, fonts
    from src.ui.application_event_filters import group_application_event_filters, remove_application_event_filter
    from src.ui.background_style import appearance_stylesheet, install_background_style
    from src.ui.widgets.icon_combo import LeadingIconComboBox

    previous_qss, previous_theme = qapp.styleSheet(), theme.current_theme()
    for name in ("_combo_wheel_filter", "_font_inheritance", "_background_style"):
        monkeypatch.setattr(qapp, name, None, raising=False)
    monkeypatch.setattr(acrylic, "_filter_singleton", None)
    with group_application_event_filters(qapp):
        wheel = combo_wheel.install(qapp)
        acrylic.install(qapp)
    with group_application_event_filters(qapp):
        fonts.apply_font(qapp, qapp.font())
        surfaces = install_background_style(qapp)
        qapp.setStyleSheet(appearance_stylesheet())
        theme.set_theme(mode)
        surfaces.apply(False, theme_name=mode)

    owner = DropdownBackdrop()
    owner.setGeometry(160, 120, 720, 480)
    combo = LeadingIconComboBox("actions/game", parent=owner) if kind == "icon" else QComboBox(owner)
    combo.setEditable(kind == "editable")
    combo.addItems(["选项一", "选项二", "选项三", "选项四"])
    combo.setGeometry(100, 60, 320, 38)
    owner.show()
    owner.raise_()
    owner.activateWindow()
    hwnd = None
    try:
        for target_index in (1, 2):
            qtbot.waitUntil(owner.isActiveWindow)
            qtbot.wait(80)
            qtbot.mouseClick(combo, Qt.MouseButton.LeftButton, pos=QPoint(combo.width() - 12, combo.height() // 2))
            popup = combo.view().window()
            qtbot.waitUntil(popup.isVisible)

            def capture_surface():
                """直接捕获桌面合成结果，包含两像素外缘以检查矩形阴影。"""
                interactive_desktop()
                assert popup.isVisible(), "像素验证期间下拉列表必须保持展开"
                ctypes.windll.dwmapi.DwmFlush()
                position = popup.mapToGlobal(QPoint(-2, -2))
                return (
                    popup.screen()
                    .grabWindow(0, position.x(), position.y(), popup.width() + 4, popup.height() + 4)
                    .toImage()
                )

            def assert_corners_clear():
                """四角和窗口外侧须与真实下层内容相同，不允许模糊叠色或矩形阴影。"""
                actual = capture_surface()
                origin = owner.mapFromGlobal(popup.mapToGlobal(QPoint()))
                underlying = owner.grab(QRect(origin, popup.size()).adjusted(-2, -2, 2, 2)).toImage()
                for point in (
                    popup.rect().topLeft(),
                    popup.rect().topRight(),
                    popup.rect().bottomLeft(),
                    popup.rect().bottomRight(),
                    QPoint(-1, popup.height() // 2),
                    QPoint(popup.width() + 1, popup.height() // 2),
                    QPoint(popup.width() // 2, popup.height() + 1),
                ):
                    pixel = image_color(actual, point.x() + 2, point.y() + 2)
                    expected = image_color(underlying, point.x() + 2, point.y() + 2)
                    assert pixel == expected, (
                        f"下拉框边缘 {point} 不能叠色或残留矩形阴影：{pixel.name()} != {expected.name()}"
                    )

            def assert_frosted_surface():
                """加厚底色后仍需模糊细条纹，仅保留宽色带的轻微差异。"""
                actual = capture_surface()
                striped = [image_color(actual, x + 2, 57).lightness() for x in range(120, 280)]
                assert max(striped) - min(striped) < 5, "必须保留背景模糊，普通半透明仍会显露细条纹"
                blue = image_color(actual, 122, 100)
                purple = image_color(actual, 262, 100)
                assert 5 < sum(abs(a - b) for a, b in zip(blue.getRgb()[:3], purple.getRgb()[:3])) < 20, (
                    "不能用不透明底色代替磨砂"
                )
                assert (blue.lightness() > 180) if mode == "light" else (blue.lightness() < 100)

            qtbot.waitUntil(assert_corners_clear, timeout=2000)
            qtbot.waitUntil(assert_frosted_surface, timeout=2000)
            assert_clipped_window(popup)
            if hwnd is not None:
                assert int(popup.winId()) == hwnd, "重开缓存下拉框不能重建原生窗口"
            hwnd = int(popup.winId())
            before = image_color(capture_surface(), 122, 100)
            owner.changed = not owner.changed
            owner.update()

            def assert_live_backdrop():
                """下层内容变化后，面板仍需显示当前背景的模糊结果。"""
                after = image_color(capture_surface(), 122, 100)
                assert sum(abs(a - b) for a, b in zip(before.getRgb()[:3], after.getRgb()[:3])) > 12

            qtbot.waitUntil(assert_live_backdrop, timeout=2000)
            assert_corners_clear()
            combo.view().setCurrentIndex(combo.model().index(target_index, 0))
            qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
            assert combo.currentIndex() == target_index
            assert not popup.isVisible()
            qtbot.wait(80)
            stopped_grabs = owner.grabs
            qtbot.wait(100)
            assert owner.grabs == stopped_grabs, "下拉框关闭后应停止背景采样"
    finally:
        owner.close()
        owner.deleteLater()
        from PySide6.QtCore import QEvent

        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        surfaces.dispose()
        for handler in (wheel, acrylic._filter_singleton, qapp._font_inheritance):
            remove_application_event_filter(qapp, handler)
            handler.deleteLater()
        theme.set_theme(previous_theme)
        qapp.setStyleSheet(previous_qss)
