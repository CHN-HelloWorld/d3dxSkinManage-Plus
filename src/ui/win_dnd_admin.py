"""支持 Windows 提权进程接收文件拖放。

UIPI 会阻止普通资源管理器向提权进程发起 OLE 拖放，仅放行窗口消息不能恢复该通道。
本模块撤销 Qt 注册的 OLE 目标，启用 WM_DROPFILES，并放行其所需的跨进程消息。
收到路径和落点后，转换为 Qt 拖放事件交给对应控件。

该通道不提供拖动期间的悬停反馈。应用内部 QDrag 操作期间须临时恢复 OLE 目标，
由 suspend_for_internal_drag 管理切换；未提权时不接管拖放。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QMimeData, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QWidget

from src.utils.logger import logger

_IS_WIN = sys.platform == "win32"

WM_COPYGLOBALDATA = 0x0049  # 未文档化：USER32 用它跨进程搬运全局内存句柄（HDROP）
WM_COPYDATA = 0x004A
WM_NCHITTEST = 0x0084  # 用于拖放目标的命中测试。
WM_DROPFILES = 0x0233
_MSGFLT_ADD = 1  # ChangeWindowMessageFilter（进程级，旧 API）
_MSGFLT_ALLOW = 1  # ChangeWindowMessageFilterEx（窗口级）
_ALLOW_MSGS = (WM_DROPFILES, WM_COPYGLOBALDATA, WM_COPYDATA, WM_NCHITTEST)
_TOKEN_ELEVATION_TYPE = 18  # TOKEN_INFORMATION_CLASS::TokenElevationType
# TOKEN_ELEVATION_TYPE：Default=1、Full=2、Limited=3，分别表示默认、已提权和受限令牌。
_TOKEN_ELEVATION_TYPE_FULL = 2
_TOKEN_QUERY = 0x0008


class _MSG(ctypes.Structure):
    """对应 Windows 消息结构，供原生事件过滤器读取文件拖放消息。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]


def _u32():
    """取得当前进程的 Windows 用户界面接口库。"""
    return ctypes.windll.user32  # type: ignore[attr-defined]


def _sh32():
    """取得用于解析文件拖放句柄的 Windows Shell 接口库。"""
    return ctypes.windll.shell32  # type: ignore[attr-defined]


def _ole32():
    """取得用于恢复 Qt 拖放目标的 OLE 接口库。"""
    return ctypes.windll.ole32  # type: ignore[attr-defined]


def is_uac_elevated() -> bool:
    """判断当前进程是否使用 UAC 提权后的完整令牌。

    管理员权限本身不代表存在完整性级别差异；UAC 关闭时不接管正常的 OLE 拖放。
    """
    if not _IS_WIN:
        return False
    try:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        advapi = ctypes.windll.advapi32  # type: ignore[attr-defined]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            value = wintypes.DWORD()
            size = wintypes.DWORD()
            ok = advapi.GetTokenInformation(
                token, _TOKEN_ELEVATION_TYPE, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)
            )
            return bool(ok) and value.value == _TOKEN_ELEVATION_TYPE_FULL
        finally:
            k32.CloseHandle(token)
    except Exception as exc:
        logger.debug("提权状态检测失败: %s", exc)
        return False


# 文件拖放通道的注册与撤销。

# 保存窗口对应的 Qt OLE 目标指针，供内部拖动时恢复注册。
_saved_targets: dict[int, int] = {}
_OLE_TARGET_PROP = "OleDropTargetInterface"


def _grab_qt_drop_target(hwnd: int) -> int:
    """获取窗口属性中的 Qt OLE 目标指针，并增加引用计数。

    RegisterDragDrop 将目标记录在 OleDropTargetInterface，保留该对象可在内部拖动时重新注册。
    """
    if hwnd in _saved_targets:
        return _saved_targets[hwnd]
    user32 = _u32()
    user32.GetPropW.restype = ctypes.c_void_p
    user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    ptr = user32.GetPropW(wintypes.HWND(hwnd), _OLE_TARGET_PROP)
    if not ptr:
        return 0
    try:  # 增加 IUnknown 引用计数，防止撤销注册时释放仍需复用的对象。
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        add_ref = ctypes.cast(
            ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[1], ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        )
        add_ref(ctypes.c_void_p(ptr))
    except Exception as exc:
        logger.debug("AddRef Qt 拖放目标失败: %s", exc)
    _saved_targets[hwnd] = int(ptr)
    return int(ptr)


