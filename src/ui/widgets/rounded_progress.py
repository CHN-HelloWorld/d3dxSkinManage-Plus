"""带裁剪的忙碌指示器，不依赖系统进度块样式。"""

from PySide6.QtCore import QElapsedTimer, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QProgressBar

from src.ui import theme


class RoundedProgressBar(QProgressBar):
    """为不确定进度状态绘制往返移动的圆角进度块。"""

    def __init__(self, parent=None):
        """创建动画时钟和重绘定时器，等待控件显示后启动动画。"""
        super().__init__(parent)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(32)
        self._timer.timeout.connect(self.update)

    def showEvent(self, event):
        """显示时重置动画起点并启动重绘计时。"""
        super().showEvent(event)
        self._clock.restart()
        self._timer.start()

    def hideEvent(self, event):
        """隐藏时停止动画定时器，避免不可见控件持续重绘。"""
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        """不确定进度时绘制圆角轨道和往返进度块，确定进度时停止动画并使用默认绘制。"""
        if self.minimum() != 0 or self.maximum() != 0:
            self._timer.stop()
            return super().paintEvent(event)
        if not self._timer.isActive() and self.isVisible():
            self._timer.start()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = min(6.0, float(self.height()))
        track = QRectF(0, (self.height() - height) / 2, self.width(), height)
        outline = QPainterPath()
        outline.addRoundedRect(track, height / 2, height / 2)
        painter.fillPath(outline, QColor(theme.color("bg.subtle")))
        painter.setClipPath(outline)
        width = max(height, track.width() * 0.32)
        phase = (self._clock.elapsed() % 1600) / 800
        fraction = phase if phase <= 1 else 2 - phase
        chunk = QRectF((track.width() - width) * fraction, track.top(), width, height)
        painter.setPen(QColor(theme.color("accent")))
        painter.setBrush(QColor(theme.color("accent")))
        painter.drawRoundedRect(chunk, height / 2, height / 2)
