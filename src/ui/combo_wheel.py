"""统一禁止下拉框通过滚轮切换当前选项。"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QComboBox

from src.ui.application_event_filters import install_application_event_filter


class ComboWheelFilter(QObject):
    """阻止未展开的组合框因滚轮误改选项，同时保留父级滚动响应。"""

    def eventFilter(self, obj, event):
        """忽略组合框上的滚轮事件并停止其默认处理，其他事件继续传递。"""
        if isinstance(obj, QComboBox) and event.type() == QEvent.Type.Wheel:
            # 跳过 QComboBox 的切项逻辑，同时允许事件冒泡给页面滚动区。
            # 展开列表接收事件的是 view/viewport，不受此规则影响。
            event.ignore()
            return True
        return False


def install(app: QApplication) -> ComboWheelFilter:
    """覆盖已有和后续创建的下拉框（含插件），过滤器随 QApplication 存活。"""
    wheel_filter = getattr(app, "_combo_wheel_filter", None)
    if wheel_filter is None or (not shiboken6.isValid(wheel_filter)):
        wheel_filter = ComboWheelFilter(app)
        app._combo_wheel_filter = wheel_filter
    install_application_event_filter(app, wheel_filter, (QEvent.Type.Wheel,))
    return wheel_filter
