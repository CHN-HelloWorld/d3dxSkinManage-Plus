"""在界面线程采样并配置进程级图像缓存预算。"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QGuiApplication

from src.core.image_memory_policy import (
    MIB,
    available_memory,
    effective_image_cache_mb,
    physical_memory_bytes,
    recommend_image_cache_mb,
)
from src.utils.image_cache_budget import image_cache_budget


def recommended_image_cache_mb() -> int:
    """综合物理内存与各显示器尺寸计算缓存建议值，取支持最大显示器的 MiB 预算。"""
    memory = physical_memory_bytes()
    app = QGuiApplication.instance()
    screens = app.screens() if app is not None else []
    if not screens:
        return recommend_image_cache_mb(memory)
    # 共享缓存预算须支持窗口移动到最大的显示器。
    return max(
        recommend_image_cache_mb(
            memory,
            width=screen.availableGeometry().width(),
            height=screen.availableGeometry().height(),
            dpr=screen.devicePixelRatio(),
        )
        for screen in screens
    )


def apply_image_memory_config(config) -> int:
    """解析自动或手动缓存配置并应用字节预算，返回当前环境的建议 MiB 值。"""
    recommended = recommended_image_cache_mb()
    budget_mb = effective_image_cache_mb(
        config.get("images.memory_cache_mode"),
        config.get("images.memory_cache_mb"),
        recommended,
    )
    image_cache_budget.set_budget(budget_mb * MIB)
    return recommended


class ImageMemoryMonitor(QObject):
    """每个主窗口使用自己的定时器，全局回调不持有窗口。"""

    def __init__(self, config, parent: QObject):
        """保存配置并立即采样内存，随后每五秒刷新缓存预算与内存压力。"""
        super().__init__(parent)
        self._config = config
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()
        self._timer.start()

    def refresh(self) -> None:
        """重新应用缓存设置，并将可用内存与压力信息交给预算控制器。"""
        self.apply_settings()
        image_cache_budget.observe_memory(*available_memory())

    def apply_settings(self) -> None:
        """根据当前配置和显示器环境更新共享图片缓存预算。"""
        apply_image_memory_config(self._config)

    def stop(self) -> None:
        """停止周期内存采样，保留已应用的缓存配置。"""
        self._timer.stop()