def _arm(hwnd: int) -> None:
    """为顶层窗口放行必要消息，撤销 OLE 目标并启用文件拖放。"""
    h = wintypes.HWND(hwnd)
    user32, ole32, shell32 = _u32(), _ole32(), _sh32()
    for msg in _ALLOW_MSGS:
        user32.ChangeWindowMessageFilterEx(h, msg, _MSGFLT_ALLOW, None)
    # OLE 目标仍存在时，拖放源不会回退到 WM_DROPFILES，因此需先撤销注册。
    _grab_qt_drop_target(hwnd)
    ole32.RevokeDragDrop(h)
    shell32.DragAcceptFiles(h, True)


def _restore_ole_target(hwnd: int) -> bool:
    """恢复 Qt 的 OLE 拖放目标，并暂停文件消息通道。"""
    ptr = _saved_targets.get(hwnd, 0)
    if not ptr:
        return False
    ole32 = _ole32()
    ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _sh32().DragAcceptFiles(wintypes.HWND(hwnd), False)
    return ole32.RegisterDragDrop(wintypes.HWND(hwnd), ctypes.c_void_p(ptr)) == 0


def _widget_for_hwnd(hwnd: int) -> QWidget | None:
    """按原生句柄寻找可见顶层 Qt 控件，忽略遍历期间已销毁的窗口。"""
    for top in QApplication.topLevelWidgets():
        try:
            if top.isVisible() and int(top.winId()) == hwnd:
                return top
        except RuntimeError:  # 已析构
            continue
    return None


