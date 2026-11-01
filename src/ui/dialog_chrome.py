"""应用统一的对话框边框。

保留原对话框布局和关闭处理，标题栏空间独立于 Qt 内部布局。
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.utils.constants import APP_DISPLAY_NAME
from src.utils.i18n import t as tr
from src.utils.paths import get_resources_dir


def _color(name: str) -> str:
    """读取对话框共享色板中的指定语义颜色。"""
    return theme.color(f"mod_updates.{name}")


def center_dialog(dialog: QDialog) -> None:
    """相对所属应用窗口居中，并保持边框位于屏幕内。"""
    parent = dialog.parentWidget()
    owner = parent.window() if parent is not None else None
    screen = owner.screen() if owner is not None else dialog.screen()
    if screen is None:
        return
    available = screen.availableGeometry()
    frame = dialog.frameGeometry()
    frame.moveCenter(owner.frameGeometry().center() if owner is not None else available.center())
    frame.moveLeft(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)))
    frame.moveTop(max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))
    dialog.move(frame.topLeft())


class _CloseButton(QPushButton):
    """随悬停与主题状态切换图标颜色的标题栏关闭按钮。"""

    def refresh_icon(self, hovered=False):
        """按悬停状态设置白色或主题次要文字色的关闭图标。"""
        self.setIcon(
            QIcon(icons.colored_pixmap_from("mod-updates/close", 30, "#ffffff" if hovered else _color("secondary")))
        )

    def showEvent(self, event):
        # 点击关闭可能不触发离开事件，复用标题栏前清理悬停状态。
        """显示时刷新关闭图标，使重新打开的对话框跟随当前主题。"""
        self.refresh_icon()
        super().showEvent(event)

    def enterEvent(self, event):
        """鼠标进入时将关闭图标切为高对比颜色。"""
        self.refresh_icon(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时恢复关闭图标的主题颜色。"""
        self.refresh_icon()
        super().leaveEvent(event)


