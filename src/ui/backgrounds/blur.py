"""对动态背景降采样后执行有界模糊，不逐帧创建图形场景。"""

from PIL import Image, ImageFilter
from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage

from src.ui.backgrounds.video_convert import scale_image


class _Signals(QObject):
    """发布模糊任务的代次标识与结果图像。"""

    ready = Signal(int, QImage)


class BlurFrame(QRunnable):
    """在线程池中缩小、模糊并按显示区域缩放背景帧。"""

    def __init__(self, generation: int, image: QImage, amount: int, size=None, fit="cover"):
        """保存输入图像、模糊强度及可选目标尺寸，并创建完成信号对象。"""
        super().__init__()
        self.generation, self.image, self.amount = generation, image, amount
        self.size, self.fit = size, fit
        self.signals = _Signals()

    def run(self):
        """执行图像处理并始终发出完成信号；处理异常时返回原始图像。"""
        result = self.image
        try:
            if self.amount:
                reduced = result.size().scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio)
                # 先从捕获缓冲降采样，只将小图交给 Pillow，避免完整的 Python 像素复制。
                small = scale_image(result, reduced).convertToFormat(QImage.Format.Format_RGBA8888)
                raw = bytes(small.constBits())
                pixels = Image.frombytes("RGBA", (small.width(), small.height()), raw)
                radius = self.amount / 100 * 40 * reduced.width() / self.image.width()
                pixels = pixels.filter(ImageFilter.GaussianBlur(radius))
                data = pixels.tobytes()
                result = QImage(
                    data, pixels.width, pixels.height, pixels.width * 4, QImage.Format.Format_RGBA8888
                ).copy()
                if not self.image.hasAlphaChannel():
                    result = result.convertToFormat(QImage.Format.Format_RGB32)
            if self.size:
                aspect = (
                    Qt.AspectRatioMode.KeepAspectRatio
                    if self.fit == "contain"
                    else Qt.AspectRatioMode.KeepAspectRatioByExpanding
                )
                target = self.image.size().scaled(*self.size, aspect)
                result = scale_image(result, target)
        except Exception:
            # 无效或空帧不能终止 Qt 工作线程。
            result = self.image
        finally:
            self.signals.ready.emit(self.generation, result)
