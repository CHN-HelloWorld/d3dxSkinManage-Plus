"""配置内存图像保留，磁盘缓存使用独立设置。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from src.core.image_memory_policy import MAX_IMAGE_CACHE_MB, MIB, MIN_IMAGE_CACHE_MB
from src.ui import image_memory_runtime
from src.ui.widgets.int_line_edit import IntLineEdit
from src.ui.widgets.settings_components import (
    CacheSplit,
    SettingsCard,
    SettingsRow,
    plain_widget,
    style_button,
    text_label,
)
from src.utils.i18n import t
from src.utils.i18n import t as tr
from src.utils.image_cache_budget import image_cache_budget


class ImageMemorySettings(SettingsCard):
    """提供图片内存额度、自动推荐、实时占用及缓存释放的设置卡片。"""

    settings_changed = Signal()

    def __init__(self, config, parent: QWidget | None = None):
        """构建内存用量摘要与额度控制，绑定保存和清理动作并准备可见时运行的刷新定时器。"""
        super().__init__(t("图片内存缓存"), "memory", tr("保留近期图片，让总览、画廊与在线 Mod 回看更快。"), "RAM")
        if parent is not None:
            self.setParent(parent)
        self._config = config
        summary = QWidget()
        usage_layout = QVBoxLayout(summary)
        usage_layout.setContentsMargins(0, 0, 20, 0)
        usage_layout.setSpacing(8)
        usage_layout.addWidget(text_label(tr("当前图片占用"), "smallHelp"))
        metric = QHBoxLayout()
        metric.setContentsMargins(0, 0, 0, 0)
        metric.setSpacing(5)
        self.usage_label = text_label("", "metric")
        metric.addWidget(self.usage_label, 0, Qt.AlignmentFlag.AlignBaseline)
        metric.addWidget(text_label("MiB", "smallHelp"), 0, Qt.AlignmentFlag.AlignBaseline)
        metric.addStretch(1)
        usage_layout.addLayout(metric)
        meta = QHBoxLayout()
        self.limit_label = text_label("", "smallHelp")
        self.percent_label = text_label("", "smallHelp")
        meta.addWidget(self.limit_label)
        meta.addStretch(1)
        meta.addWidget(self.percent_label)
        usage_layout.addLayout(meta)
        self.usage_bar = QProgressBar()
        self.usage_bar.setObjectName("settingsUsageBar")
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setFixedHeight(6)
        usage_layout.addWidget(self.usage_bar)
        usage_layout.addWidget(text_label(tr("仅统计图片缓存，非程序总内存。"), "muted", wrap=True))
        usage_layout.addStretch(1)
        controls = plain_widget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(0)
        controls_layout.addStretch(1)
        amount = QHBoxLayout()
        amount.setContentsMargins(0, 0, 0, 0)
        amount.setSpacing(8)
        self.memory_input = IntLineEdit(MIN_IMAGE_CACHE_MB, MAX_IMAGE_CACHE_MB)
        self.memory_input.setFixedWidth(76)
        self.memory_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.memory_input.setAccessibleName(tr("图片内存额度（MiB）"))
        self.memory_input.setToolTip(
            t(
                "图片缓存总额度：{minimum}–{maximum} MiB；缩小后立即释放超额缓存。",
                minimum=MIN_IMAGE_CACHE_MB,
                maximum=MAX_IMAGE_CACHE_MB,
            )
        )
        amount.addWidget(self.memory_input)
        amount.addWidget(text_label("MiB", "muted"))
        self.auto_button = style_button(QPushButton(tr("自动推荐")))
        self.auto_button.setCheckable(True)
        self.memory_input.textEdited.connect(lambda _text: self.auto_button.setChecked(False))
        self.auto_button.clicked.connect(self._restore_auto)
        amount.addWidget(self.auto_button)
        self._quota_row = SettingsRow(
            tr("缓存额度"),
            amount,
            tr(
                "{MIN_IMAGE_CACHE_MB}–{MAX_IMAGE_CACHE_MB} MiB，按需占用",
                MIN_IMAGE_CACHE_MB=MIN_IMAGE_CACHE_MB,
                MAX_IMAGE_CACHE_MB=MAX_IMAGE_CACHE_MB,
            ),
            dense=True,
            keywords=tr("额度分配 手动额度 推荐额度"),
        )
        self._quota_row.description_label.setWordWrap(False)
        controls_layout.addWidget(self._quota_row)
        self.pressure_label = text_label("", "warning", wrap=True)
        self.pressure_label.hide()
        controls_layout.addWidget(self.pressure_label)
        self.error_label = text_label("", "error", wrap=True)
        self.error_label.hide()
        controls_layout.addWidget(self.error_label)
        controls_layout.addStretch(1)
        self._split = CacheSplit(summary, controls)
        self.rows.addWidget(self._split)
        self.clear_button = style_button(QPushButton(t("清理图片内存")), icon="clean")
        self.clear_button.setToolTip(t("释放已缓存的图片；控件当前显示的图片继续保留，后续浏览按需加载。"))
        self.clear_button.clicked.connect(self._clear)
        self.add_footer(tr("释放缓存后，当前显示的图片继续保留。"), self.clear_button)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_usage)
        self.reload()
        self.memory_input.valueChanged.connect(self._manual_changed)
        self.memory_input.editingFinished.connect(
            lambda: (
                self._manual_changed(self.memory_input.value())
                if not self._manual and not self.auto_button.isChecked()
                else None
            )
        )

    def retheme(self):
        """将缓存摘要与额度控制分栏同步到当前主题。"""
        self._split.retheme()

    def reload(self) -> None:
        """应用已保存的图片内存配置，恢复手动或推荐额度并刷新实时占用。"""
        recommended = image_memory_runtime.apply_image_memory_config(self._config)
        self._manual = self._config.get("images.memory_cache_mode") == "manual"
        self.memory_input.setValue(self._config.get("images.memory_cache_mb") if self._manual else recommended)
        self.auto_button.setChecked(not self._manual)
        self.auto_button.setToolTip(
            tr("按设备推荐 {recommended} MiB；直接编辑额度可手动设置。", recommended=recommended)
        )
        self.refresh_usage()

    def _save(self, changes: dict) -> None:
        """原子保存图片配置变化，失败时恢复原配置并展示错误，成功时发出设置变化信号。"""
        preferences = self._config.get("images")
        preferences.update(changes)
        if not self._config.set_atomic("images", preferences):
            self.reload()
            self.error_label.setText(t("保存失败，已保持原设置。请检查设置文件是否可写。"))
            self.error_label.show()
            return
        self.error_label.clear()
        self.error_label.hide()
        self.reload()
        self.settings_changed.emit()

    def _manual_changed(self, value: int) -> None:
        """将输入额度以手动模式保存到图片内存配置。"""
        self._save({"memory_cache_mode": "manual", "memory_cache_mb": value})

    def _restore_auto(self) -> None:
        """保存自动推荐模式，使运行时重新按设备条件分配图片内存额度。"""
        self._save({"memory_cache_mode": "auto"})

    def _clear(self) -> None:
        """清空受统一预算管理的图片缓存并立即更新占用显示。"""
        image_cache_budget.clear()
        self.refresh_usage()

    def refresh_usage(self) -> None:
        """读取缓存预算快照，更新占用、额度、百分比和系统内存压力导致的临时限额提示。"""
        usage = image_cache_budget.snapshot()
        self.usage_label.setText(f"{usage.bytes_used / MIB:.1f}")
        self.usage_label.setAccessibleDescription(
            t(
                "已缓存 {used} MiB / 当前上限 {limit} MiB",
                used=f"{usage.bytes_used / MIB:.1f}",
                limit=f"{usage.limit_bytes / MIB:.0f}",
            )
        )
        self.limit_label.setText(
            f"{tr('临时上限') if usage.under_pressure else tr('生效上限')} {usage.limit_bytes / MIB:.0f} MiB"
        )
        self.percent_label.setText(f"{min(100, round(100 * usage.bytes_used / max(1, usage.limit_bytes)))}%")
        self.usage_bar.setValue(min(1000, int(1000 * usage.bytes_used / max(1, usage.limit_bytes))))
        self.pressure_label.setText(
            t(
                "系统内存紧张，已临时降低额度并暂停提前加载；内存充足后恢复 {mb} MiB。",
                mb=f"{usage.configured_bytes / MIB:.0f}",
            )
            if usage.under_pressure
            else ""
        )
        self.pressure_label.setVisible(usage.under_pressure)

    def showEvent(self, event) -> None:
        """显示设置卡片时立即刷新缓存占用，并开始每秒更新。"""
        super().showEvent(event)
        self.refresh_usage()
        self._timer.start()

    def hideEvent(self, event) -> None:
        """隐藏设置卡片时停止用量刷新定时器。"""
        self._timer.stop()
        super().hideEvent(event)
