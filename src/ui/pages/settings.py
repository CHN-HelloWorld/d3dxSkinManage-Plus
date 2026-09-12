"""设置页 - 游戏自定义配置、主题立即生效、i18n"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from time import perf_counter

import shiboken6
from PySide6.QtCore import QObject, QRect, QSignalBlocker, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from src.core.archive_concurrency import (
    MAX_ARCHIVE_WORKERS,
    effective_archive_workers,
    recommended_archive_workers,
)
from src.core.config import Config
from src.core.deploy_concurrency import (
    MAX_DEPLOY_CONCURRENCY,
    effective_deploy_concurrency,
    recommended_deploy_concurrency,
)
from src.core.mod_update_concurrency import (
    MAX_UPDATE_CONCURRENCY,
    effective_update_concurrency,
    recommended_update_concurrency,
)
from src.ui import theme
from src.ui.fonts import installed_families
from src.ui.shortcut_manager import ShortcutSettings
from src.ui.widgets.background_settings import BackgroundSettingsMixin
from src.ui.widgets.font_picker import install_font_previews
from src.ui.widgets.image_memory_settings import ImageMemorySettings
from src.ui.widgets.int_line_edit import IntLineEdit
from src.ui.widgets.settings_components import (
    CacheSplit,
    ChoiceSegments,
    SettingsRow,
    ThemeChoices,
    plain_widget,
    settings_note,
    style_button,
    text_label,
)
from src.ui.widgets.settings_navigation import SettingsCategoryPage
from src.ui.widgets.settings_shell import SettingsShellMixin
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import get_i18n, t
from src.utils.i18n import t as tr
from src.utils.language_pack import normalize_language_code
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class _ProxyTestWorker(QObject):
    """在独立线程测试代理连通性与延迟，结果通过信号返回。"""

    finished = Signal(bool, str)

    def __init__(self, proxy: str | None) -> None:
        """保存待检测的代理地址，将连接测试留给后台执行入口。"""
        super().__init__()
        self._proxy = proxy

    def run(self) -> None:
        """在后台检测代理连通性和 HTTP 延迟，并将成功结果或异常说明统一发回界面。"""
        # 先通过 TCP 握手检查代理端口，再经代理请求 generate_204，测量实际 HTTP 往返延迟。
        from src.utils.proxy import measure_proxy_latency

        try:
            ok, msg = measure_proxy_latency(self._proxy, timeout=6.0)
        except Exception as e:
            ok, msg = False, tr("测试失败：{e}", e=e)
        self.finished.emit(ok, msg)


class _CacheSizeWorker(QObject):
    """后台统计缓存目录总占用；rglob 大目录可能上千文件，放线程避免卡 UI。"""

    finished = Signal(float)  # 占用 MB

    def run(self) -> None:
        """尽力累计可读取缓存文件的大小，以 MiB 返回统计结果。"""
        from src.utils.paths import get_cache_dir

        total = 0
        try:
            for f in get_cache_dir().rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        self.finished.emit(total / (1024 * 1024))


class _UsageBar(QWidget):
    """占用条（下载数据行）：整条 = 总占用，左侧高亮段 = 其中「已入库可清理」的比例。

    颜色在 paintEvent 里现取 `theme.color`，故换主题自动跟随，无需登记重涂器。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建固定高度的透明用量条，初始可清理比例为零。"""
        super().__init__(parent)
        self._ratio = 0.0
        self.setFixedHeight(6)
        self.setMinimumWidth(200)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_ratio(self, ratio: float) -> None:
        """将用量比例限制在零到一之间，并请求重绘。"""
        self._ratio = max(0.0, min(1.0, float(ratio or 0.0)))
        self.update()
