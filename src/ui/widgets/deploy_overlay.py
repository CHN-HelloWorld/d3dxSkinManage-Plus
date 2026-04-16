"""在卡片上显示部署进度及百分比。

覆盖层随卡片尺寸变化，进度由外部更新；不持有后台线程或业务模型。
总览页面在卡片回收时同时释放覆盖层。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from src.ui import theme
from src.utils.i18n import t as tr


class DeployOverlay(QWidget):
    """覆盖在卡片上的部署进度蒙层（半透明遮罩 + 居中进度环 + 百分比文字）。"""

    def __init__(self, parent: QWidget):
        """创建可阻挡鼠标操作的透明部署遮罩，初始化进度环尺寸并默认隐藏。"""
        super().__init__(parent)
        self._progress = 0.0
        # 自绘半透明部署遮罩并接收鼠标事件，阻止操作下方正在部署的卡片。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._ring_px = 44  # 进度环外径
        self._line_width = 4
        self.hide()

    def set_progress(self, frac: float) -> None:
        """将进度限制在零到一之间，更新部署提示并请求重绘。"""
        self._progress = max(0.0, min(1.0, frac))
        self.setToolTip(tr("正在应用配置并保存启用状态…") if self._progress >= 1 else tr("正在准备并部署 Mod…"))
        self.update()

    def cover(self) -> None:
        """铺满父控件并置顶显示。"""
        p = self.parentWidget()
        if p is not None:
            self.setGeometry(p.rect())
        self.raise_()
        self.show()

    def paintEvent(self, event) -> None:
        """绘制半透明遮罩、圆环进度和准备或收尾状态文字，并确保画笔资源结束使用。"""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            # ① 半透明遮罩（压暗卡片，聚焦进度）
            painter.fillRect(self.rect(), QColor(0, 0, 0, 130))

            # ② 居中进度环
            cx, cy = self.width() / 2, self.height() / 2
            r = self._ring_px / 2
            rect = QRectF(cx - r, cy - r, self._ring_px, self._ring_px)
            # 轨道
            painter.setPen(QPen(QColor(255, 255, 255, 60), self._line_width))
            painter.drawEllipse(rect)
            # 进度弧（12 点方向起，顺时针）
            pen = QPen(QColor(theme.color("accent")), self._line_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            start = 90 * 16
            span = -int(self._progress * 360 * 16)
            painter.drawArc(rect, start, span)

            # ③ 居中百分比
            painter.setPen(QColor(255, 255, 255, 235))
            f: QFont = painter.font()
            f.setPixelSize(12)
            f.setBold(True)
            painter.setFont(f)
            text = (
                tr("收尾中")
                if self._progress >= 1
                else tr("准备中")
                if self._progress <= 0
                else f"{int(self._progress * 100)}%"
            )
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )
        finally:
            # 回调结束后父图形效果即可释放源图，包括绘制抛出异常的情况。
            painter.end()
