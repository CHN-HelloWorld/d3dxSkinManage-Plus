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
