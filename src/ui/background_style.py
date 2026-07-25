"""应用条件颜色和背景表面，不替换全局样式。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from functools import lru_cache
from time import perf_counter

import shiboken6
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from src.ui.application_event_filters import install_application_event_filter, remove_application_event_filter


class BackgroundStyle(QObject):
    """刷新控件外观选择器，保留全局样式表。"""

    BUDGET_SECONDS = 0.006
    BATCH_INTERVAL_MS = 8

    def __init__(self, app: QApplication) -> None:
        """保存应用外观状态，创建分批刷新定时器，并订阅控件显示和样式准备事件。"""
        super().__init__(app)
        self._app = app
        self._enabled = False
        self._theme_name: str | None = None
        self._disposed = False
        self._generation = 0
        self._polishing: set[int] = set()
        self._widgets: deque[QWidget] = deque()
        self._on_finished: Callable[[], None] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply_batch)
        install_application_event_filter(app, self, (QEvent.Type.Polish, QEvent.Type.Show))

    @property
    def enabled(self) -> bool:
        """返回当前目标，包括正在应用中的目标。"""
        return self._enabled

    def apply(
        self, enabled: bool, on_finished: Callable[[], None] | None = None, *, theme_name: str | None = None
    ) -> bool:
        """同步应用外观，提供回调时分批执行。

        每次调用替换未完成的工作和回调；省略主题时保留当前颜色目标，返回值仅表示目标是否变化。
        """
        if self._disposed:
            return False
        enabled = bool(enabled)
        changed = enabled != self._enabled or (theme_name is not None and theme_name != self._theme_name)
        self._enabled = enabled
        if theme_name is not None:
            self._theme_name = theme_name
        self._generation += 1
        generation = self._generation
        self._timer.stop()
        widgets = self._app.allWidgets()
        if on_finished is not None:
            widgets.sort(key=lambda widget: (not shiboken6.isValid(widget)) or not widget.isVisible())
        self._widgets = deque(widgets)
        self._on_finished = on_finished
        if on_finished is not None:
            self._timer.start(0)
        else:
            while self._widgets and generation == self._generation and not self._disposed:
                self._refresh_widget(self._widgets.popleft())
        return changed

    def dispose(self) -> None:
        """停止待处理工作并移除继承，不保留回调。"""
        if self._disposed or (not shiboken6.isValid(self)):
            return
        self._disposed = True
        self._generation += 1
        self._timer.stop()
        self._widgets.clear()
        self._on_finished = None
        remove_application_event_filter(self._app, self)
        if getattr(self._app, "_background_style", None) is self:
            self._app._background_style = None

    def eventFilter(self, obj, event):
        """在控件首次准备样式或显示时同步背景属性，并阻止向已销毁对象继续投递。"""
        if self._disposed or not isinstance(obj, QWidget):
            return False
        kind = event.type()
        if kind in (QEvent.Type.Polish, QEvent.Type.Show):
            # 首次样式准备会自行解析选择器，不对新控件重复触发完整样式更新。
            self._refresh_widget(obj, repolish=kind != QEvent.Type.Polish)
            return not shiboken6.isValid(obj)
        return False

    def _refresh_widget(self, widget: QWidget, *, repolish: bool = True) -> None:
        """将背景和主题属性同步到控件，并按需重新应用样式。

        重入时跳过同一控件；每次样式回调后检查 Qt 对象生命周期，避免访问已销毁控件。"""
        identity = id(widget)
        if identity in self._polishing:
            return
        self._polishing.add(identity)
        try:
            # 未设置的属性等同于 False，样式事件结束后重新核对是否出现更新的目标。
            while (
                not self._disposed
                and shiboken6.isValid(widget)
                and (
                    bool(widget.property("_backgroundSurface")) != self._enabled
                    or (self._theme_name is not None and widget.property("_appearanceTheme") != self._theme_name)
                )
            ):
                if bool(widget.property("_backgroundSurface")) != self._enabled:
                    widget.setProperty("_backgroundSurface", self._enabled)
                if not shiboken6.isValid(widget):
                    return
                if self._theme_name is not None:
                    widget.setProperty("_appearanceTheme", self._theme_name)
                if not shiboken6.isValid(widget):
                    return
                if not repolish:
                    continue
                style = widget.style()
                style.unpolish(widget)
                if not shiboken6.isValid(widget):
                    return
                style.polish(widget)
                if not shiboken6.isValid(widget):
                    return
                # 字体继续由原有继承机制更新，本控制器不复制字体属性。
                QApplication.sendEvent(widget, QEvent(QEvent.Type.StyleChange))
                if shiboken6.isValid(widget):
                    widget.update()
        finally:
            self._polishing.discard(identity)

    def _apply_batch(self) -> None:
        """在时间预算内刷新一批控件；代次变化时终止旧任务，队列清空后执行完成回调。"""
        if self._disposed:
            return
        generation = self._generation
        deadline = perf_counter() + self.BUDGET_SECONDS
        while self._widgets:
            self._refresh_widget(self._widgets.popleft())
            if self._disposed or generation != self._generation:
                return
            if self._widgets and perf_counter() >= deadline:
                self._timer.start(self.BATCH_INTERVAL_MS)
                return
        callback, self._on_finished = self._on_finished, None
        if callback is not None:
            callback()


@lru_cache(maxsize=2)
def _with_theme_selectors(light_qss: str, dark_qss: str) -> str:
    """为明暗两套 QSS 添加主题属性选择器，使其可共存于同一应用样式表。"""
    from src.ui.theme import _with_selector_attribute

    return "\n".join(
        _with_selector_attribute(qss, f'[_appearanceTheme="{name}"]')
        for name, qss in (("light", light_qss), ("dark", dark_qss))
    )


def appearance_stylesheet() -> str:
    """同时安装明暗规则并保持相对优先级。

    按源内容缓存选择器转换，强调色或资源变化时仍使样式失效。
    """
    from src.ui.theme import appearance_qss

    return _with_theme_selectors(appearance_qss("light"), appearance_qss("dark"))


def install_background_style(app: QApplication) -> BackgroundStyle:
    """返回当前 QApplication 拥有的共享控制器。"""
    controller = getattr(app, "_background_style", None)
    if controller is None or (not shiboken6.isValid(controller)):
        controller = BackgroundStyle(app)
        app._background_style = controller
    return controller
