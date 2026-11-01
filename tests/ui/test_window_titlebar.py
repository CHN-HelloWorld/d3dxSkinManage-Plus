"""标题栏设置必须立即重绘，并保留窗口原有的活动状态。"""

from types import SimpleNamespace

import pytest

from src.ui import win_effects


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
        assert lparam.value == 0
        if caption["active"] != bool(wparam.value):
            caption["active"] = bool(wparam.value)
            caption["dark"] = bool(attributes[20])
        return 1

    user32 = SimpleNamespace(GetForegroundWindow=get_foreground, SendMessageW=send_message)
    monkeypatch.setattr(win_effects, "_supported", lambda: True)
    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    monkeypatch.setattr(win_effects.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)

    assert win_effects.apply_dark_titlebar(SimpleNamespace(winId=lambda: hwnd), enabled)

    assert caption == {"active": active, "dark": enabled}
    assert get_foreground() == foreground


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
