"""在线图片磁盘额度：当前上限与滑块量程独立，所有对外数值以 MB 计。

使用真实 Qt 布局显示固定读数、全宽滑块和量程输入。磁盘总占用通过
usage_changed 交给父级卡片展示，不将它映射成在线图片额度的占用比例。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme
from src.utils.i18n import t as tr

_UNIT_FACTORS = {"MB": 1, "GB": 1024, "TB": 1024 * 1024}
_UNITS_ORDER = ("MB", "GB", "TB")
_MAX_RANGE_MB = 1024 * 1024 * 1024
_MIN_RANGE_MB = 64


class CacheCapSlider(QWidget):
    """当前额度（0 为不限制）与用户定义的滑块最大值。"""

    value_changed = Signal(int)
    range_changed = Signal(int)
    usage_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        """构建在线图片磁盘缓存额度滑块、范围编辑器和单位选择器，并初始化同步保护状态。

        当前缓存上限与滑块最大值分别保存，界面宽度不足时范围设置切换为纵向布局。
        """
        super().__init__(parent)
        self.setObjectName("CacheCapSlider")
        self._value_mb = 0
        self._range_mb = 4096
        self._unit = "MB"
        self._usage_mb = -1.0
        self._syncing = False
        self._compact = None
        self._displayed_range = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(260, 100)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # 允许宽行缩小后重新排列，保留最小宽度并由布局决定高度。
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        heading = QHBoxLayout()
        heading.setSpacing(12)
        heading_info = QVBoxLayout()
        heading_info.setSpacing(4)
        self._title = self._label(tr("在线图片缓存上限"), "title")
        self._help = self._label(tr("只限制在线图片的磁盘占用"), "help")
        heading_info.addWidget(self._title)
        heading_info.addWidget(self._help)
        heading.addLayout(heading_info, 1)
        self._readout = self._label(tr("不限制"), "readout", wrap=False)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self._readout)
        self._layout.addLayout(heading)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, self._range_mb)
        self._slider.setSingleStep(64)
        self._slider.setPageStep(256)
        self._slider.setMinimumWidth(150)
        self._slider.setFixedHeight(22)
        self._slider.setAccessibleName(tr("在线图片缓存上限"))
        self._slider.setAccessibleDescription(tr("0 表示不限制；用方向键调整当前上限。"))
        self._slider.valueChanged.connect(self._on_slider_moved)
        self._layout.addWidget(self._slider)

        ends = QHBoxLayout()
        ends.addWidget(self._label(tr("0 · 不限制"), "muted", wrap=False))
        ends.addStretch(1)
        self._range_end = self._label("", "muted", wrap=False)
        ends.addWidget(self._range_end)
        self._layout.addLayout(ends)

        self._divider = QWidget(self)
        self._divider.setObjectName("cacheCapDivider")
        self._divider.setFixedHeight(1)
        self._layout.addWidget(self._divider)

        self._range_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._range_row.setSpacing(16)
        self._range_info = QWidget(self)
        info_layout = QVBoxLayout(self._range_info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)
        self._range_title = self._label(tr("滑块最大值"), "title")
        self._range_help = self._label(tr("决定可拖动范围，不等于当前上限"), "help")
        info_layout.addWidget(self._range_title)
        info_layout.addWidget(self._range_help)
        self._range_row.addWidget(self._range_info)

        self._range_controls = QWidget(self)
        controls_layout = QHBoxLayout(self._range_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(7)
        self._edit = QLineEdit(self._range_controls)
        self._edit.setFixedSize(76, 32)
        self._edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._edit.setAccessibleName(tr("滑块最大值"))
        self._edit.setAccessibleDescription(self._range_help.text())
        validator = QDoubleValidator(0.0, float(_MAX_RANGE_MB), 3, self)
        validator.setLocale(QLocale.c())
        self._edit.setValidator(validator)
        self._edit.editingFinished.connect(self._on_edit_committed)
        controls_layout.addWidget(self._edit)
        self._range_title.setBuddy(self._edit)

        self._unit_combo = QComboBox(self._range_controls)
        for unit in _UNITS_ORDER:
            self._unit_combo.addItem(unit, unit)
        self._unit_combo.setFixedSize(80, 32)
        self._unit_combo.setAccessibleName(tr("滑块最大值的单位"))
        self._unit_combo.currentIndexChanged.connect(self._on_unit_changed)
        controls_layout.addWidget(self._unit_combo)
        controls_layout.addStretch(1)
        self._range_row.addWidget(self._range_controls, 1)
        self._layout.addLayout(self._range_row)

        self._policy = self._label(tr("超出上限时，优先移除最久未浏览的在线缩略图，再次浏览时按需下载。"), "muted")
        self._layout.addWidget(self._policy)
        self.retheme()
        self._set_compact(self.width() < 450)
        self._refresh_edit_from_range()
        self._refresh_readouts()

    def _label(self, text: str, role: str, *, wrap: bool = True) -> QLabel:
        """创建带缓存设置角色属性的标签，并允许文字按需换行和横向压缩。"""
        label = QLabel(text, self)
        label.setProperty("cacheCapRole", role)
        label.setWordWrap(wrap)
        label.setMinimumWidth(0)
        return label

    def retheme(self) -> None:
        """刷新局部 Fluent token；父页面在主题切换后调用。"""
        colors = {
            key: theme.color(f"settings.{key}")
            for key in ("text", "secondary", "muted", "line", "control", "surface", "soft", "accent")
        }
        self.setStyleSheet(f"""
            QWidget#CacheCapSlider, QWidget#CacheCapSlider QWidget {{
                background: transparent; border: none;
            }}
            QLabel[cacheCapRole="title"] {{
                color: {colors["text"]}; font-size: 12px; font-weight: 600;
            }}
            QLabel[cacheCapRole="help"] {{ color: {colors["secondary"]}; font-size: 11px; }}
            QLabel[cacheCapRole="muted"] {{ color: {colors["muted"]}; font-size: 11px; }}
            QLabel[cacheCapRole="readout"] {{
                color: {colors["accent"]}; font-size: 18px; font-weight: 600;
            }}
            QWidget#CacheCapSlider QWidget#cacheCapDivider {{ background: {colors["line"]}; }}
            QWidget#CacheCapSlider QLineEdit, QWidget#CacheCapSlider QComboBox {{
                color: {colors["text"]}; background: {colors["surface"]};
                border: 1px solid {colors["control"]}; border-radius: 4px;
                padding: 0 8px; font-size: 12px;
            }}
            QWidget#CacheCapSlider QLineEdit:focus, QWidget#CacheCapSlider QComboBox:focus {{
                border-color: {colors["accent"]};
            }}
            QWidget#CacheCapSlider QComboBox::drop-down {{ border: none; width: 22px; }}
            QWidget#CacheCapSlider QComboBox QAbstractItemView {{
                color: {colors["text"]}; background: {colors["surface"]};
                selection-background-color: {colors["soft"]};
            }}
            QWidget#CacheCapSlider QSlider::groove:horizontal {{
                height: 5px; border-radius: 2px; background: {colors["line"]};
            }}
            QWidget#CacheCapSlider QSlider::sub-page:horizontal {{
                background: {colors["accent"]}; border-radius: 2px;
            }}
            QWidget#CacheCapSlider QSlider::handle:horizontal {{
                width: 9px; margin: -6px 0;
                border: 4px solid {colors["surface"]}; border-radius: 8px;
                background: {colors["accent"]};
            }}
            QWidget#CacheCapSlider QSlider::handle:horizontal:focus {{
                border-color: {colors["control"]};
            }}
        """)
        self._readout.ensurePolished()
        self._readout.setFixedWidth(self._readout.fontMetrics().horizontalAdvance("1023.999 TB") + 2)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        """结合布局自然尺寸和按宽度换行的高度返回控件建议尺寸。"""
        hint = self._layout.sizeHint()
        width = max(460, hint.width())
        height = self._layout.totalHeightForWidth(width)
        return QSize(width, max(hint.height(), height, 100))

    def minimumSizeHint(self) -> QSize:
        """返回允许紧凑布局的最小宽度及能容纳内容的最小高度。"""
        return QSize(260, max(100, self._layout.minimumSize().height()))

    def _set_compact(self, compact: bool) -> None:
        """按紧凑状态切换范围说明与输入控件的横向或纵向排列，并刷新布局尺寸。"""
        if self._compact == compact:
            return
        self._compact = compact
        self._range_info.setMinimumWidth(0 if compact else 180)
        self._range_info.setMaximumWidth(16777215 if compact else 180)
        self._range_row.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        self._range_row.setSpacing(8 if compact else 16)
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        """依据当前宽度是否小于四百五十像素切换范围编辑区布局。"""
        super().resizeEvent(event)
        self._set_compact(self.width() < 450)

    def value_mb(self) -> int:
        """返回当前缓存上限的 MB 值，零表示不限制。"""
        return self._value_mb

    def range_max_mb(self) -> int:
        """返回滑块可拖动范围的最大 MB 值。"""
        return self._range_mb
