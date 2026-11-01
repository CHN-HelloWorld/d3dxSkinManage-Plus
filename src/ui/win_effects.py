"""为 Windows 窗口配置系统背景、标题栏和圆角。

不受支持的平台或系统调用失败时保留基础外观，不影响窗口功能。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QPainterPath, QRegion
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from src.utils.logger import logger

# DwmSetWindowAttribute 属性常量
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # BOOL：深色标题栏
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Win10 1809 / 1903 使用旧编号。
_DWMWA_SYSTEMBACKDROP_TYPE = 38  # int：背景材质（Win11 22H2+）
_DWMWA_WINDOW_CORNER_PREFERENCE = 33  # int：窗口圆角偏好（Win11）
_DWMWA_TRANSITIONS_FORCEDISABLED = 3  # BOOL：禁用窗口开/关 DWM 过渡动画

_WM_NCACTIVATE = 0x0086  # 更新非客户区的活动外观。

# DWM 窗口圆角偏好。
_DWMWCP_ROUND = 2  # 标准圆角(≈8px)
_DWMWCP_ROUNDSMALL = 3  # 小圆角(≈4px)

# DWM 系统背景材质。
_DWMSBT_MAINWINDOW = 2  # Mica
_DWMSBT_TRANSIENTWINDOW = 3  # Acrylic

_CORNER_RADIUS_PROPERTY = "_window_corner_radius"


class _RoundedCornerFilter(QObject):
    """为不支持系统圆角的窗口同步裁切区域，同时约束内容和原生模糊层。"""

    def eventFilter(self, window, event):
        """窗口几何、状态或句柄变化后更新裁剪，保持显示与输入区域一致。"""
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.WindowStateChange,
            QEvent.Type.WinIdChange,
            QEvent.Type.DevicePixelRatioChange,
            QEvent.Type.Paint,
        ):
            self.update_mask(window)
        return False

    def update_mask(self, window: QWidget) -> None:
        """按逻辑像素生成圆角区域；绘制前恢复被 Qt 原生弹窗样式覆盖的遮罩。"""
        radius = window.property(_CORNER_RADIUS_PROPERTY)
        if radius is None:
            return
        if window.isMaximized() or window.isFullScreen() or window.rect().isEmpty():
            window.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(window.rect()), radius, radius)
        region = QRegion(path.toFillPolygon().toPolygon())
        if window.mask() != region:
            window.setMask(region)


def _supported() -> bool:
    """返回当前平台是否为可调用原生窗口效果接口的 Windows。"""
    return sys.platform == "win32"


def _set_attr(hwnd: int, attr: int, value: int) -> bool:
    """设置整数型 DWM 窗口属性；接口失败返回 False 并记录调试信息。"""
    try:
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        val = ctypes.c_int(value)
        res = dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(attr),
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        return res == 0
    except Exception as e:
        logger.debug("DwmSetWindowAttribute(attr=%s) 失败: %s", attr, e)
        return False


def apply_dark_titlebar(window, enabled: bool) -> bool:
    """设置标题栏明暗，避免同步重绘并保留窗口原有焦点和活动状态。"""
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    value = 1 if enabled else 0
    if not (
        _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, value)
        or _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, value)
    ):
        return False
    try:
        flags = window.windowFlags()
        if (
            not window.isVisible()
            or flags & Qt.WindowType.FramelessWindowHint
            or not flags & Qt.WindowType.WindowTitleHint
        ):
            # 隐藏窗口在首次显示时采用新属性；自绘标题栏和浮层没有原生标题栏需要刷新。
            return True
        # Win10 缓存已绘制的标题栏颜色，需要更新活动外观后才会换色。
        # lParam=-1 禁止 DefWindowProc 同步重绘非客户区，交由 DWM 合成新外观。
        # 不缓存明暗状态：Qt 的调色板更新也可能改写同一 DWM 属性。
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        active = user32.GetForegroundWindow() == hwnd
        for state in (not active, active):
            user32.SendMessageW(wintypes.HWND(hwnd), _WM_NCACTIVATE, wintypes.WPARAM(state), wintypes.LPARAM(-1))
    except Exception as e:
        logger.debug("刷新标题栏失败: %s", e)
    return True


def apply_mica(window, theme_name: str) -> bool:
    """给窗口启用 Mica 背景并匹配标题栏明暗。失败静默回退。"""
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False

    apply_dark_titlebar(window, theme_name == "dark")
    ok = _set_attr(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_MAINWINDOW)
    if ok:
        logger.debug("已启用 Mica 背景 (%s)", theme_name)
    return ok


def apply_acrylic_backdrop(window, dark: bool = True) -> bool:
    """为支持的 Windows 11 窗口配置亚克力系统背景。

    调用方须准备透明窗口和半透明填充，以显示系统材质；不支持时返回失败。
    """
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    apply_dark_titlebar(window, dark)
    ok = _set_attr(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_TRANSIENTWINDOW)
    if ok:
        logger.debug("已启用 Acrylic 系统背景 (transient)")
    return ok


def apply_rounded_corners(window: QWidget, small: bool = False, *, fallback_radius: int | None = None) -> bool:
    """优先使用 DWM 圆角；系统不支持时用窗口区域裁切内容与模糊表面。

    QSS 圆角只影响绘制背景，不能限制 Windows 10 的矩形亚克力层。
    small 使用四个逻辑像素的半径，否则使用八个；区域裁切随窗口几何更新。
    fallback_radius 可匹配已采用自定义圆角尺寸的窗口，不改变系统圆角配置。
    """
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    pref = _DWMWCP_ROUNDSMALL if small else _DWMWCP_ROUND
    app = QApplication.instance()
    corner_filter = getattr(app, "_window_corner_filter", None)
    if _set_attr(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, pref):
        if window.property(_CORNER_RADIUS_PROPERTY) is not None:
            window.setProperty(_CORNER_RADIUS_PROPERTY, None)
            window.removeEventFilter(corner_filter)
            window.clearMask()
        return True
    if corner_filter is None:
        # Qt 缓存弹窗的 Python 包装对象可能被回收，由应用持有过滤器以保留后续回调。
        corner_filter = _RoundedCornerFilter(app)
        app._window_corner_filter = corner_filter
    if window.property(_CORNER_RADIUS_PROPERTY) is None:
        window.installEventFilter(corner_filter)
    radius = fallback_radius if fallback_radius is not None else 4 if small else 8
    window.setProperty(_CORNER_RADIUS_PROPERTY, radius)
    corner_filter.update_mask(window)
    return True


def disable_open_close_transition(window) -> bool:
    """禁用系统窗口显示和关闭过渡。

    亚克力背景由 DWM 异步合成，系统过渡快照可能尚未包含模糊层。
    禁用过渡并在显示前准备材质，可避免首帧背景不一致。
    """
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    return _set_attr(hwnd, _DWMWA_TRANSITIONS_FORCEDISABLED, 1)


# 通过 SetWindowCompositionAttribute 配置原生模糊或亚克力材质。

# 下拉浮层使用 WA_TranslucentBackground，对应 WS_EX_LAYERED 分层窗口和逐像素 alpha。
# DWMSBT_TRANSIENTWINDOW 在这类窗口上可能返回成功但不绘制系统背景材质，
# 因此使用 ACCENT 策略绘制模糊背景，窗口区域裁切时避免系统亚克力的矩形叠色。


_WCA_ACCENT_POLICY = 19
_ACCENT_ENABLE_BLURBEHIND = 3
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4


class _ACCENT_POLICY(ctypes.Structure):
    """对应 Windows 背景强调策略结构，包含效果模式、标志和 ABGR 叠色。"""

    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),  # 0xAABBGGRR：AA=叠色不透明度，BBGGRR=叠色(BGR)
        ("AnimationId", ctypes.c_uint),
    ]


class _WINCOMPATTRDATA(ctypes.Structure):
    """封装窗口合成属性编号、数据指针与缓冲区长度。"""

    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def needs_popup_paint_before_blur() -> bool:
    """Windows 10 先上传弹窗内容再启用模糊，避免合成器丢失首次绘制。"""
    return _supported() and sys.getwindowsversion().build < 22000


def acrylic_surface_alpha(alpha: float) -> float:
    """为 Windows 10 不带系统叠色的回退模糊提供足够的文字衬底。"""
    if _supported() and sys.getwindowsversion().build < 22000:
        return max(alpha, 0.94)
    return alpha


def apply_acrylic_blurbehind(window, dark: bool = True) -> bool:
    """为透明分层窗口配置 ACCENT 模糊背景。

    调用方先设置透明属性及半透明填充，再使用 SetWindowCompositionAttribute 应用材质。
    Windows 10 使用实时模糊，避免完整 Acrylic 在移动窗口时的合成延迟。
    窗口使用区域裁切时也采用无系统叠色的实时模糊，让 Qt 的透明像素约束四角；
    ACCENT 亚克力会在区域之外填充矩形叠色。失败时返回 False，保留基础填充。
    """
    if not _supported():
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    apply_dark_titlebar(window, dark)
    # 深色和浅色主题分别使用近黑和近白叠色，主要可读性由调用方填充决定。
    # 颜色编码为 0xAABBGGRR，灰阶颜色的三个通道相同。
    gradient = 0x30202020 if dark else 0x30F3F3F3
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        set_comp = getattr(user32, "SetWindowCompositionAttribute", None)
        if set_comp is None:
            return False
        set_comp.argtypes = [wintypes.HWND, ctypes.POINTER(_WINCOMPATTRDATA)]
        set_comp.restype = ctypes.c_int

        accent = _ACCENT_POLICY()
        clipped = window.property(_CORNER_RADIUS_PROPERTY) is not None
        # Win10 的拖动保护不能依赖是否安装圆角遮罩，否则调整窗口边框就会恢复慢速材质。
        live_blur = clipped or sys.getwindowsversion().build < 22000
        accent.AccentState = _ACCENT_ENABLE_BLURBEHIND if live_blur else _ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 0
        accent.GradientColor = gradient
        accent.AnimationId = 0
        data = _WINCOMPATTRDATA()
        data.Attribute = _WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.byref(accent), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(accent)
        res = set_comp(wintypes.HWND(hwnd), ctypes.byref(data))
        ok = bool(res)
        if ok:
            logger.debug("已启用%s磨砂", "实时模糊" if live_blur else " Acrylic")
        return ok
    except Exception as e:
        logger.debug("SetWindowCompositionAttribute 失败: %s", e)
        return False


# 对话框和消息框统一使用自定义标题栏，并在系统圆角不可用时裁切窗口区域。

# 应用级事件过滤器覆盖静态 QMessageBox 及插件创建的 QDialog，文件选择保留系统对话框。

_dialog_titlebar_mgr = None


def install_dialog_titlebar_theming(app) -> None:
    """每个应用安装一次共用对话框边框。"""
    global _dialog_titlebar_mgr
    if _dialog_titlebar_mgr is None:
        from src.ui.dialog_chrome import install

        _dialog_titlebar_mgr = install(app)


def refresh_open_dialog_titlebars(dark: bool) -> None:
    """主题切换时更新已打开对话框的标题栏明暗状态。"""
    if not _supported():
        return
    try:
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible() and w.isWindow():
                apply_dark_titlebar(w, dark)
    except Exception as e:
        logger.debug("刷新对话框标题栏失败: %s", e)
