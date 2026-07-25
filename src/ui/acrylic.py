"""统一菜单、下拉框及对话框的亚克力背景。

独立弹窗须在创建原生窗口前设置透明背景属性，再应用半透明填充和系统模糊。
应用级过滤器在样式准备及显示事件中处理菜单和下拉容器；
菜单底色独立于页面背景设置，以保持操作文字可读。
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QMenu, QStyle, QStyleOptionComboBox, QWidget

from src.ui import theme
from src.ui.win_effects import acrylic_surface_alpha


def _menu_qss(*, acrylic: bool = False) -> str:
    # 操作文字需要比装饰面板更稳定的衬底。
    """生成主题菜单样式，并依据是否使用亚克力调整背景不透明度。"""
    fill = theme.rgba("bg.card", acrylic_surface_alpha(0.72 if acrylic else 0.88))
    return (
        f"QMenu {{ background-color: {fill}; color: {theme.color('text.primary')};"
        f" border: 1px solid {theme.color('stroke.card')}; border-radius: 8px; padding: 4px; }}"
        f"QMenu::item {{ padding: 6px 16px 6px 10px; border-radius: 4px; }}"
        f"QMenu::item:selected {{ background-color: {theme.rgba('accent', 0.40)}; }}"
        f"QMenu::item:disabled {{ color: {theme.color('text.disabled')}; }}"
        f"QMenu::separator {{ height: 1px; background: {theme.color('stroke.divider')};"
        f" margin: 5px 8px; }}"
    )


_MARK = "_acrylic_styled"


def _mark_once(w) -> bool:
    """标记弹层是否首次完成结构设置，首次返回 True。

    透明背景会重建原生窗口，样式修改又会触发 Polish，结构初始化只能执行一次，
    避免递归及中断鼠标抓取。主题切换只更新颜色，不清除此标记。
    """
    if w.property(_MARK):
        return False
    w.setProperty(_MARK, True)
    return True


def _combo_container_qss() -> str:
    """生成组合框弹出容器的圆角、边框和半透明底色样式。"""
    return (
        f"QComboBoxPrivateContainer {{ background: {theme.rgba('bg.card', acrylic_surface_alpha(0.84))};"
        f" border: 1px solid {theme.color('stroke.card')}; border-radius: 8px; }}"
    )


def _combo_view_qss() -> str:
    """返回列表视图的透明圆角样式，使容器负责绘制背景。"""
    return "background-color: transparent; border: none; border-radius: 8px;"


def _style_tooltip(tooltip: QWidget) -> None:
    """提示窗使用高对比文字和较厚的亚克力底色，隔离祖先的内联样式。"""
    # Qt 的 QTipLabel 会继承触发控件的样式；祖先的无选择器 background:transparent
    # 比全局 QToolTip 规则优先，原生提示窗因此成为黑底。只在提示窗本身覆盖，
    # 不修改触发控件；透明属性只准备一次，原生效果等 Show 后再施加。
    if _mark_once(tooltip):
        tooltip.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    qss = (
        f"QToolTip {{ background-color: {theme.rgba('bg.card', acrylic_surface_alpha(0.92))};"
        f" color: {theme.color('text.primary')};"
        f" border: 1px solid {theme.color('stroke.card')};"
        f" padding: 4px 8px; border-radius: {theme.radius('control')}px; }}"
    )
    if tooltip.styleSheet() != qss:
        tooltip.setStyleSheet(qss)


def reset_for_theme_change() -> None:
    """主题或背景切换后，就地更新已缓存弹层的内联颜色。

    QComboBoxPrivateContainer 和 QMenu 由 Qt 按需创建并长期缓存，需显式更新内联主题色。
    保留浮层材质及 WA_TranslucentBackground，避免重建原生窗口并中断鼠标捕获。
    setStyleSheet 触发的 Polish 可能再次进入过滤器，`_mark_once` 标记负责阻止重复处理。
    """
    from PySide6.QtWidgets import QApplication

    for w in QApplication.allWidgets():
        try:
            if w.metaObject().className() == "QTipLabel":
                _style_tooltip(w)
                if w.isVisible():
                    _prepare_popup_effects(w, small=True)
                continue
            if not w.property(_MARK):
                continue
            if isinstance(w, QMenu):
                explicit = w.objectName() == "acrylicMenu"
                qss = _menu_qss(acrylic=explicit)
                if w.styleSheet() != qss:
                    w.setStyleSheet(qss)
            elif w.metaObject().className() == "QComboBoxPrivateContainer":
                # 主题切换时更新弹窗颜色，显示时按当前主题重新配置模糊效果。
                view = w.findChild(QAbstractItemView)
                qss = _combo_container_qss()
                if w.styleSheet() != qss:
                    w.setStyleSheet(qss)
                if view is not None:
                    qss = _combo_view_qss()
                    if view.styleSheet() != qss:
                        view.setStyleSheet(qss)
                    vp = view.viewport()
                    if vp is not None and vp.styleSheet() != "background: transparent;":
                        vp.setStyleSheet("background: transparent;")
        except Exception:
            pass
