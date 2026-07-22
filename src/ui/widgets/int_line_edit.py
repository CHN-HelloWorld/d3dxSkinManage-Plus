"""使用 QLineEdit 和 QIntValidator 实现无微调按钮的整数输入框。

提供与 QSpinBox 一致的 `value`、`setValue` 和 `valueChanged` 接口。
失焦或按回车时提交，将空值或越界值调整到 [minimum, maximum] 范围内。
仅在提交值变化时发出 `valueChanged(int)`，避免加载配置时误触发自动保存。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QLineEdit, QWidget


class _ClampedIntValidator(QIntValidator):
    """将提交时的非法或越界文本修正为范围内整数的验证器。"""

    def fixup(self, text: str) -> str:
        """解析并限制整数文本，无法解析时使用下限值作为修正结果。"""
        # 输入处于中间状态时先由校验器修正，避免失焦后不触发编辑完成。
        try:
            value = int(text.strip())
        except ValueError:
            value = self.bottom()
        return str(max(self.bottom(), min(self.top(), value)))


class IntLineEdit(QLineEdit):
    """提供数值读写和变更信号的整数输入框，程序化设置不发出变更信号。"""

    valueChanged = Signal(int)

    def __init__(self, minimum: int = 0, maximum: int = 99, parent: QWidget | None = None) -> None:
        """设置整数范围、初始下限值和右对齐输入，并在编辑结束时提交数值。"""
        super().__init__(parent)
        self._min = int(minimum)
        self._max = int(maximum)
        self._value = self._min
        self.setValidator(_ClampedIntValidator(self._min, self._max, self))
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.setText(str(self._value))
        # 失焦 / 回车提交并夹紧
        self.editingFinished.connect(self._commit)

    # 数值读写接口。

    def setRange(self, minimum: int, maximum: int) -> None:
        """替换整数验证范围，并将当前值重新限制到新范围内。"""
        self._min = int(minimum)
        self._max = int(maximum)
        self.setValidator(_ClampedIntValidator(self._min, self._max, self))
        self.setValue(self._value)  # 重新夹紧

    def value(self) -> int:
        """返回最近已提交的整数值，而非尚未确认的输入文本。"""
        return self._value

    def setValue(self, v: int) -> None:
        """程序化设置并限制整数值，不发出变更信号，避免配置加载触发自动保存。"""
        v = max(self._min, min(self._max, int(v)))
        self._value = v
        self.blockSignals(True)
        self.setText(str(v))
        self.blockSignals(False)

    # 内部

    def _commit(self) -> None:
        """解析并限制输入值，仅在实际变化时发出 valueChanged。"""
        txt = (self.text() or "").strip()
        try:
            v = int(txt) if txt else self._min
        except ValueError:
            v = self._value
        v = max(self._min, min(self._max, v))
        old = self._value
        self._value = v
        # 回写（可能因夹紧/清空而与用户输入不同）
        self.blockSignals(True)
        self.setText(str(v))
        self.blockSignals(False)
        if v != old:
            self.valueChanged.emit(v)
