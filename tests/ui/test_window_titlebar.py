"""标题栏换色不应强制同步重绘，也不应干扰无边框窗口的拖动。"""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from src.ui import win_effects
from tests.ui.test_window_corners import interactive_desktop as interactive_desktop


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("active", [True, False])
def test_titlebar_repaints_without_changing_foreground_window(monkeypatch, enabled, active):
    """模拟原生标题栏缓存，验证明暗更新及原有活动状态恢复。"""
    hwnd = 0x123456789
    foreground = hwnd if active else hwnd + 1
    caption = {"active": active, "dark": not enabled}
    attributes = {}

    def set_attr(handle, attr, value):
        """记录 DWM 属性，模拟设置属性后尚未重绘的系统行为。"""
        assert handle == hwnd
        attributes[attr] = value
        return True

    def get_foreground():
        """返回不随非客户区重绘变化的前台窗口句柄。"""
        return foreground

    def send_message(handle, message, wparam, lparam):
        """模拟活动状态变化触发的非客户区重绘，并校验消息参数。"""
        assert handle.value == hwnd
        assert message == 0x0086  # WM_NCACTIVATE 更新非客户区，不切换键盘焦点。
        assert lparam.value == -1, "禁止 DefWindowProc 在激活消息内同步重绘非客户区"
        if caption["active"] != bool(wparam.value):
            caption["active"] = bool(wparam.value)
            caption["dark"] = bool(attributes[20])
        return 1

    user32 = SimpleNamespace(GetForegroundWindow=get_foreground, SendMessageW=send_message)
    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    monkeypatch.setattr(win_effects.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)

    window = SimpleNamespace(
        winId=lambda: hwnd,
        isVisible=lambda: True,
        windowFlags=lambda: Qt.WindowType.Window | Qt.WindowType.WindowTitleHint,
    )
    assert win_effects.apply_dark_titlebar(window, enabled)

    assert caption == {"active": active, "dark": enabled}
    assert get_foreground() == foreground


@pytest.mark.parametrize(
    "visible,flags",
    [
        (False, Qt.WindowType.Window | Qt.WindowType.WindowTitleHint),
        (True, Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.FramelessWindowHint),
        (True, Qt.WindowType.ToolTip),
        (True, Qt.WindowType.Popup),
    ],
)
def test_titlebar_does_not_activate_hidden_or_captionless_windows(monkeypatch, visible, flags):
    """DWM 设置成功也不能给隐藏窗口和自绘标题栏引入合成状态切换。"""
    attributes, messages = [], []

    def set_attr(hwnd, attr, value):
        """保留真实调用方的窗口属性请求，以验证成功路径的刷新边界。"""
        attributes.append((hwnd, attr, value))
        return True

    def get_foreground():
        """前台窗口固定为另一个窗口，便于观察额外的激活消息。"""
        return 456

    def send_message(*args):
        """记录调用方送出的消息，不替调用方判断窗口类型。"""
        messages.append(args)
        return 1

    user32 = SimpleNamespace(GetForegroundWindow=get_foreground, SendMessageW=send_message)
    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    monkeypatch.setattr(win_effects.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    window = SimpleNamespace(winId=lambda: 123, isVisible=lambda: visible, windowFlags=lambda: flags)

    assert win_effects.apply_dark_titlebar(window, True)
    assert attributes == [(123, 20, 1)]
    assert messages == [], "无原生标题栏可刷新的窗口不能切换非客户区激活状态"


def test_older_win10_titlebar_uses_supported_attribute(monkeypatch):
    attributes = []

    def set_attr(_hwnd, attr, value):
        """只接受早期 Win10 的属性编号，覆盖兼容回退路径。"""
        attributes.append((attr, value))
        return attr == 19

    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    # 原生重绘失败时，已成功设置的标题栏属性仍然有效。
    monkeypatch.setattr(win_effects.ctypes, "windll", SimpleNamespace(), raising=False)

    assert win_effects.apply_dark_titlebar(SimpleNamespace(winId=lambda: 123), True)
    assert attributes == [(20, 1), (19, 1)]


def test_unsupported_titlebar_does_not_send_native_messages(monkeypatch):
    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: False)
    monkeypatch.setattr(win_effects.ctypes, "windll", SimpleNamespace(), raising=False)

    assert not win_effects.apply_dark_titlebar(SimpleNamespace(winId=lambda: 123), True)


def test_native_win10_titlebar_updates_before_show_and_without_moving(qapp, qtbot, interactive_desktop):
    """用实际桌面像素检查首次显示和来回换色，同时检查焦点、句柄及几何保持不变。"""
    if not win_effects.needs_popup_paint_before_blur():
        pytest.skip("需要 Windows 10 标题栏合成器")

    import winreg

    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QLineEdit, QMainWindow

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            if winreg.QueryValueEx(key, "ColorPrevalence")[0]:
                pytest.skip("系统强调色覆盖标题栏明暗色，无法按默认颜色验证")
    except FileNotFoundError:
        pass

    window = QMainWindow()
    qtbot.addWidget(window)
    window.setWindowTitle("标题栏验证")
    window.resize(640, 300)
    editor = QLineEdit(window)
    window.setCentralWidget(editor)
    hwnd = int(window.winId())
    assert win_effects.apply_dark_titlebar(window, True)
    window.show()
    window.raise_()
    window.activateWindow()
    editor.setFocus()
    qtbot.waitUntil(window.isActiveWindow)

    def caption_is_dark(dark):
        """读取原生标题栏的空白区域，避免把 QWidget 客户区截图当作系统绘制结果。"""
        interactive_desktop()
        assert window.isVisible()
        margins = window.windowHandle().frameMargins()
        assert margins.top() > 0
        point = window.mapToGlobal(QPoint(window.width() // 2, -max(1, margins.top() // 2)))
        win_effects.ctypes.windll.dwmapi.DwmFlush()
        image = window.screen().grabWindow(0, point.x(), point.y(), 1, 1).toImage()
        assert not image.isNull()
        lightness = image.pixelColor(0, 0).lightness()
        return lightness < 80 if dark else lightness > 200

    qtbot.waitUntil(lambda: caption_is_dark(True))
    geometry = window.geometry()
    for dark in (False, True, False):
        assert win_effects.apply_dark_titlebar(window, dark)
        qtbot.waitUntil(lambda: caption_is_dark(dark))
        assert window.geometry() == geometry
        assert int(window.winId()) == hwnd
        assert window.isActiveWindow()
        assert qapp.focusWidget() is editor
