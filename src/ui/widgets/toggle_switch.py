"""保留 QCheckBox 接口的自绘拨动开关。

颜色来自当前主题，位置及填充随状态变化过渡。
选中状态同步更新，动画只影响显示；关闭动画时直接应用最终外观。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox

from src.ui import anim, theme


class ToggleSwitch(QCheckBox):
    """Win11 风格拨动开关。固定尺寸，右对齐放入设置行。"""

    _W = 42
    _H = 22

    def __init__(self, parent=None) -> None:
        """创建无文字开关，并将选中状态连接到滑块位置动画。"""
        super().__init__(parent)
        self.setText("")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self._W, self._H)
        # 旋钮归一化位置：0=关（左）/1=开（右）。初值与当前勾选态一致，避免首帧跳动。
        self._knob_pos = 1.0 if self.isChecked() else 0.0
        self._knob_anim: QPropertyAnimation | None = None
        # 勾选态变化 → 平滑滑动（setChecked / 点击 / 键盘都会触发 toggled）
        self.toggled.connect(self._animate_knob)

    def hitButton(self, pos) -> bool:
        """整个 42×22 控件区域都可点击。

        QCheckBox 默认仅将样式定义的指示器区域作为点击区，本控件则自行绘制整条开关。
        使用完整 rect 判断命中，确保右侧区域的点击也能触发 toggled。
        """
        return self.rect().contains(pos)

    def sizeHint(self) -> QSize:
        """返回开关轨道的固定逻辑尺寸。"""
        return QSize(self._W, self._H)

    def setChecked(self, checked: bool) -> None:
        """同步勾选状态；信号被屏蔽或控件尚未显示时直接设置旋钮位置。"""
        super().setChecked(checked)
        target = 1.0 if checked else 0.0
        if (self.signalsBlocked() or not self.isVisible()) and self._knob_pos != target:
            self._knob_pos = target
            self.update()

    # 动画属性
    def _get_knob(self) -> float:
        """返回滑块在关闭与开启端点之间的归一化位置。"""
        return self._knob_pos

    def _set_knob(self, value: float) -> None:
        """更新动画属性中的滑块位置并请求重绘。"""
        self._knob_pos = value
        self.update()

    # QPropertyAnimation 目标属性（float 0..1）
    knobPos = Property(float, _get_knob, _set_knob)

    def _animate_knob(self, checked: bool) -> None:
        """停止已有位移动画并从当前位置缓动到目标端点，禁用动画时直接完成状态切换。"""
        target = 1.0 if checked else 0.0
        if not anim.is_enabled():
            self._knob_pos = target
            self.update()
            return
        if self._knob_anim is not None:
            try:
                self._knob_anim.stop()
            except RuntimeError:
                pass
            self._knob_anim = None
        a = QPropertyAnimation(self, b"knobPos", self)
        a.setDuration(anim.DURATION["fast"])
        a.setStartValue(self._knob_pos)
        a.setEndValue(target)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._knob_anim = a

    @staticmethod
    def _lerp_color(c1_hex: str, c2_hex: str, t: float) -> QColor:
        """按指定比例对两个颜色的 RGB 通道进行线性插值。"""
        a, b = QColor(c1_hex), QColor(c2_hex)
        return QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
        )

    def paintEvent(self, _event) -> None:
        """依据滑块位置混合关闭边框、开启底色与滑块颜色，绘制连续过渡的开关外观。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self._W, self._H
        r = h / 2
        kd = h - 8  # 旋钮直径
        ky = 4
        t = max(0.0, min(1.0, self._knob_pos))

        # 轨道：关态描边 与 开态实填 按 t 交叉淡入（避免突变）
        if t < 1.0:
            p.setOpacity(1.0 - t)
            pen = QPen(QColor(theme.color("text.tertiary")))
            pen.setWidthF(1.4)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, w - 2, h - 2, r - 1, r - 1)
        if t > 0.0:
            p.setOpacity(t)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.color("accent")))
            p.drawRoundedRect(0, 0, w, h, r, r)
        p.setOpacity(1.0)

        # 旋钮：x 位置在 关位(4) 与 开位(w-kd-4) 之间插值；颜色 次要灰→白 插值
        off_x, on_x = 4.0, float(w - kd - 4)
        kx = off_x + (on_x - off_x) * t
        knob_color = self._lerp_color(theme.color("text.secondary"), "#ffffff", t)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QRectF(kx, float(ky), float(kd), float(kd)))
        p.end()
