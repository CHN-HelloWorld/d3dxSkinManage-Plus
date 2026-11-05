"""将宿主语义样式绑定到插件控件，并随主题和插件生命周期更新。"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Any
from weakref import ReferenceType, ref

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from src.plugin_sdk.disposable import Disposable
from src.plugin_sdk.ui_ext import STYLE_ROLES
from src.ui import icons, settings_style, theme


def stylesheet(role: str) -> str:
    """按当前设置页配色和统一尺寸生成仅作用于指定语义控件的样式。"""
    if role not in STYLE_ROLES:
        raise ValueError(f"Unknown plugin style role: {role}")
    c = settings_style.color
    selector = f'QWidget[pluginSdkRole="{role}"]'
    body = theme.font("size_body")
    secondary = theme.font("size_secondary")
    border = settings_style.stroke()
    radius = theme.radius("card")
    if role == "card":
        return f"{selector} {{ background: {c('surface')}; border: 1px solid {border}; border-radius: {radius}px; }}"
    if role in {"title", "subtitle", "muted", "heading"}:
        size, ink, weight = {
            "title": (23, "text", 600),
            "subtitle": (secondary, "secondary", 400),
            "muted": (theme.font("size_caption"), "muted", 400),
            "heading": (theme.font("size_subtitle"), "text", 600),
        }[role]
        return (
            f"{selector} {{ background: transparent; border: none; color: {c(ink)};"
            f" font-size: {size}px; font-weight: {weight}; }}"
        )
    if role in {"button", "primary_button"}:
        primary = role == "primary_button"
        fill, ink = (c("accent"), c("on_accent")) if primary else (c("surface"), c("text"))
        line = c("accent") if primary else settings_style.stroke("control")
        hover = theme.color("accent.hover") if primary else c("hover")
        return f"""
            {selector} {{ background: {fill}; color: {ink}; border: 1px solid {line};
                border-radius: 6px; padding: 7px 14px; min-height: 20px; font-size: {body}px; }}
            {selector}:hover {{ background: {hover}; }}
            {selector}:focus {{ border: 1px solid {c("accent")}; }}
            {selector}:disabled {{ background: {c("soft")}; color: {c("muted")}; border-color: {border}; }}
        """
    if role == "input":
        arrow = (icons.write_themed_svg("actions/chevron-down", "settings.secondary") or "").replace("\\", "/")
        return f"""
            {selector} {{ background: {c("surface")}; color: {c("text")};
                border: 1px solid {settings_style.stroke("control")};
                border-radius: 6px; padding: 6px 9px; min-height: 24px; font-size: {body}px; }}
            {selector}:focus {{ border-color: {c("accent")}; }}
            {selector}:disabled {{ background: {c("soft")}; color: {c("muted")}; }}
            QComboBox[pluginSdkRole="input"] {{ padding-right: 27px; }}
            QComboBox[pluginSdkRole="input"]::drop-down {{ width: 23px; background: transparent; border: none; }}
            QComboBox[pluginSdkRole="input"]::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
            {selector} QAbstractItemView {{ background: {c("surface")}; color: {c("text")};
                selection-background-color: {c("accent_soft")}; selection-color: {c("accent")}; }}
        """
    if role == "table":
        return f"""
            {selector} {{ background: {c("surface")}; alternate-background-color: {c("soft")}; color: {c("text")};
                border: 1px solid {border}; border-radius: 6px; gridline-color: {border}; font-size: {secondary}px;
                selection-background-color: {c("accent_soft")}; selection-color: {c("text")}; }}
            {selector} QHeaderView::section {{ background: {c("soft")}; color: {c("secondary")};
                border: none; border-bottom: 1px solid {border}; padding: 8px; font-weight: 600; }}
            {selector} QTableCornerButton::section {{ background: {c("soft")}; border: none; }}
            {selector}::item {{ padding: 5px 8px; }}
        """
    return f"""
        {selector} {{ background: {c("line")}; color: {c("text")}; border: none; border-radius: 3px;
            min-height: 6px; text-align: center; }}
        {selector}::chunk {{ background: {c("accent")}; border: none; border-radius: 3px; }}
    """


@dataclass
class _Binding:
    """保存弱控件引用及恢复原样式所需的快照。"""

    widget: ReferenceType[QWidget]
    role: str
    previous_style: str
    previous_role: Any


class PluginStyleBindings:
    """为每个插件维护可释放的语义样式，不延长控件的生命周期。"""

    def __init__(self) -> None:
        """初始化绑定编号和插件归属索引。"""
        self._tokens = count()
        self._bindings: dict[str, dict[int, _Binding]] = {}

    def bind(self, plugin_id: str, widget: QWidget, role: str) -> Disposable:
        """绑定有效控件，替换其旧绑定，返回可恢复原样式的释放句柄。"""
        if role not in STYLE_ROLES:
            raise ValueError(f"Unknown plugin style role: {role}")
        if not isinstance(widget, QWidget) or not shiboken6.isValid(widget):
            raise TypeError("Plugin style target must be a live QWidget")
        for owner, bindings in list(self._bindings.items()):
            for token, binding in list(bindings.items()):
                if binding.widget() is widget:
                    self._release(owner, token)
        token = next(self._tokens)
        binding = _Binding(ref(widget), role, widget.styleSheet(), widget.property("pluginSdkRole"))
        self._bindings.setdefault(plugin_id, {})[token] = binding
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply(binding)

        def destroyed(_object=None):
            """控件销毁时移除弱绑定，不访问已经失效的 Qt 对象。"""
            self._release(plugin_id, token)

        widget.destroyed.connect(destroyed)
        return Disposable(lambda: self._release(plugin_id, token))

    @staticmethod
    def _apply(binding: _Binding) -> None:
        """为仍存活的控件应用当前主题，保留控件输入和对象身份。"""
        widget = binding.widget()
        if widget is not None and shiboken6.isValid(widget):
            widget.setProperty("pluginSdkRole", binding.role)
            widget.setStyleSheet(stylesheet(binding.role))

    def _release(self, plugin_id: str, token: int) -> None:
        """移除指定绑定并恢复仍存活控件的原样式，重复释放无副作用。"""
        owned = self._bindings.get(plugin_id, {})
        binding = owned.pop(token, None)
        if not owned:
            self._bindings.pop(plugin_id, None)
        if binding is not None:
            widget = binding.widget()
            if widget is not None and shiboken6.isValid(widget):
                widget.setProperty("pluginSdkRole", binding.previous_role)
                widget.setStyleSheet(binding.previous_style)

    def retheme(self) -> None:
        """重新生成活动绑定样式，并清除已经销毁的控件记录。"""
        for plugin_id, bindings in list(self._bindings.items()):
            for token, binding in list(bindings.items()):
                widget = binding.widget()
                if widget is None or not shiboken6.isValid(widget):
                    self._release(plugin_id, token)
                else:
                    self._apply(binding)

    def cleanup_plugin(self, plugin_id: str) -> None:
        """停止插件全部控件的主题更新，并释放其绑定记录。"""
        for token in list(self._bindings.get(plugin_id, {})):
            self._release(plugin_id, token)
