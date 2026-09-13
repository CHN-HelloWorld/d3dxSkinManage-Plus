"""管理器快捷键的 Qt 通知与焦点分派；不注册系统全局热键。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from weakref import WeakKeyDictionary, ref

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

from src.core.config import Config
from src.core.shortcuts import ShortcutStore, normalize_binding
from src.utils.i18n import t as tr

_MODIFIER_KEYS = {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr}
_SHIFTED_SYMBOLS = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))


def binding_from_event(event: QKeyEvent) -> str:
    """使用与配置一致的主键名称；左右修饰键、数字键盘等价。"""
    modifiers = event.modifiers()
    if (
        event.key() in _MODIFIER_KEYS
        or event.nativeVirtualKey() == 0xE5
        or modifiers & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.GroupSwitchModifier)
    ):
        return ""
    key = event.key()
    if key == Qt.Key.Key_Backtab:
        key = Qt.Key.Key_Tab
        modifiers |= Qt.KeyboardModifier.ShiftModifier
    main = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
    # Windows 的 Shift 标点事件报告变换后的符号，显示时还原物理主键。
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        main = _SHIFTED_SYMBOLS.get(main, main)
    parts = [
        name
        for flag, name in (
            (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
            (Qt.KeyboardModifier.AltModifier, "Alt"),
            (Qt.KeyboardModifier.ShiftModifier, "Shift"),
        )
        if modifiers & flag
    ]
    return normalize_binding("+".join(parts + [main]))


class ShortcutSettings(QObject):
    """由主窗口持有并注入设置页；仅在成功提交后通知界面与分派器。"""

    changed = Signal()
    catalog_changed = Signal()

    def __init__(self, parent: QObject | None = None, config: Config | None = None):
        """创建快捷键存储，并绑定 Qt 父对象以便发布配置变化信号。"""
        super().__init__(parent)
        self.store = ShortcutStore(config or Config())

    @property
    def commands(self):
        """返回当前内置和插件快捷键命令目录。"""
        return self.store.commands

    @property
    def groups(self):
        """返回快捷键命令的分组目录。"""
        return self.store.groups

    def command(self, command_id):
        """按命令标识获取命令定义，未知标识由存储映射抛出 KeyError。"""
        return self.store.command_by_id[command_id]

    def register_plugin(self, owner, title, commands):
        """注册插件命令后通知目录和绑定变化，使设置页与分发器刷新。"""
        self.store.register_plugin(owner, title, commands)
        self.catalog_changed.emit()
        self.changed.emit()

    def unregister_plugin(self, owner):
        """移除插件命令并通知目录及绑定变化。"""
        self.store.unregister_plugin(owner)
        self.catalog_changed.emit()
        self.changed.emit()

    @property
    def bindings(self) -> dict[str, str | None]:
        """返回当前生效的命令与按键绑定映射。"""
        return self.store.bindings

    @property
    def custom_count(self) -> int:
        """返回偏离默认配置的快捷键数量。"""
        return self.store.custom_count

    @property
    def last_error(self) -> str:
        """返回最近一次快捷键保存失败的说明。"""
        return self.store.last_error

    def binding(self, command_id: str) -> str | None:
        """读取指定命令当前生效的按键序列，未绑定时返回 None。"""
        return self.store.binding(command_id)

    def conflicts(self, command_id: str, binding: str):
        """查找指定候选按键与其他命令在相容上下文中的冲突。"""
        return self.store.conflicts(command_id, binding)

    def save_binding(self, command_id: str, binding: str, *, replace: bool = False) -> bool:
        """保存单项绑定并按需替换冲突，成功后发出配置变化信号。"""
        return self._commit(lambda: self.store.save_binding(command_id, binding, replace=replace))

    def restore_defaults(self) -> bool:
        """恢复默认快捷键配置，持久化成功后通知订阅者。"""
        return self._commit(self.store.restore_defaults)

    def restore(self, snapshot: dict[str, str | None]) -> bool:
        """恢复给定绑定快照，成功保存后通知界面与分发器。"""
        return self._commit(lambda: self.store.restore(snapshot))

    def _commit(self, action: Callable[[], bool]) -> bool:
        """执行存储操作，仅在成功时发出 changed 并返回 True。"""
        if not action():
            return False
        self.changed.emit()
        return True

    def reload(self) -> None:
        """程序设置整体恢复默认后重新发布（Config 已更新，不重复读盘）。"""
        self.changed.emit()


@dataclass
class _Context:
    """保存控件弱引用、快捷键上下文、动作表及对子控件的适用范围。"""

    widget: ref
    name: str
    actions: dict[str, Callable[[], None]]
    children: bool


class ShortcutDispatcher(QObject):
    """先处理焦点所在编辑器，再处理明确登记的列表，最后处理主窗口命令。

    ShortcutOverride 与 KeyPress 使用同一份解析结果，避免 Qt 原生 Ctrl+A/C/X/V
    抢先执行已被改绑或替换的动作。未登记的控件不会操作其他页面残留的选中项。
    """

    def __init__(self, settings: ShortcutSettings, window: QWidget):
        """读取绑定并订阅配置和焦点变化，将按键过滤器安装到当前有效焦点控件。"""
        super().__init__(window)
        self.settings = settings
        self.window = window
        self._contexts: list[_Context] = []
        self._window_actions: dict[str, Callable[[], None]] = {}
        self._plugin_actions: dict[str, tuple[str, Callable[[], None]]] = {}
        self._composing: WeakKeyDictionary = WeakKeyDictionary()
        self._bindings = settings.bindings
        settings.changed.connect(self._reload)
        self._focus_ref = lambda: None
        app = QApplication.instance()
        app.focusChanged.connect(self._focus_changed)
        self._focus_changed(None, app.focusWidget())

    def _focus_changed(self, old, current) -> None:
        # 仅对当前焦点处理按键和输入法，不让定时器、绘制及工作通知进入该分发路径。
        """从旧焦点移除过滤器和输入法状态，再跟踪所属窗口中的新焦点。"""
        previous = self._focus_ref()
        if previous is not None:
            self._composing.pop(previous, None)
            if shiboken6.isValid(previous):
                previous.removeEventFilter(self)
        self._focus_ref = lambda: None
        if current is not None and self._belongs(current):
            current.installEventFilter(self)
            self._focus_ref = ref(current)

    def _reload(self) -> None:
        """在设置变化后重新获取当前生效的按键绑定。"""
        self._bindings = self.settings.bindings

    def register_context(
        self, widget: QWidget, context: str, actions: dict[str, Callable[[], None]], *, children: bool = True
    ) -> None:
        """注册控件的局部动作上下文，并指定是否对后代控件生效。"""
        self._contexts.append(_Context(ref(widget), context, actions, children))

    def register_window_action(self, command_id: str, callback: Callable[[], None]) -> None:
        """为命令标识设置窗口级动作回调。"""
        self._window_actions[command_id] = callback

    def register_plugin_action(self, owner: str, command_id: str, callback: Callable[[], None]) -> None:
        """登记插件动作所有权，拒绝覆盖窗口动作或其他插件已占用的命令。"""
        current = self._plugin_actions.get(command_id)
        if command_id in self._window_actions or current is not None and current[0] != owner:
            raise ValueError(tr("快捷键处理函数已被占用：{command_id}", command_id=command_id))
        self._plugin_actions[command_id] = (owner, callback)

    def unregister_plugin(self, owner: str) -> None:
        """移除指定插件拥有的全部动作回调。"""
        self._plugin_actions = {cid: item for cid, item in self._plugin_actions.items() if item[0] != owner}

    def _belongs(self, widget: QWidget | None) -> bool:
        """沿父控件链判断目标是否属于当前主窗口。"""
        while widget is not None:
            if widget is self.window:
                return True
            widget = widget.parentWidget()
        return False

    def _context(self, focus: QWidget) -> _Context | None:
        """从焦点向上寻找最近可见可用的已注册上下文，并遵守后代适用标记。"""
        current = focus
        while current is not None:
            for context in self._contexts:
                root = context.widget()
                if root is not None and shiboken6.isValid(root) and current is root:
                    if (current is focus or context.children) and root.isVisible() and root.isEnabled():
                        return context
            if current.isWindow():
                break
            current = current.parentWidget()
        return None

    @staticmethod
    def _text_editor(focus: QWidget):
        """从焦点向上寻找文本编辑控件，在顶层窗口边界停止。"""
        current = focus
        while current is not None:
            if isinstance(current, (QLineEdit, QPlainTextEdit, QTextEdit)):
                return current
            # 不越过编辑区域所属的独立窗口。
            if current.isWindow():
                break
            current = current.parentWidget()
        return None

    @staticmethod
    def _text_action(editor, command_id: str) -> None:
        """执行文本选择或剪贴板动作，对只读编辑器屏蔽剪切、粘贴与删除。"""
        if command_id == "edit.select_all":
            editor.selectAll()
        elif command_id == "edit.copy":
            editor.copy()
        elif not editor.isReadOnly():
            if command_id == "edit.cut":
                editor.cut()
            elif command_id == "edit.paste":
                editor.paste()
            elif command_id == "edit.delete":
                if isinstance(editor, QLineEdit):
                    editor.del_()
                else:
                    cursor = editor.textCursor()
                    cursor.deleteChar()
                    editor.setTextCursor(cursor)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """在当前窗口焦点上解析快捷键，按文本、局部和窗口上下文选择动作。

        输入法预编辑、快捷键录制、模态限制和原生输入场景不接管；已被改绑的默认按键被消费，非文本动作忽略自动重复。"""
        if not isinstance(watched, QWidget):
            return False
        kind = event.type()
        if kind == QEvent.Type.InputMethod:
            if self._belongs(watched):
                self._composing[watched] = bool(event.preeditString())
            return False
        if kind == QEvent.Type.FocusOut:
            self._composing.pop(watched, None)
            return False
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        app = QApplication.instance()
        focus = app.focusWidget()
        if (
            focus is None
            or watched is not focus
            or not self._belongs(focus)
            or not self.window.isVisible()
            or self.window.isMinimized()
            or not self._belongs(app.activeWindow())
            or not focus.isEnabled()
        ):
            return False
        # 3DMigoto INI 按键录制拥有输入优先权，不能被管理器命令抢占。
        from src.ui.widgets.key_capture import KeyCaptureField

        if (
            QWidget.keyboardGrabber() is not None
            or focus.window().property("shortcutRecording")
            or isinstance(focus, KeyCaptureField)
            and focus._capturing
        ):
            return False
        editor = self._text_editor(focus)
        if self._composing.get(focus) or editor is not None and self._composing.get(editor):
            return False
        binding = binding_from_event(event)
        if not binding:
            return False
        if editor is not None and not event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        ):
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Delete):
                return False  # 文本输入/删除不是可改派的业务单键。
        modal = app.activeModalWidget()
        if modal is not None and focus.window() is not modal:
            return False
        popup = app.activePopupWidget()
        context = self._context(focus) if editor is None else None
        commands = self.settings.commands
        command_ids = [command.id for command in commands if self._bindings.get(command.id) == binding]
        action = None
        for command_id in command_ids:
            command = self.settings.command(command_id)
            plugin_action = self._plugin_actions.get(command_id)
            if editor is not None and "text" in command.contexts:
                action = partial(self._text_action, editor, command_id)
                break
            if context is not None and command_id in context.actions:
                action = context.actions[command_id]
                break
            if context is not None and context.name in command.contexts and plugin_action is not None:
                action = plugin_action[1]
                break
            if modal is None and popup is None and "window" in command.contexts:
                action = self._window_actions.get(command_id) or (plugin_action[1] if plugin_action else None)
                break
        # 原生控件仍认识旧的 Ctrl+A/C/X/V；改键后必须抑制这些旧入口。
        context_name = "text" if editor is not None else context.name if context else None
        obsolete = any(
            command.default == binding
            and context_name in command.contexts
            and self._bindings.get(command.id) != binding
            for command in commands
            if not command.group.startswith("plugin:")
        )
        if action is None and not obsolete:
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if action is not None and (editor is not None or not event.isAutoRepeat()):
            action()
        event.accept()
        return True