class DialogTitleBar(QWidget):
    """与对话框材质一致的紧凑标题栏。"""

    def __init__(self, window: QDialog):
        """创建图标、标题和关闭按钮，并绑定所属窗口标题、图标及关闭动作。"""
        super().__init__(window)
        self.setObjectName("dialogTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(31)
        self._drag_origin = None
        self._window_origin = QPoint()
        self._coalesced_move = False
        self._pending_move = None
        self._pending_native_move = False
        self._native_move = None
        self._move_timer = QTimer(self)
        self._move_timer.setSingleShot(True)
        self._move_timer.timeout.connect(self._apply_pending_move)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(7)
        self.icon_label = QLabel()
        self.icon_label.setObjectName("dialogWindowIcon")
        self.icon_label.setFixedSize(16, 16)
        self.title_label = QLabel()
        self.title_label.setObjectName("dialogWindowTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for label in (self.icon_label, self.title_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label, 1 if label is self.title_label else 0)
        self.close_button = _CloseButton()
        self.close_button.setObjectName("dialogClose")
        self.close_button.setAutoDefault(False)
        self.close_button.setFixedSize(42, 30)
        self.close_button.setIconSize(QSize(15, 15))
        self.close_button.setToolTip(tr("关闭"))
        # 调用关闭以保留任务窗口拒绝关闭的能力，并让消息框处理自己的取消按钮。
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.close_button)
        window.windowTitleChanged.connect(self._set_title)
        window.windowIconChanged.connect(self._set_icon)
        self._set_title(window.windowTitle())
        icon = window.windowIcon()
        if icon.isNull():
            icon = QIcon(str(get_resources_dir() / "app.ico"))
        window.setWindowIcon(icon)
        self._set_icon(icon)
        self.retheme()

    def _set_title(self, title):
        """同步窗口标题与悬浮提示，空标题回退应用名并更新关闭按钮无障碍名称。"""
        self.title_label.setText(title or APP_DISPLAY_NAME)
        self.title_label.setToolTip(title)
        self.close_button.setAccessibleName(tr("关闭{value}", value=title or APP_DISPLAY_NAME))

    def _set_icon(self, icon):
        """按当前屏幕缩放将窗口图标绘制到标题栏的固定逻辑尺寸。"""
        self.icon_label.setPixmap(icon.pixmap(QSize(16, 16), self.devicePixelRatioF()))

    def retheme(self):
        """根据对话框材质重建标题栏样式，并刷新关闭图标。"""
        fill = "transparent" if self.window().property("_dialog_acrylic") else _color("surface")
        self.setStyleSheet(f"""
            QWidget#dialogTitleBar {{ background: {fill}; border: 0; }}
            QLabel {{ color: {_color("secondary")}; background: transparent;
                     font-size: 11px; border: 0; padding: 0; margin: 0; }}
            QPushButton {{ background: transparent; border: 0; border-radius: 0;
                          padding: 0; margin: 0; min-width: 42px; max-width: 42px;
                          min-height: 30px; max-height: 30px; }}
            QPushButton:hover {{ background: #c42b1c; }}
            QPushButton:focus {{ background: transparent; border: 0; outline: 0; }}
            QPushButton:hover:focus {{ background: #c42b1c; }}
            QPushButton:disabled {{ background: transparent; }}
        """)
        self.close_button.refresh_icon()

    def mousePressEvent(self, event):
        """优先进入 Win10 共用原生拖动路径，其他输入保留原有移动回退。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._end_drag()
            from src.ui.native_move import begin_native_move

            if begin_native_move(self.window()):
                event.accept()
                return
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = self.window().pos()
            from src.ui import win_effects

            self._coalesced_move = (
                event.spontaneous()
                and QApplication.platformName() == "windows"
                and win_effects.needs_popup_paint_before_blur()
            )
            if self._coalesced_move:
                from src.ui.native_resize import begin_native_resize

                self._native_move = begin_native_resize(self.window())
            else:
                # 系统移动可能接受请求却未接管，继续送达标题栏的事件仍须移动窗口。
                handle = self.window().windowHandle()
                if handle is not None:
                    handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """合并 Win10 系统指针事件，避免窗口依次追赶已经过期的位置。"""
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._coalesced_move:
                self._pending_move = event.globalPosition().toPoint()
                self._pending_native_move = event.spontaneous()
                if not self._move_timer.isActive():
                    self._move_timer.start(0)
            else:
                self.window().move(self._window_origin + event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
        else:
            self._end_drag()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """提交释放位置并取消排队更新，避免松开后仍继续移动。"""
        if event.button() == Qt.MouseButton.LeftButton and self._coalesced_move:
            self._move_timer.stop()
            self._pending_move = event.globalPosition().toPoint()
            self._pending_native_move = False
            self._apply_pending_move(finish=True)
        self._end_drag()
        super().mouseReleaseEvent(event)

    def _apply_pending_move(self, *, finish: bool = False) -> None:
        """以最新光标计算目标位置，复用原生几何基准并保留窗口尺寸与层级。"""
        position = self._pending_move
        if position is not None and self._pending_native_move:
            position = QCursor.pos()
        self._pending_move = None
        self._pending_native_move = False
        if self._drag_origin is None or position is None:
            return
        window = self.window()
        target = self._window_origin + position - self._drag_origin
        if not finish and self._native_move is not None:
            geometry = QRect(window.geometry())
            geometry.moveTopLeft(target)
            if self._native_move.apply(window, geometry):
                return
            from src.ui.native_resize import begin_native_resize

            window.move(target)
            self._native_move = begin_native_resize(window)
            return
        window.move(target)

    def hideEvent(self, event):
        """标题栏隐藏时终止拖动，防止位置基准残留。"""
        self._end_drag()
        super().hideEvent(event)

    def _end_drag(self):
        """清除拖动基准，停止手动更新窗口位置。"""
        # Qt 析构父窗口时仍会投递隐藏事件，循环回收可能已清空 Python 状态。
        timer = getattr(self, "_move_timer", None)
        if timer is None or not shiboken6.isValid(timer):
            return
        timer.stop()
        self._pending_move = None
        self._pending_native_move = False
        self._native_move = None
        self._coalesced_move = False
        self._drag_origin = None

    def changeEvent(self, event):
        """主题确实改变时刷新标题栏，避免重复响应样式传播事件。"""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "close_button"):
            current = theme.current_theme()
            if getattr(self, "_theme", None) != current:
                self._theme = current
                self.retheme()


class ResizeHandle(QWidget):
    """在 Windows 合并 Qt 缩放请求，其他可响应的平台使用原生缩放。"""

    def __init__(self, parent: QWidget, edges: Qt.Edge, cursor: Qt.CursorShape):
        """配置边缘方向、光标及合并拖动事件的定时器，右下角额外显示尺寸提示图标。"""
        super().__init__(parent)
        self.edges = edges
        self._drag_origin = None
        self._initial_geometry = QRect()
        self._pending_position = None
        self._pending_native_position = False
        self._native_resize = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._apply_pending_resize)
        self.setCursor(cursor)
        self.setAccessibleName(tr("调整窗口大小"))
        # 命中区域不绘制继承的控件背景。
        self.setStyleSheet("background: transparent; border: none;")
        if edges == Qt.Edge.RightEdge | Qt.Edge.BottomEdge:
            indicator = QLabel(self)
            indicator.setFixedSize(12, 12)
            indicator.setPixmap(icons.colored_pixmap_from("mod-updates/resize", 12, _color("muted")))
            indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setToolTip(tr("拖动调整窗口大小"))

    def mousePressEvent(self, event):
        """仅对可调整的普通窗口启动左键缩放，记录几何基准并按平台选择原生或手动路径。"""
        window = self.window()
        if event.button() != Qt.MouseButton.LeftButton or window.isMaximized() or window.isFullScreen():
            super().mousePressEvent(event)
            return
        self._cancel_resize()
        handle = window.windowHandle()
        # Windows 模态缩放会等待同步绘制，改由 Qt 合并拖动位置，避免慢帧阻塞输入或重放过期尺寸。
        if QApplication.platformName() == "windows" or handle is None or not handle.startSystemResize(self.edges):
            self._drag_origin = event.globalPosition().toPoint()
            self._initial_geometry = window.geometry()
            if QApplication.platformName() == "windows":
                from src.ui.native_resize import begin_native_resize

                self._native_resize = begin_native_resize(window)
        event.accept()

    def mouseMoveEvent(self, event):
        """合并连续鼠标移动，只安排一次待处理尺寸更新；失去拖动条件时取消。"""
        if self._drag_origin is None or not event.buttons() & Qt.MouseButton.LeftButton:
            self._cancel_resize()
            super().mouseMoveEvent(event)
            return
        self._pending_position = event.globalPosition().toPoint()
        self._pending_native_position = event.spontaneous() and QApplication.platformName() == "windows"
        # 指针事件批次结束后处理最新位置，不反复重启活动定时器。
        if not self._resize_timer.isActive():
            self._resize_timer.start(0)
        event.accept()

    def _apply_pending_resize(self, *, finish=False):
        """依据拖动边缘计算受最小和最大尺寸约束的目标几何，优先原生更新并在基准失效时重建。"""
        position = self._pending_position
        if position is not None and self._pending_native_position:
            # 原生输入投递时可能已过期，布局前重新采样当前位置；程序发送的事件保留指定坐标。
            position = QCursor.pos()
        self._pending_position = None
        self._pending_native_position = False
        if self._drag_origin is None or position is None:
            return
        window = self.window()
        delta = position - self._drag_origin
        rect = QRect(self._initial_geometry)
        minimum, maximum = window.minimumSize(), window.maximumSize()
        if self.edges & Qt.Edge.LeftEdge:
            width = max(minimum.width(), min(maximum.width(), rect.width() - delta.x()))
            rect.setLeft(rect.right() - width + 1)
        elif self.edges & Qt.Edge.RightEdge:
            rect.setWidth(max(minimum.width(), min(maximum.width(), rect.width() + delta.x())))
        if self.edges & Qt.Edge.TopEdge:
            height = max(minimum.height(), min(maximum.height(), rect.height() - delta.y()))
            rect.setTop(rect.bottom() - height + 1)
        elif self.edges & Qt.Edge.BottomEdge:
            rect.setHeight(max(minimum.height(), min(maximum.height(), rect.height() + delta.y())))
        if not finish and self._native_resize is not None:
            if self._native_resize.apply(window, rect):
                return
            # Qt 处理显示器或缩放变化后重新设置拖动基准。
            from src.ui.native_resize import begin_native_resize

            window.setGeometry(rect)
            self._native_resize = begin_native_resize(window)
            return
        window.setGeometry(rect)

    def mouseReleaseEvent(self, event):
        """左键释放时提交最终 Qt 几何，随后停止尺寸更新调度。"""
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            # 释放事件可能先于绘制或最后移动事件，结束拖动前提交释放位置。
            self._resize_timer.stop()
            self._pending_position = event.globalPosition().toPoint()
            # 释放按钮后指针可能继续移动。
            self._pending_native_position = False
            self._apply_pending_resize(finish=True)
            self._cancel_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _cancel_resize(self):
        """丢弃待处理位置和原生基准，停止尺寸更新定时器。"""
        # 控件状态可能先于 Qt 父窗口释放，剩余子定时器由 Qt 的所有权关系销毁。
        timer = getattr(self, "_resize_timer", None)
        if timer is None or not shiboken6.isValid(timer):
            return
        timer.stop()
        self._pending_position = None
        self._pending_native_position = False
        self._drag_origin = None
        self._native_resize = None

    def hideEvent(self, event):
        """缩放手柄隐藏时取消当前拖动，避免后续定时器更新窗口。"""
        self._cancel_resize()
        super().hideEvent(event)


class _DialogFrame(QObject):
    """为普通对话框管理自定义标题栏、边缘缩放手柄及进度布局。"""

    def __init__(self, dialog):
        """安装无边框标题栏与八个缩放区域，调整内容边距并接管缺少布局的进度对话框。"""
        super().__init__(dialog)
        self.dialog = dialog
        self._theme = theme.current_theme()
        self._close_allowed = bool(dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint)
        if not dialog.windowFlags() & Qt.WindowType.FramelessWindowHint:
            dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.titlebar = DialogTitleBar(dialog)
        # 边框由对话框自身样式绘制，避免全窗口覆盖层变为原生同级窗口后阻挡点击。
        self._progress_layout = None
        self._progress_snapshot = None
        margins = dialog.contentsMargins()
        # 在现有布局外预留标题栏空间，避免消息框重建布局时覆盖。
        dialog.setContentsMargins(margins.left() + 1, margins.top() + 32, margins.right() + 1, margins.bottom() + 1)
        if dialog.minimumHeight() == dialog.maximumHeight():
            dialog.setFixedHeight(dialog.height() + 33)
        if isinstance(dialog, QProgressDialog) and dialog.layout() is None:
            self._progress_layout = QVBoxLayout(dialog)
            self._progress_layout.setContentsMargins(16, 12, 16, 12)
            self._progress_layout.setSpacing(12)
            self._layout_progress()
        self.handles = []
        for edges, cursor in (
            (Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            (Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            (Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            (Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
        ):
            self.handles.append(ResizeHandle(dialog, edges, cursor))
        dialog.installEventFilter(self)
        self.update_geometry()

    def _layout_progress(self):
        """修正进度对话框忽略外边距的内部几何。

        复用原控件保留取消和自动关闭语义，在 Qt 内部布局后核对替换的标签、进度条和按钮。
        """
        layout = self._progress_layout
        if layout is None:
            return
        children = self.dialog.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
        controls = [
            child for cls in (QLabel, QProgressBar, QPushButton) for child in children if isinstance(child, cls)
        ]
        for widget in controls:
            if isinstance(widget, QLabel) and widget.styleSheet() != "background: transparent;":
                widget.setStyleSheet("background: transparent;")

        def snapshot():
            """捕获进度对话框内容区域和子控件几何、可见性与尺寸建议，用于检测布局变化。"""
            return (
                self.dialog.contentsRect(),
                tuple((widget, widget.geometry(), widget.isHidden(), widget.sizeHint()) for widget in controls),
            )

        if snapshot() == self._progress_snapshot:
            return
        for index, widget in enumerate(controls):
            if layout.indexOf(widget) != index:
                layout.insertWidget(index, widget)
                if isinstance(widget, QPushButton):
                    layout.setAlignment(widget, Qt.AlignmentFlag.AlignRight)
        layout.invalidate()
        layout.setGeometry(self.dialog.contentsRect())
        self._progress_snapshot = snapshot()

    def update_geometry(self):
        """按窗口尺寸重排标题栏和缩放手柄，并依据固定尺寸、窗口状态和关闭权限控制交互。"""
        dialog = self.dialog
        width, height = dialog.width(), dialog.height()
        self.titlebar.setGeometry(1, 1, max(0, width - 2), 31)
        self.titlebar.show()
        self.titlebar.raise_()
        fixed_w = dialog.minimumWidth() == dialog.maximumWidth()
        fixed_h = dialog.minimumHeight() == dialog.maximumHeight()
        fixed = bool(dialog.windowFlags() & Qt.WindowType.MSWindowsFixedSizeDialogHint)
        for handle in self.handles:
            edges = handle.edges
            left, right = bool(edges & Qt.Edge.LeftEdge), bool(edges & Qt.Edge.RightEdge)
            top, bottom = bool(edges & Qt.Edge.TopEdge), bool(edges & Qt.Edge.BottomEdge)
            corner = (left or right) and (top or bottom)
            w = 12 if corner else 5 if left or right else width - 24
            h = 12 if corner else 5 if top or bottom else height - 24
            x = width - w if right else 0 if left else 12
            y = height - h if bottom else 0 if top else 12
            handle.setGeometry(x, y, max(0, w), max(0, h))
            handle.setVisible(
                not (
                    fixed
                    or dialog.isMaximized()
                    or dialog.isFullScreen()
                    or ((left or right) and fixed_w)
                    or ((top or bottom) and fixed_h)
                )
            )
            handle.raise_()
        self.titlebar.close_button.setEnabled(self._close_allowed)

    def eventFilter(self, obj, event):
        # 垃圾回收可能先清理 Python 状态，子对象拆卸仍会产生事件，不能仅检查原生对话框有效性。
        """随布局和窗口状态更新边框几何，主题或显示变化时刷新标题栏与原生圆角。"""
        dialog = getattr(self, "dialog", None)
        if dialog is None or not shiboken6.isValid(dialog):
            return False
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self._layout_progress()
        if kind in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest, QEvent.Type.WindowStateChange):
            self.update_geometry()
        if kind in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            current = theme.current_theme()
            if current != self._theme:
                self._theme = current
                self.titlebar.retheme()
                self.dialog.update()
        if kind == QEvent.Type.Show:
            from src.ui.win_effects import apply_dark_titlebar, apply_rounded_corners

            apply_dark_titlebar(self.dialog, theme.current_theme() == "dark")
            apply_rounded_corners(self.dialog)
        return False


class DialogFrameManager(QObject):
    """覆盖静态 Qt 对话框、插件对话框和应用对话框。"""

    def eventFilter(self, obj, event):
        """为普通顶层对话框配置透明材质、入场动画和统一边框，跳过原生文件及专用不透明对话框。"""
        if not isinstance(obj, QDialog) or isinstance(obj, QFileDialog):
            return False
        if obj.property("opaqueDialogSurface"):
            # 拥有不透明表面的对话框保留自身原生边框，避免样式准备时重建分层缓冲。
            return False
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
            if obj.isWindow():
                # 构造函数返回前可能收到样式事件，此时仅设置标志，显示时再添加子控件。
                if event.type() == QEvent.Type.Polish:
                    if not obj.windowFlags() & Qt.WindowType.FramelessWindowHint:
                        obj.setWindowFlag(Qt.WindowType.FramelessWindowHint)
                    if not obj.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
                        obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                else:
                    from src.ui.acrylic import apply_dialog_acrylic

                    # 私有控件未创建前也可能收到样式事件，进度取消保持立即生效。
                    fresh_surface = not obj.property("_dialog_acrylic")
                    apply_dialog_acrylic(obj, tint_alpha=0.78, animate=not isinstance(obj, QProgressDialog))
                    if (
                        fresh_surface
                        and not isinstance(obj, QProgressDialog)
                        and getattr(obj, "_slide_anim", None) is None
                    ):
                        # 显示期间新安装的表面过滤器尚未收到当前显示事件。
                        from src.ui import anim

                        obj._dialog_resting_geometry = obj.geometry()
                        anim.slide_in(obj, dy=16, duration=150)
                    if not obj.property("customDialogFrame") and not obj.property("dialogFrameInstalled"):
                        frame = decorate_dialog(obj)
                        frame.titlebar.retheme()
                    elif obj.property("customDialogFrame"):
                        for bar in obj.findChildren(DialogTitleBar):
                            bar.retheme()
        return False


def decorate_dialog(dialog: QDialog) -> _DialogFrame:
    """允许构造阶段需要关闭按钮的对话框显式安装边框。"""
    if not dialog.property("dialogFrameInstalled"):
        dialog.setProperty("dialogFrameInstalled", True)
        dialog._dialog_frame = _DialogFrame(dialog)
        # 边框在显示时安装，此时样式选择器已被缓存。
        dialog.style().unpolish(dialog)
        dialog.style().polish(dialog)
    return dialog._dialog_frame


def install(app: QApplication) -> DialogFrameManager:
    """安装应用级对话框装饰过滤器，并保留系统原生文件对话框支持。"""
    from src.ui.application_event_filters import install_application_event_filter

    # 文件和目录选择使用系统对话框，其他对话框使用统一边框。
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, False)
    manager = DialogFrameManager(app)
    install_application_event_filter(app, manager, (QEvent.Type.Polish, QEvent.Type.Show))
    return manager
