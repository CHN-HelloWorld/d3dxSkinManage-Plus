"""录制键盘组合，并统一转换为 3DMigoto 按键词项。"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from src.core.key_tokens import friendly_name
from src.ui import icons
from src.utils.i18n import t

# Qt.Key → 3DMigoto 主键 token（不含修饰前缀）
_QT_KEY_MAP: dict = {
    Qt.Key.Key_Up: "VK_UP",
    Qt.Key.Key_Down: "VK_DOWN",
    Qt.Key.Key_Left: "VK_LEFT",
    Qt.Key.Key_Right: "VK_RIGHT",
    Qt.Key.Key_Return: "VK_RETURN",
    Qt.Key.Key_Enter: "VK_RETURN",
    Qt.Key.Key_Space: "VK_SPACE",
    Qt.Key.Key_Tab: "VK_TAB",
    Qt.Key.Key_Backtab: "VK_TAB",
    Qt.Key.Key_Backspace: "VK_BACK",
    Qt.Key.Key_Delete: "VK_DELETE",
    Qt.Key.Key_Insert: "VK_INSERT",
    Qt.Key.Key_Home: "VK_HOME",
    Qt.Key.Key_End: "VK_END",
    Qt.Key.Key_PageUp: "VK_PRIOR",
    Qt.Key.Key_PageDown: "VK_NEXT",
    Qt.Key.Key_CapsLock: "VK_CAPITAL",
    Qt.Key.Key_NumLock: "VK_NUMLOCK",
    Qt.Key.Key_ScrollLock: "VK_SCROLL",
    Qt.Key.Key_Pause: "VK_PAUSE",
    Qt.Key.Key_Print: "VK_SNAPSHOT",
    Qt.Key.Key_Menu: "VK_APPS",
    Qt.Key.Key_Minus: "VK_OEM_MINUS",
    Qt.Key.Key_Equal: "VK_OEM_PLUS",
    Qt.Key.Key_Plus: "VK_OEM_PLUS",
    Qt.Key.Key_Comma: "VK_OEM_COMMA",
    Qt.Key.Key_Period: "VK_OEM_PERIOD",
    Qt.Key.Key_Semicolon: "VK_OEM_1",
    Qt.Key.Key_Slash: "VK_OEM_2",
    Qt.Key.Key_QuoteLeft: "VK_OEM_3",
    Qt.Key.Key_BracketLeft: "VK_OEM_4",
    Qt.Key.Key_Backslash: "VK_OEM_5",
    Qt.Key.Key_BracketRight: "VK_OEM_6",
    Qt.Key.Key_Apostrophe: "VK_OEM_7",
    **{getattr(Qt.Key, f"Key_F{i}"): f"VK_F{i}" for i in range(1, 25)},
}

_KEYPAD_MAP = {
    **{int(Qt.Key.Key_0) + i: f"VK_NUMPAD{i}" for i in range(10)},
    Qt.Key.Key_Plus: "VK_ADD",
    Qt.Key.Key_Minus: "VK_SUBTRACT",
    Qt.Key.Key_Asterisk: "VK_MULTIPLY",
    Qt.Key.Key_Slash: "VK_DIVIDE",
    Qt.Key.Key_Period: "VK_DECIMAL",
    Qt.Key.Key_Comma: "VK_SEPARATOR",
}


def qt_key_to_token(key: int, modifiers, native_virtual_key: int = 0, win_key: str = "VK_LWIN") -> str:
    """把一次按键事件映射成 3DMigoto token；未知键返回空串。"""
    base = _KEYPAD_MAP.get(key) if modifiers & Qt.KeyboardModifier.KeypadModifier else None
    if base is None:
        base = _QT_KEY_MAP.get(key)
    # 原生虚拟键保留不同布局和组合标点的物理键身份。
    if native_virtual_key and 0xBA <= native_virtual_key <= 0xE2:
        base = f"0x{native_virtual_key:02X}"
    elif 0x30 <= native_virtual_key <= 0x39:
        base = chr(native_virtual_key)
    if base is None:
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            base = chr(int(key)).lower()  # 字母 → 小写单字母
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            base = chr(int(key))  # 数字
        else:
            return ""
    prefix = []
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        prefix.append("ctrl")
    if modifiers & Qt.KeyboardModifier.AltModifier:
        prefix.append("alt")
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        prefix.append("shift")
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        prefix.append(win_key)
    return " ".join(prefix + [base]) if prefix else base


class KeyCaptureField(QWidget):
    """显示一个键值；点击进入捕获态，按键即映射写回；✎ 切手动文本框回退处理。"""

    token_changed = Signal(str)  # 新 token 确定后发出

    _MODIFIER_ONLY = {
        Qt.Key.Key_Control,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Meta,
    }

    def __init__(self, token: str = "", parent: QWidget | None = None) -> None:
        """记录原始按键标记，创建组合键录制和手动输入按钮，并初始化按下键集合。"""
        super().__init__(parent)
        self._token = token
        self._capturing = False
        self._chord: list[str] = []
        self._pressed_keys: set[int] = set()
        self._win_key = "VK_LWIN"
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._btn = QPushButton(self)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self.enter_capture)
        lay.addWidget(self._btn, 1)

        self._edit = QPushButton(self)
        icons.apply_icon(self._edit, "actions/edit")
        self._edit.setIconSize(QSize(14, 14))
        self._edit.setFixedWidth(24)
        self._edit.setAccessibleName(t("手动输入"))
        self._edit.clicked.connect(self._enter_manual)
        lay.addWidget(self._edit)

        self._refresh()

    # 公开 API
    def token(self) -> str:
        """返回当前已提交的原始按键标记。"""
        return self._token

    def display_text(self) -> str:
        """返回录制按钮当前显示的友好键名或录制提示。"""
        return self._btn.text()

    def enter_capture(self) -> None:
        """清空上一轮录制状态并获取焦点；控件可见时独占键盘直到录制提交或取消。"""
        self._capturing = True
        self._chord.clear()
        self._pressed_keys.clear()
        self._win_key = "VK_LWIN"
        self._btn.setText(t("按住组合键，松开完成（Esc 取消）"))
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self.isVisible():
            self.grabKeyboard()

    # 内部
    def _refresh(self) -> None:
        """用友好键名更新按钮文字，空绑定显示占位符。"""
        self._btn.setText(friendly_name(self._token) or "—")

    def _commit(self, token: str) -> None:
        """结束录制并释放键盘，仅在非空绑定发生变化时保存并发出 token_changed 信号。"""
        self._capturing = False
        if QWidget.keyboardGrabber() is self:
            self.releaseKeyboard()
        if token and token != self._token:
            self._token = token
            self._refresh()
            self.token_changed.emit(token)
        else:
            self._refresh()

    def _cancel(self) -> None:
        """取消录制、释放键盘并恢复原绑定的显示文字。"""
        self._capturing = False
        if QWidget.keyboardGrabber() is self:
            self.releaseKeyboard()
        self._refresh()

    def _handle_captured(self, key: int, modifiers, native_virtual_key: int = 0) -> None:
        """处理捕获的按键，更新组合键状态；Esc 取消捕获。"""
        if key == Qt.Key.Key_Escape:
            self._cancel()
            return
        if key in self._MODIFIER_ONLY:
            if key == Qt.Key.Key_Meta:
                self._win_key = "VK_RWIN" if native_virtual_key == 0x5C else "VK_LWIN"
            if self._chord:
                part = {
                    Qt.Key.Key_Control: "ctrl",
                    Qt.Key.Key_Alt: "alt",
                    Qt.Key.Key_Shift: "shift",
                    Qt.Key.Key_Meta: self._win_key,
                }[key]
                if part not in self._chord:
                    self._chord.insert(0, part)
                    self._btn.setText(friendly_name(" ".join(self._chord)) + " …")
            return  # 等真正的主键
        token = qt_key_to_token(key, modifiers, native_virtual_key, self._win_key)
        if token:
            self._pressed_keys.add(key)
            for part in token.split():
                if part not in self._chord:
                    self._chord.append(part)
            self._btn.setText(friendly_name(" ".join(self._chord)) + " …")

    def _enter_manual(self) -> None:
        """取消当前录制后打开文本输入框，将组合键中的加号分隔符规范化为空格再提交。"""
        if self._capturing:
            self._cancel()
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self,
            t("手动输入"),
            t(
                "组合键用空格分隔（也可输入 Ctrl+Alt+X）。\n键盘：ctrl alt x / VK_F10 / VK_LWIN x\n小键盘：VK_DECIMAL "
                "VK_NUMPAD0；鼠标：VK_XBUTTON1\n手柄：XB_A / XB2_LEFT_TRIGGER；排除键：no_alt F1\nEsc 请手动输入 "
                "VK_ESCAPE："
            ),
            text=self._token,
        )
        if ok and text.strip():
            # 加号自身也是小键盘加法键别名，仅替换组合键分隔符。
            self._commit(re.sub(r"(?<=\S)\s*\+\s*(?=[^+\s])", " ", text.strip()))

    # Qt 事件
    def event(self, event) -> bool:
        # 在应用快捷键前捕获组合键，包括退出和制表键。
        """录制期间接管快捷键覆盖和按键按下事件，避免应用快捷键截走组合键。"""
        if self._capturing and event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if self._capturing and event.type() == QEvent.Type.KeyPress:
            # Qt 在按键处理前执行制表键焦点遍历，需在事件入口捕获。
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        """录制期间处理非自动重复按键并吞入事件，其余状态保留默认按键行为。"""
        if self._capturing:
            if not event.isAutoRepeat():
                self._handle_captured(event.key(), event.modifiers(), event.nativeVirtualKey())
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        """录制中的已按下键首次释放时提交完整组合键，并阻止事件继续传播。"""
        if self._capturing:
            if not event.isAutoRepeat() and event.key() in self._pressed_keys:
                self._commit(" ".join(self._chord))
            event.accept()
            return
        super().keyReleaseEvent(event)

    def hideEvent(self, event) -> None:
        """控件隐藏时取消录制并释放键盘，避免隐藏控件持有全局输入。"""
        if self._capturing:
            self._cancel()
        super().hideEvent(event)

    def focusOutEvent(self, event) -> None:
        """焦点离开时取消录制并恢复原绑定显示。"""
        if self._capturing:
            self._cancel()
        super().focusOutEvent(event)
