"""将声明式插件设置绑定到经过校验的配置门面。"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk.configuration import ConfigurationChange, ConfigurationFacade
from src.plugin_sdk.errors import PluginError
from src.ui import theme
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import t as tr


class PluginSettingsForm(QWidget):
    """保存、重置并实时同步插件设置，写入失败时保留原值和错误说明。"""

    def __init__(self, configuration: ConfigurationFacade, contributions, parent=None) -> None:
        """根据已本地化的声明构造设置行，并把配置订阅绑定到控件生命周期。"""
        super().__init__(parent)
        self._configuration = configuration
        self._controls: dict[str, QWidget] = {}
        self._definitions = {item.data["key"]: item.data for item in contributions}
        self.rows: list[QBoxLayout] = []
        self._labels: list[tuple[QLabel, str]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        for definition in self._definitions.values():
            self._add_setting(layout, definition)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        self._error.hide()
        layout.addWidget(self._error)
        self._labels.append((self._error, "danger"))
        # Qt 信号不会保活普通 Python 接收者，表单必须持有注销句柄直至销毁。
        self._subscription = configuration.on_changed(self._changed)
        self.destroyed.connect(self._subscription.dispose)
        self.retheme()

    def _add_setting(self, layout: QVBoxLayout, definition: dict) -> None:
        """创建单行说明、类型匹配的控件和恢复默认按钮。"""
        key, kind = definition["key"], definition["type"]
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        row.setSpacing(12)
        self.rows.append(row)
        words = QVBoxLayout()
        label = QLabel(definition.get("title", key))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._labels.append((label, "text.primary"))
        words.addWidget(label)
        if definition.get("description"):
            description = QLabel(definition["description"])
            description.setTextFormat(Qt.TextFormat.PlainText)
            description.setWordWrap(True)
            words.addWidget(description)
            self._labels.append((description, "text.secondary"))
        row.addLayout(words, 1)
        if "enum" in definition:
            control = QComboBox()
            titles = definition.get("enumTitles", [str(value) for value in definition["enum"]])
            for index, title in enumerate(titles):
                control.addItem(title, index)
            control.currentIndexChanged.connect(partial(self._save_control, key))
        elif kind == "bool":
            control = ToggleSwitch()
            control.toggled.connect(partial(self._save_control, key))
        elif (
            kind == "int"
            and "minimum" in definition
            and "maximum" in definition
            and -(2**31) <= definition["minimum"] <= definition["maximum"] < 2**31
        ):
            control = QSpinBox()
            control.setRange(definition.get("minimum", -(2**31)), definition.get("maximum", 2**31 - 1))
            control.valueChanged.connect(partial(self._save_control, key))
        else:
            control = QLineEdit()
            control.setMinimumWidth(130)
            control.setMaximumWidth(230)
            control.editingFinished.connect(partial(self._save_control, key))
        control.setAccessibleName(label.text())
        control.setProperty("pluginSetting", key)
        self._controls[key] = control
        self._sync(key)
        row.addWidget(control)
        reset = QPushButton(tr("恢复默认"))
        reset.setProperty("resetSetting", key)
        reset.clicked.connect(partial(self._reset, key))
        row.addWidget(reset)
        layout.addLayout(row)

    def _sync(self, key: str) -> None:
        """在阻断控件信号的情况下读取有效值，避免配置通知产生递归写入。"""
        value = self._configuration.get(key)
        control = self._controls[key]
        with QSignalBlocker(control):
            if isinstance(control, QComboBox):
                control.setCurrentIndex(self._definitions[key]["enum"].index(value))
            elif isinstance(control, ToggleSwitch):
                control.setChecked(value)
            elif isinstance(control, QSpinBox):
                control.setValue(value)
            else:
                control.setText(str(value))

    def _save_control(self, key: str, *_args) -> None:
        """提交控件当前值，校验或磁盘错误时显示说明并恢复最后有效值。"""
        control = self._controls[key]
        try:
            if isinstance(control, QComboBox):
                value = self._definitions[key]["enum"][control.currentIndex()]
            elif isinstance(control, ToggleSwitch):
                value = control.isChecked()
            elif isinstance(control, QSpinBox):
                value = control.value()
            else:
                kind = self._definitions[key]["type"]
                value = (
                    int(control.text())
                    if kind == "int"
                    else float(control.text())
                    if kind == "float"
                    else control.text()
                )
            self._configuration.set(key, value)
            self._error.hide()
        except (OSError, ValueError, RuntimeError, PluginError) as exc:
            self._show_error(key, exc)

    def _reset(self, key: str, *_args) -> None:
        """移除用户覆盖值，配置通知负责同步对应控件。"""
        try:
            self._configuration.reset(key)
            self._sync(key)
            self._error.hide()
        except (OSError, ValueError, RuntimeError, PluginError) as exc:
            self._show_error(key, exc)

    def _show_error(self, key: str, error: Exception) -> None:
        """展示保存错误，并在上下文仍可用时恢复控件的真实持久值。"""
        self._error.setText(tr("保存插件设置失败：{error}", error=error))
        self._error.show()
        try:
            self._sync(key)
        except (OSError, ValueError, RuntimeError, PluginError):
            self._controls[key].setEnabled(False)

    def _changed(self, event: ConfigurationChange) -> None:
        """将插件或其他宿主入口写入的配置变化同步到当前控件。"""
        if event.key in self._controls:
            self._sync(event.key)

    def retheme(self) -> None:
        """原位刷新说明和错误颜色，保留正在编辑的控件状态。"""
        for label, token in self._labels:
            label.setStyleSheet(f"background: transparent; color: {theme.color(token)}; font-size: 13px;")
