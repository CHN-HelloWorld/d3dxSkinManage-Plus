"""在工作线程转换独立视频帧，输出尺寸匹配显示区域。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from src.ui.backgrounds.video_convert import convert_mapped_frame
from src.ui.backgrounds.video_frame import clear_video_frame
from src.utils.i18n import t as tr


def convert_owned_frame(frame, size, fit):
    """在所属工作线程完成单帧转换与释放。"""
    image, error = QImage(), ""
    try:
        converted = convert_mapped_frame(frame, size, fit)
        if converted is None:
            image = frame.toImage()
            if not image.isNull():
                aspect = (
                    Qt.AspectRatioMode.KeepAspectRatio
                    if fit == "contain"
                    else Qt.AspectRatioMode.KeepAspectRatioByExpanding
                )
                image = image.scaled(*size, aspect, Qt.TransformationMode.SmoothTransformation)
        else:
            image = converted
    except Exception as exc:
        error = tr("视频画面转换失败：{exc}", exc=exc)
    finally:
        clear_video_frame(frame)
    return image, error
