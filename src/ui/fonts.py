"""选择已安装字体，并提供统一字体族来源。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import perf_counter

import shiboken6
from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from src.ui.application_event_filters import install_application_event_filter
from src.utils.qt_timers import call_later


def installed_families() -> list[str]:
    """列出可选字体族，排除 Windows 竖排别名。"""
    return sorted((family for family in QFontDatabase.families() if not family.startswith("@")), key=str.casefold)


def resolve_font(family: str = "") -> QFont:
    """偏好为空或字体不可用时使用系统界面字体。"""
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if family and family in installed_families():
        # 所选字体缺少字符时保留系统字体回退。
        font.setFamilies(list(dict.fromkeys((family, font.family()))))
    return font


def qss_family() -> str:
    """样式使用应用当前字体族，主题切换后同样更新。"""
    if QGuiApplication.instance() is None:
        return "sans-serif"
    families = QGuiApplication.font().families()
    quoted = ['"' + family.replace("\\", "\\\\").replace('"', '\\"') + '"' for family in families]
    return ", ".join([*quoted, "sans-serif"])


class FontCatalog(QObject):
    """分批准备字体元数据，不提前解析字形。

    避免首次自定义字体解析阻塞界面；预览等待准备完成，尺寸提示不解析自定义字体。
    """

    ready = Signal()
    BUDGET_SECONDS = 0.004

    def __init__(self, app: QApplication) -> None:
        """订阅系统字体库变化，并以单次定时器分批预热字体样式目录。"""
        super().__init__(app)
        self.is_ready = False
        self._waiting: list[Callable[[], None]] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._prepare_batch)
        app.fontDatabaseChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        """重新读取已安装字体族并安排异步预热，暂时撤销就绪状态。"""
        self.is_ready = False
        self._pending = deque(installed_families())
        self._timer.start(0)

    def when_ready(self, callback: Callable[[], None]) -> None:
        """字体目录就绪后执行回调；已就绪时也通过事件循环异步调用。"""
        if self.is_ready:
            call_later(0, self, callback)
        else:
            self._waiting.append(callback)

    def _prepare_batch(self) -> None:
        """在时间预算内查询字体样式，全部完成后发出 ready 并执行等待回调。"""
        deadline = perf_counter() + self.BUDGET_SECONDS
        while self._pending:
            QFontDatabase.styles(self._pending.popleft())
            if perf_counter() >= deadline:
                self._timer.start(8)
                return
        self.is_ready = True
        self.ready.emit()
        waiting, self._waiting = self._waiting, []
        for callback in waiting:
            callback()


def font_catalog() -> FontCatalog:
    """获取应用持有的唯一字体目录，首次调用时创建并启动预热。"""
    app = QApplication.instance()
    catalog = getattr(app, "_font_catalog", None)
    if catalog is None:
        catalog = FontCatalog(app)
        app._font_catalog = catalog
    return catalog


def _set_widget_family(widget: QWidget, families: list[str]) -> None:
    """仅修改有效控件的字体族，保留字号、字重和其他显式字体属性。"""
    if not shiboken6.isValid(widget):
        return
    font = widget.font()
    if font.families() != families:
        # 保留字号、字重、斜体和间距等显式设置，仅更新字体族。
        font.setFamilies(families)
        widget.setFont(font)


class _FontInheritance(QObject):
    """样式变化后维持已选字体族。

    Qt 可能为弹窗使用系统类字体，因此仅在这些生命周期节点纠正字体族。
    """

    def __init__(self, app: QApplication) -> None:
        """保存应用父对象，并建立防止字体更新重入的标记。"""
        super().__init__(app)
        self._updating = False

    def eventFilter(self, obj, event):
        """在控件显示或样式变化时同步应用字体族，重入事件不重复更新。"""
        if (
            not self._updating
            and isinstance(obj, QWidget)
            and event.type() in (QEvent.Type.Polish, QEvent.Type.StyleChange, QEvent.Type.Show)
        ):
            self._updating = True
            try:
                _set_widget_family(obj, QApplication.font().families())
            finally:
                self._updating = False
        return False


def _install_inheritance(app: QApplication) -> None:
    """为应用安装唯一的字体继承过滤器，使后来创建的控件使用当前字体族。"""
    current = getattr(app, "_font_inheritance", None)
    if current is None or (not shiboken6.isValid(current)):
        app._font_inheritance = _FontInheritance(app)
        install_application_event_filter(
            app, app._font_inheritance, (QEvent.Type.Polish, QEvent.Type.StyleChange, QEvent.Type.Show)
        )


def apply_font(app: QApplication, font: QFont) -> None:
    """启动时同步设置字体，不刷新主题、图标或背景。"""
    _install_inheritance(app)
    live = getattr(app, "_live_font_application", None)
    if live is not None:
        live.cancel()
    app.setFont(font)
    families = font.families()
    for widget in app.allWidgets():
        _set_widget_family(widget, families)


class _FontApplication(QObject):
    """等待字体目录就绪后分批更新所有控件，并以代次隔离过期请求。"""

    BUDGET_SECONDS = 0.006

    def __init__(self, app: QApplication) -> None:
        """创建应用级字体更新队列、代次和单次调度定时器。"""
        super().__init__(app)
        self._app = app
        self._generation = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply_batch)
        self._widgets: deque[QWidget] = deque()
        self._on_finished = None

    def cancel(self) -> None:
        """使等待中的回调失效，停止分批更新并清空控件队列。"""
        self._generation += 1
        self._timer.stop()
        self._widgets.clear()
        self._on_finished = None

    def start(self, font: QFont, on_finished: Callable[[], None]) -> None:
        """复制目标字体并替换旧请求，字体目录就绪后开始应用。"""
        self.cancel()
        generation = self._generation
        self._font = QFont(font)
        self._on_finished = on_finished
        font_catalog().when_ready(lambda: self._begin(generation))

    def _begin(self, generation: int) -> None:
        """核验请求代次后更新应用字体，并优先将可见控件加入刷新队列。"""
        if (not shiboken6.isValid(self)) or generation != self._generation:
            return
        _install_inheritance(self._app)
        self._app.setFont(self._font)
        self._families = self._font.families()
        # 优先刷新可见控件，分批覆盖对话框、菜单和隐藏页面。
        self._widgets = deque(sorted(self._app.allWidgets(), key=lambda w: not w.isVisible()))
        self._timer.start(0)

    def _apply_batch(self) -> None:
        """在时间预算内逐个更新字体族，队列结束后等待布局事件再报告完成。"""
        deadline = perf_counter() + self.BUDGET_SECONDS
        while self._widgets:
            _set_widget_family(self._widgets.popleft(), self._families)
            if perf_counter() >= deadline:
                self._timer.start(8)
                return
        callback, self._on_finished = self._on_finished, None
        if callback is not None:
            # 字体和布局事件处理完成后再显示完成状态。
            generation = self._generation
            call_later(0, self, lambda: self._finish(generation, callback))

    def _finish(self, generation: int, callback: Callable[[], None]) -> None:
        """仅在对象仍有效且请求代次未变时执行完成回调。"""
        if shiboken6.isValid(self) and generation == self._generation:
            callback()


def apply_font_live(app: QApplication, font: QFont, on_finished: Callable[[], None]) -> None:
    """复用应用级字体更新器，异步应用目标字体并在完成后通知调用者。"""
    live = getattr(app, "_live_font_application", None)
    if live is None:
        live = _FontApplication(app)
        app._live_font_application = live
    live.start(font, on_finished)