def _deliver(top: QWidget, paths: list[str], phys: QPoint) -> bool:
    """根据文件路径与落点构造 Qt 拖放事件。

    Windows 提供物理像素坐标，须按设备像素比转换为 Qt 逻辑坐标后命中目标控件。
    """
    dpr = top.devicePixelRatioF() or 1.0
    local = QPoint(int(phys.x() / dpr), int(phys.y() / dpr))
    gpos = top.mapToGlobal(local)
    target = QApplication.widgetAt(gpos) or top
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    global _last_mime
    _last_mime = mime  # 保留 MIME 数据引用，供延迟处理拖放的控件读取。
    widget: QWidget | None = target
    while widget is not None:
        if widget.acceptDrops():
            pos = widget.mapFromGlobal(gpos)
            enter = QDragEnterEvent(
                pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
            )
            QApplication.sendEvent(widget, enter)
            if enter.isAccepted():
                drop = QDropEvent(
                    QPointF(pos),
                    Qt.DropAction.CopyAction,
                    mime,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                drop.setDropAction(Qt.DropAction.CopyAction)
                QApplication.sendEvent(widget, drop)
                logger.info("管理员模式旧式拖放：%d 项投给 %s", len(paths), widget.__class__.__name__)
                return True
        widget = widget.parentWidget()
    logger.debug("管理员模式旧式拖放：落点 %s 上没有接收拖放的控件", local)
    return False


_last_mime: QMimeData | None = None


class _DropFilesFilter(QAbstractNativeEventFilter):
    """拦下 WM_DROPFILES，取出路径与落点后交给 :func:`_deliver`。"""

    def nativeEventFilter(self, event_type, message):
        """消费 Windows 文件拖放消息，提取路径和落点后释放拖放句柄，再向匹配的 Qt 窗口投递。"""
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message != WM_DROPFILES:
                return False, 0
            shell32 = _sh32()
            h_drop = wintypes.HANDLE(msg.wParam)
            count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
            buf = ctypes.create_unicode_buffer(32768)
            paths = []
            for i in range(count):
                if shell32.DragQueryFileW(h_drop, i, buf, 32768):
                    paths.append(buf.value)
            point = wintypes.POINT()
            shell32.DragQueryPoint(h_drop, ctypes.byref(point))
            shell32.DragFinish(h_drop)
            top = _widget_for_hwnd(int(msg.hwnd or 0))
            if top is not None and paths:
                _deliver(top, paths, QPoint(point.x, point.y))
        except Exception as exc:
            logger.warning("管理员模式旧式拖放处理失败: %s", exc)
        return True, 0


class _ArmOnShow(QObject):
    """顶层窗口显示或重新激活时注册文件消息通道。

    Qt 可能因拖放属性变化重新注册 OLE 目标，因此激活时需重新确认。
    """

    _EVENTS = (QEvent.Type.Show, QEvent.Type.WinIdChange, QEvent.Type.WindowActivate)

    def eventFilter(self, obj, event):
        """在已创建的顶层窗口显示或句柄变化时启用旧式拖放通道，失败仅记录调试信息。"""
        if event.type() in self._EVENTS and isinstance(obj, QWidget) and obj.isWindow():
            # 改窗口标志时，WState_Created 仍可能为 True，但自己的 HWND 已被清除。
            # winId 会在 WinIdChange 栈内重建窗口；这里只查询现有句柄，并排除
            # effectiveWinId 回退到父窗口的情况。正常 Show/Activate 会再次挂通道。
            if not obj.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
                return False
            try:
                wid = obj.effectiveWinId()
                if wid and QWidget.find(wid) is obj:
                    _arm(int(wid))
            except Exception as exc:
                logger.debug("挂旧式拖放通道失败: %s", exc)
        return False


_installed = False
_native_filter: _DropFilesFilter | None = None
_arm_filter: _ArmOnShow | None = None


def active() -> bool:
    """旧式拖放通道是否已生效（即：本进程被提权且已安装）。"""
    return _installed


def install(app: QApplication) -> bool:
    """按需安装拖放适配，非 Windows 或未提权时返回 False。"""
    global _installed, _native_filter, _arm_filter
    if _installed:
        return True
    if not _IS_WIN or not is_uac_elevated():
        return False
    try:
        user32 = _u32()
        for msg in _ALLOW_MSGS:
            # 进程级消息过滤覆盖无法逐个枚举的 OLE 和 USER32 辅助窗口。
            user32.ChangeWindowMessageFilter(msg, _MSGFLT_ADD)
        _native_filter = _DropFilesFilter()
        app.installNativeEventFilter(_native_filter)
        _arm_filter = _ArmOnShow(app)
        app.installEventFilter(_arm_filter)
        for top in QApplication.topLevelWidgets():
            if top.isVisible():
                _arm(int(top.winId()))
        _installed = True
        logger.info(
            "检测到以管理员身份运行：UIPI 会拦掉 OLE 拖放，已切到 Shell 旧式 "
            "WM_DROPFILES 通道（拖动过程无悬停高亮，落下即导入）"
        )
        return True
    except Exception as exc:
        logger.warning("管理员模式拖放补偿安装失败，拖放导入将不可用: %s", exc)
        return False


class suspend_for_internal_drag:
    """在应用内部 QDrag 操作期间恢复 Qt 的 OLE 拖放目标。

    使用之前保留的目标指针重新注册，操作结束后恢复文件消息通道；未提权时不处理。
    """

    def __init__(self, widget: QWidget) -> None:
        """在管理员拖放桥已启用时捕获内部拖动所属顶层窗口的原生句柄。"""
        self._hwnd = 0
        if _installed and widget is not None:
            top = widget.window()
            wid = top.winId() if top is not None else None
            self._hwnd = int(wid) if wid else 0

    def __enter__(self) -> suspend_for_internal_drag:
        """内部拖动期间恢复 Qt 的 OLE 目标，暂时让出旧式文件拖放通道。"""
        if self._hwnd:
            try:
                if not _restore_ole_target(self._hwnd):
                    logger.debug("恢复 Qt OLE 拖放目标失败，内部拖拽本次不可用")
            except Exception as exc:
                logger.debug("让位 OLE 拖放失败: %s", exc)
        return self

    def __exit__(self, *_exc) -> None:
        """内部拖动结束后重新启用旧式文件拖放通道，恢复失败不掩盖原异常。"""
        if self._hwnd:
            try:
                _arm(self._hwnd)
            except Exception as exc:
                logger.debug("恢复旧式拖放通道失败: %s", exc)
