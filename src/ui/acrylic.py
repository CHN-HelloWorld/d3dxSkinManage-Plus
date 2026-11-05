"""统一菜单、下拉框及对话框的亚克力背景。

独立弹窗须在创建原生窗口前设置透明背景属性，再应用半透明填充和系统模糊。
应用级过滤器在样式准备及显示事件中处理菜单和下拉容器；
菜单底色独立于页面背景设置，以保持操作文字可读。
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QRectF, Qt, QTimer
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


_POPUP_SHOW_GENERATION = "_acrylic_popup_show_generation"
_POPUP_PENDING_PAINT = "_acrylic_popup_pending_paint"


def _apply_popup_blur(popup: QWidget) -> None:
    """按当前主题启用弹出窗口模糊；普通弹出窗口失败时尝试另一种系统材质接口。"""
    from src.ui import win_effects

    dark = theme.current_theme() == "dark"
    if not win_effects.apply_acrylic_blurbehind(popup, dark=dark) and popup.metaObject().className() != "QTipLabel":
        win_effects.apply_acrylic_backdrop(popup, dark=dark)


def _prepare_popup_effects(popup: QWidget, *, small: bool = False) -> None:
    """准备圆角表面；支持原生模糊的弹层按系统要求安排首次合成。"""
    from src.ui import win_effects

    win_effects.disable_open_close_transition(popup)
    win_effects.apply_rounded_corners(popup, small=small)
    generation = (popup.property(_POPUP_SHOW_GENERATION) or 0) + 1
    popup.setProperty(_POPUP_SHOW_GENERATION, generation)
    surface = getattr(popup.parentWidget(), "_combo_acrylic_surface", None)
    if (
        popup.metaObject().className() == "QComboBoxPrivateContainer"
        and surface is not None
        and surface.backdrop is not None
    ):
        # 采样磨砂与圆角由 Qt 同层合成，叠加原生模糊会再次污染透明四角。
        popup.setProperty(_POPUP_PENDING_PAINT, None)
        return
    # 先让 Qt 完成弹窗表面绘制，再调整 Windows 10 的原生合成策略。
    if win_effects.needs_popup_paint_before_blur():
        popup.setProperty(_POPUP_PENDING_PAINT, generation)
        popup.update()
    else:
        popup.setProperty(_POPUP_PENDING_PAINT, None)
        _apply_popup_blur(popup)


def _finish_popup_effects(popup: QWidget, window, generation: int) -> None:
    """仅为仍可见、句柄和显示代次未变的弹出窗口补设模糊效果。"""
    if (
        not shiboken6.isValid(popup)
        or not popup.isVisible()
        or popup.property(_POPUP_SHOW_GENERATION) != generation
        or not shiboken6.isValid(window)
        or popup.windowHandle() is not window
    ):
        return
    try:
        _apply_popup_blur(popup)
        # 修改原生合成不会标记 Qt 缓冲为脏，需重新上传完整弹窗及透明视口。
        popup.update()
    except Exception:
        pass


class AcrylicPopupFilter(QObject):
    """在 Polish/Show 阶段给弹出层施加半透明亚克力。"""

    def eventFilter(self, obj, event):
        # 仅独立弹窗需要原生效果，普通页面子控件不参与这些高频事件。
        """识别提示、菜单和组合框浮层，在样式与显示阶段配置透明表面和原生效果。"""
        if not isinstance(obj, QWidget) or not obj.isWindow():
            return False
        et = event.type()
        if et == QEvent.Type.Paint:
            generation = obj.property(_POPUP_PENDING_PAINT)
            if generation is not None:
                obj.setProperty(_POPUP_PENDING_PAINT, None)
                window = obj.windowHandle()
                # 等待绘制和缓冲刷新结束后再执行，提示对象销毁时由上下文取消回调。
                QTimer.singleShot(0, obj, lambda: _finish_popup_effects(obj, window, generation))
            return False
        if et not in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.StyleChange):
            return False
        try:
            if obj.metaObject().className() == "QTipLabel":
                _style_tooltip(obj)
                # 提示标签的样式事件早于文字和几何设置，不应在此时创建原生窗口。
                if et == QEvent.Type.Show:
                    _prepare_popup_effects(obj, small=True)
                return False
            if et == QEvent.Type.StyleChange:
                return False
            # 仅处理按需创建的独立菜单和下拉弹窗。长期存在的浮层自行准备透明属性，
            # 避免父窗口样式级联期间重建原生窗口造成重入。
            is_menu = isinstance(obj, QMenu)
            is_combo = not is_menu and obj.metaObject().className() == "QComboBoxPrivateContainer"
            if not (is_menu or is_combo):
                return False
            if is_menu:
                if et == QEvent.Type.Polish:

                    def prepare_owner(menu=obj):
                        """显示前重新检查原始所有者是否已转为原生控件。"""
                        _prepare_menu_window_owner(menu)

                    obj.aboutToShow.connect(prepare_owner)
                _prepare_menu_window_owner(obj)
            is_cat_menu = is_menu and obj.objectName() == "catPickerMenu"
            # 先准备透明属性，再对最终句柄应用系统效果，避免句柄重建使效果失效。
            if is_combo:
                _style_combo_popup(obj)
            elif is_menu and not is_cat_menu:
                _style_menu(obj)

            # 原生圆角裁剪覆盖窗口内容与磨砂表面，不依赖背景开关。
            # 父控件加入最终布局后才创建原生窗口，避免样式准备阶段传播原生属性并改变坐标。
            # Windows 10 的亚克力效果在首次绘制后启用。
            if et == QEvent.Type.Show:
                _prepare_popup_effects(obj)
        except Exception:
            pass
        return False


_filter_singleton: AcrylicPopupFilter | None = None


def install(app) -> None:
    """在 QApplication 上安装弹窗亚克力过滤器（幂等）。"""
    from src.ui.application_event_filters import install_application_event_filter

    global _filter_singleton
    # Qt 的滚动和淡入快照窗口不透明，无法保留真实弹窗的原生材质。
    for effect in (
        Qt.UIEffect.UI_AnimateCombo,
        Qt.UIEffect.UI_AnimateMenu,
        Qt.UIEffect.UI_FadeMenu,
        Qt.UIEffect.UI_AnimateTooltip,
        Qt.UIEffect.UI_FadeTooltip,
    ):
        app.setEffectEnabled(effect, False)
    if _filter_singleton is None or (not shiboken6.isValid(_filter_singleton)):
        _filter_singleton = AcrylicPopupFilter()
        install_application_event_filter(
            app, _filter_singleton, (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.StyleChange, QEvent.Type.Paint)
        )


# 顶层对话框（QDialog）弱亚克力

# 弹出层（QMenu / 下拉框）由上面的过滤器统一处理；普通模态对话框（新建类别 / Mod
# 编辑 / 各类进度框…）是独立顶层窗口，机制不同，单独提供 :func:`apply_dialog_acrylic`。

# Qt 分层窗口通过 WA_TranslucentBackground 提供透明客户区。
# 窗口绘制半透明圆角底和描边，使用 Source 模式避免背景传播导致重复叠色。
# 原生窗口建立后应用 DWM 圆角及 ACCENT 模糊；系统不支持时保留可读的基础底色。
# 文本框使用较厚底色，图像和颜色样本保留原色。


_DIALOG_MARK = "_dialog_acrylic"


def _apply_window_acrylic_effects(dialog: QWidget, *, small: bool) -> None:
    """为已创建的对话框句柄应用系统圆角、模糊及过渡设置。"""
    try:
        from src.ui import win_effects

        # 禁用系统开窗快照过渡，避免首帧缺少尚未合成的模糊背景。
        win_effects.disable_open_close_transition(dialog)
        # 圆角对齐 paintEvent 的圆角底；磨砂层透出半透明底后的窗口内容（高斯模糊）。
        win_effects.apply_rounded_corners(dialog, small=small)
        win_effects.apply_acrylic_blurbehind(dialog, dark=(theme.current_theme() == "dark"))
    except Exception:
        pass


class _DialogAcrylicSurface(QObject):
    """在对话框自身绘制背景，让透明子控件共享同一表面。

    同级背景控件不能参与 Qt 的父背景传播，会造成文字和缩放边缘底色不一致。
    """

    def __init__(self, dialog: QWidget, radius_px: int, tint_alpha: float, animate: bool = True) -> None:
        """保存对话框圆角、叠色和入场动画配置，并订阅表面与窗口生命周期事件。"""
        super().__init__(dialog)
        self._radius = radius_px
        self._alpha = tint_alpha
        self._effects_applied = False
        self._effect_theme = theme.current_theme()
        self._animate = animate
        dialog.installEventFilter(self)

    def eventFilter(self, obj, event):
        """随绘制、主题、显示和句柄变化更新对话框材质，并维护动画状态。"""
        et = event.type()
        if et == QEvent.Type.Paint:
            self._paint_surface(obj)
        elif et == QEvent.Type.WinIdChange:
            self._effects_applied = False
        elif et in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            current = theme.current_theme()
            if self._effect_theme != current:
                self._effect_theme = current
                self._effects_applied = False
                if obj.isVisible():
                    self._apply_window_effects(obj)
                    self._effects_applied = True
                obj.update()
        elif et == QEvent.Type.Show:
            # 复位关窗动画标志：支持同一对话框实例被多次 exec（重开后仍可关）。
            obj.setProperty("_acrylic_closing", False)
            obj.setProperty("_acrylic_done", False)
            if not self._effects_applied:
                self._effects_applied = True
                self._apply_window_effects(obj)
            if self._animate and not event.spontaneous():
                self._play_entrance(obj)
        elif et == QEvent.Type.Hide:
            if _stop_dialog_slide(obj):
                resting = getattr(obj, "_dialog_resting_geometry", None)
                if resting is not None:
                    obj.setGeometry(resting)
        return False

    def _apply_window_effects(self, dialog: QWidget) -> None:
        """根据圆角尺寸应用对话框原生模糊效果。"""
        _apply_window_acrylic_effects(dialog, small=(self._radius <= 6))

    def _play_entrance(self, dialog: QWidget) -> None:
        """通过几何位移显示对话框，保留原生模糊背景。

        窗口透明度动画会启用统一透明度分层，影响 ACCENT 合成。
        位移动画在首帧前设置起始几何，避免窗口先显示在终点再发生跳变。
        """
        try:
            from src.ui import anim

            dialog._dialog_resting_geometry = dialog.geometry()
            anim.slide_in(dialog, dy=16, duration=150)
        except Exception:
            pass

    def _paint_surface(self, dialog):
        """以源覆盖模式绘制圆角叠色和细边框，保留对话框外侧透明区域。"""
        try:
            p = QPainter(dialog)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 重叠透明子控件可能请求多次父背景绘制，直接替换像素以避免底色累积。
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            rect = QRectF(dialog.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, float(self._radius), float(self._radius))
            # 每次重绘读当前主题色，自然跟随明暗切换。
            tint = QColor(theme.color("bg.layer"))
            tint.setAlphaF(acrylic_surface_alpha(self._alpha))
            p.fillPath(path, tint)
            pen = QPen(QColor(theme.color("stroke.card")))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawPath(path)
            p.end()
        except Exception:
            pass


def apply_dialog_acrylic(
    dialog: QWidget, *, small: bool = False, tint_alpha: float = 0.5, animate: bool = True
) -> None:
    """在首次显示前为顶层对话框准备半透明背景及系统模糊。

    重复调用复用已有设置。small 控制圆角尺寸，tint_alpha 控制背景不透明度。
    """
    if dialog.property(_DIALOG_MARK):
        return
    dialog.setProperty(_DIALOG_MARK, True)
    try:
        # 透明属性须在原生窗口创建前设置。
        dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    except Exception:
        return
    # 禁用 windowOpacity 入场淡入：它会启用统一 alpha 分层（LWA_ALPHA），
    # 使 DWM 的 ACCENT 模糊背景在淡入期间无法合成。
    _disable_window_opacity_fade(dialog)
    # 构造结束时预留标题栏，再创建原生窗口，避免可见分层窗口重新调整尺寸。
    from PySide6.QtWidgets import QDialog

    if isinstance(dialog, QDialog) and not dialog.property("customDialogFrame"):
        from src.ui.dialog_chrome import decorate_dialog

        decorate_dialog(dialog)
    radius_px = theme.radius("control") if small else theme.radius("card")
    # 绘制控制器 parent 到对话框，随窗口销毁，不增加遮挡内容的子控件。
    surface = _DialogAcrylicSurface(dialog, radius_px, tint_alpha, animate)
    dialog.update()
    # 显示前创建原生窗口并施加 ACCENT 效果，使 DWM 能在首帧合成磨砂。
    # 仅在窗口句柄或主题变化时重新配置，避免首显时重复处理同一句柄。
    try:
        # 创建原生窗口前完成无边框属性，之后修改会销毁已准备的表面。
        dialog.ensurePolished()
        dialog.winId()
        _apply_window_acrylic_effects(dialog, small=small)
        surface._effects_applied = True
    except Exception:
        pass
    # 关闭前播放位移动画，结束后完成对话框结果提交。
    if animate:
        _install_close_animation(dialog)


def _stop_dialog_slide(dialog: QWidget) -> bool:
    """停止仍在运行的对话框位移，返回是否取消了本轮动画。"""
    animation = getattr(dialog, "_slide_anim", None)
    if animation is None or not shiboken6.isValid(animation):
        return False
    running = animation.state() != QAbstractAnimation.State.Stopped
    animation.stop()
    dialog._slide_anim = None
    return running


def _install_close_animation(dialog: QWidget) -> None:
    """统一处理对话框关闭路径，位移动画结束后调用原始 done。

    确认、取消、Esc 和关闭按钮均通过 done 返回模态结果。
    仅延迟实际关闭，不改变返回值；重复调用不会重复提交，重新显示时重置关闭状态。
    """
    if getattr(dialog, "_acrylic_close_wrapped", False):
        return
    dialog._acrylic_close_wrapped = True  # type: ignore[attr-defined]
    orig_done = dialog.done

    def _finish(r: int) -> None:
        # 实际关闭只执行一次，避免动画完成和重复关闭同时触发。
        """只提交一次对话框结果，并恢复静止几何；对话框重新显示时重置关闭标记。"""
        if dialog.property("_acrylic_done"):
            return
        dialog.setProperty("_acrylic_done", True)
        _stop_dialog_slide(dialog)
        resting = getattr(dialog, "_dialog_resting_geometry", None)
        if resting is not None:
            # 隐藏后修改 QWidget 几何可能被尚未投递的原生移动事件覆盖。
            # 先恢复窗口位置，再在同一次回调中隐藏，避免退出位移成为下次打开基准。
            dialog.setGeometry(resting)
        orig_done(r)
        if shiboken6.isValid(dialog):
            if dialog.isVisible():
                # 工作或确认进行中时，对话框可以拒绝关闭。
                dialog.setProperty("_acrylic_closing", False)
                dialog.setProperty("_acrylic_done", False)

    def wrapper(r: int) -> None:
        # 动画期间再次请求关闭时立即结束，不再安排新动画。
        """启动退出滑动动画后提交结果，重复关闭或动画异常时直接完成关闭。"""
        if dialog.property("_acrylic_closing"):
            _finish(r)
            return
        dialog.setProperty("_acrylic_closing", True)
        try:
            from src.ui import anim

            if not _stop_dialog_slide(dialog):
                # 入场结束后的移动、缩放属于用户选择，关闭时以最新几何为基准。
                dialog._dialog_resting_geometry = dialog.geometry()
            # 关闭时使用加速上滑，使窗口在位移结束后消失。
            # slide_out 在动画完成或直接结束时均调用一次收尾回调。
            anim.slide_out(dialog, dy=40, duration=110, on_finished=lambda: _finish(r))
        except Exception:
            _finish(r)

    dialog.done = wrapper  # type: ignore[assignment]


def _disable_window_opacity_fade(dialog: QWidget) -> None:
    """移除一次性窗口淡入过滤器，并恢复完全不透明。

    窗口透明度动画会影响亚克力合成；没有过滤器时无需处理。
    """
    filt = getattr(dialog, "_intro_fade_filter", None)
    if filt is not None:
        try:
            dialog.removeEventFilter(filt)
        except Exception:
            pass
        try:
            del dialog._intro_fade_filter  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        dialog.setWindowOpacity(1.0)
    except Exception:
        pass
