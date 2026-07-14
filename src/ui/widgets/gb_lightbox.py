"""LightBox - 全屏图查看，1920px 懒加载"""

from __future__ import annotations

import re

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from src.ui import anim
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


def _upgrade_to_1920(url: str) -> str:
    """GameBanana 缩略图 URL 升级为高清版"""
    return re.sub(r"/(220-|480-|160-|110-|400-)", "/", url)


class GbLightBox(QDialog):
    """全屏图查看器"""

    def __init__(self, *, image_loader, parent=None) -> None:
        """绑定图片加载器，创建无边框模态预览窗口及加载指示器。"""
        super().__init__(parent)
        self.setWindowTitle(tr("图片预览"))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.Dialog)
        self.setModal(True)
        self.setStyleSheet("background: rgba(0, 0, 0, 200);")
        self._loader = image_loader

        layout = QVBoxLayout(self)
        self.image_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(320)
        layout.addStretch(1)
        layout.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image_label, 1)
        layout.addStretch(1)

    def show_for_url(self, url: str) -> None:
        """将图片地址升级到大图规格，以主屏幕九成尺寸请求图片并全屏显示淡入预览。"""
        screen = QApplication.primaryScreen().size()
        self.resize(screen)
        self.move(0, 0)
        self.progress.show()
        self.image_label.clear()
        target_size = QSize(int(screen.width() * 0.9), int(screen.height() * 0.9))
        self._loader.load(_upgrade_to_1920(url), self.image_label, size=target_size)
        call_later(100, self.progress, self.progress.hide)
        self.show()
        self.raise_()
        self.setFocus()
        # 整屏查看器淡入（顶级窗口 windowOpacity，复用实例每次打开都淡入）
        anim.fade_in_window(self)

    def keyPressEvent(self, event) -> None:
        """按 Escape 关闭预览，其余按键交由对话框默认处理。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        """点击图片标签之外的区域时关闭预览，并继续默认鼠标事件处理。"""
        pos = event.position().toPoint()
        target = self.childAt(pos)
        if target is not self.image_label:
            self.close()
        super().mousePressEvent(event)
