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


def _style_menu(menu: QMenu) -> None:
    """为尚未处理的普通菜单安装透明背景样式，保留自定义菜单的专用外观。"""
    if menu.objectName() in {"catPickerMenu", "modUpdatesMenu"}:
        return  # 该菜单已自定义（外层亚克力 + 内部透明），勿覆盖
    if not _mark_once(menu):
        return
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setStyleSheet(_menu_qss(acrylic=menu.objectName() == "acrylicMenu"))


def _prepare_menu_window_owner(menu: QMenu) -> None:
    """绑定顶层临时父窗口，避免将非原生的卡片和滚动容器原生化。"""
    menu.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
    owner = menu.parentWidget()
    if owner is not None and not owner.isWindow() and owner.testAttribute(Qt.WidgetAttribute.WA_NativeWindow):
        # Qt 会在显示前选择最近的原生父控件，已有原生所有者时提前避免临时父窗口类型错误。
        menu.setParent(owner.window(), menu.windowFlags())
        owner.destroyed.connect(menu.close)
        owner.destroyed.connect(menu.deleteLater)
    handle = menu.windowHandle()
    if owner is None or handle is None:
        return
    # QWidget 重挂会创建旧父链的原生窗口，破坏卡片图形效果；原生归属不改变对象生命周期。
    top_handle = owner.window().windowHandle()
    if top_handle is not None and handle.transientParent() is not top_handle:
        handle.setTransientParent(top_handle)


def prepare_acrylic_menu(menu: QMenu) -> None:
    """在原生窗口创建前准备显式亚克力菜单，并允许主题切换后就地重涂。"""
    menu.setObjectName("acrylicMenu")
    if _mark_once(menu):
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setStyleSheet(_menu_qss(acrylic=True))


class _ComboAcrylicSurface(QObject):
    """统一下拉框背景绘制，并在系统模糊无法裁切时使用实时采样的磨砂表面。"""

    def __init__(self, container: QWidget, *, sampled: bool = False) -> None:
        """把背景绘制过滤器绑定到组合框弹出容器的生命周期。"""
        super().__init__(container)
        self.backdrop = None
        if sampled:
            from src.ui.popup_backdrop import PopupBackdrop

            self.backdrop = PopupBackdrop(container)
        container.installEventFilter(self)

    def eventFilter(self, obj, event):
        """绘制浮层背景；采样材质接管容器表面，列表子控件仍由 Qt 绘制。"""
        if event.type() != QEvent.Type.Paint:
            return False
        if self.backdrop is not None:
            self._paint_sampled_surface(obj)
            return True
        combo = obj.parentWidget()
        if not isinstance(combo, QComboBox):
            return False
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        if combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, option, combo) and obj.mask().isEmpty():
            return False  # 该模式下 Qt 已绘制菜单面板。
        # 可编辑下拉框仅绘制边框，透明窗口不会自动填充背景。
        painter = QPainter(obj)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        path = QPainterPath()
        path.addRoundedRect(QRectF(obj.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        tint = QColor(theme.color("bg.card"))
        tint.setAlphaF(acrylic_surface_alpha(0.84))
        painter.fillPath(path, tint)
        painter.end()
        return False

    def _paint_sampled_surface(self, container: QWidget) -> None:
        """在同一圆角路径内合成磨砂与叠色，仅绘制一层边框并清空外侧像素。"""
        painter = QPainter(container)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(container.rect(), Qt.GlobalColor.transparent)
        path = QPainterPath()
        path.addRoundedRect(QRectF(container.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        painter.setClipPath(path)
        painter.drawImage(container.rect(), self.backdrop.image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        tint = QColor(theme.color("bg.card"))
        tint.setAlphaF(acrylic_surface_alpha(0.84))
        painter.fillPath(path, tint)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(theme.color("stroke.card")), 1.0))
        painter.drawPath(path)
        painter.end()


def _style_combo_popup(container) -> None:
    # 底色放在容器上，覆盖滚动条及列表尚未绘制的区域；内部视图透明。
    """一次性设置弹出容器与列表的透明样式，并保活自绘背景过滤器。"""
    if not _mark_once(container):
        return
    from src.ui import win_effects

    sampled = win_effects.needs_popup_paint_before_blur()
    if sampled:
        # 分层窗口须按无边框路径上传预乘透明像素，系统矩形阴影不能用于圆角表面。
        container.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        container.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
    container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # 临时容器由 Qt 管理，事件过滤器由长期存在的下拉框持有，避免包装对象回收后丢失绘制回调。
    combo = container.parentWidget()
    if isinstance(combo, QComboBox):
        combo._combo_acrylic_surface = _ComboAcrylicSurface(container, sampled=sampled)
    container.setStyleSheet(_combo_container_qss())
    view = container.findChild(QAbstractItemView)
    if view is not None:
        # 只保留容器这一层亚克力，避免列表重复叠色。
        view.setStyleSheet(_combo_view_qss())
        # 视口需保持透明，防止自动填充的不透明矩形遮盖外层圆角和亚克力效果。
        vp = view.viewport()
        if vp is not None:
            vp.setAutoFillBackground(False)
            vp.setStyleSheet("background: transparent;")
