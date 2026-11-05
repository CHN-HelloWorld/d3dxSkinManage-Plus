"""将插件界面接口连接至主窗口。

管理动态页面、对话框和通知的主题、父子关系及生命周期，插件停用时统一释放。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from weakref import ref

import shiboken6
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk.disposable import Disposable
from src.ui import icons, theme
from src.ui.plugin_style import PluginStyleBindings
from src.utils.i18n import t as tr
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.ui.main_window import MainWindow

_DEFAULT_PAGE_ICON = "navigation/plugins"


class PluginUIHost:
    """实现 ui_ext._UIHostLike：被 PluginManager 注入，桥接到 MainWindow。"""

    def __init__(self, window: MainWindow) -> None:
        """保存主窗口，并按插件标识维护页面、对话框和待显示通知。"""
        self._w = window
        # plugin_id -> list[(nav_item, page_widget)]
        self._pages: dict[str, list[tuple[QListWidgetItem, QWidget]]] = {}
        # plugin_id -> list[QDialog]
        self._dialogs: dict[str, list[QDialog]] = {}
        # 窗口显示前先将通知排队，待布局完成后再显示 Toast，确保定位依据有效。
        self._notify_ready = False
        self._notify_queue: list[tuple[str, str, str]] = []
        self._styles = PluginStyleBindings()

    # 页面

    def register_page(
        self, plugin_id: str, page_id: str, title: str, icon: str, factory: Callable[[], QWidget]
    ) -> Disposable:
        """调用插件页面工厂并验证 Qt 控件，将其包装为主题页面后注册导航项和栈索引。"""
        try:
            content = factory()
        except Exception:
            logger.exception("插件 '%s' 页面工厂抛错: %s", plugin_id, page_id)
            raise
        if content is None:
            return Disposable(lambda: None)
        if not isinstance(content, QWidget) or not shiboken6.isValid(content):
            logger.error("插件 '%s' 页面 '%s' 必须返回有效的 PySide6 QWidget", plugin_id, page_id)
            return Disposable(lambda: None)

        # 套一层主题表面容器，使插件页背景与全局一致（插件控件本身用 Qt 默认样式）
        page = QWidget()
        page.setObjectName("pluginPageSurface")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._retheme_page(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(content)

        stack = self._w._stack
        index = stack.addWidget(page)

        icon_name = icon or _DEFAULT_PAGE_ICON
        item = QListWidgetItem(tr(title))
        try:
            icons.apply_icon(item, icon_name)
        except Exception:
            icons.apply_icon(item, _DEFAULT_PAGE_ICON)
        item.setData(Qt.ItemDataRole.UserRole, index)
        item.setData(Qt.ItemDataRole.UserRole + 1, icon_name)
        item.setData(Qt.ItemDataRole.UserRole + 2, tr(title))
        item.setSizeHint(QSize(140, 40))
        self._w._nav.addItem(item)

        self._pages.setdefault(plugin_id, []).append((item, page))
        logger.info("插件 '%s' 注册页面: %s（stack=%d）", plugin_id, title, index)
        return Disposable(lambda: self._remove_pages(plugin_id, [(item, page)]))

    # 对话框

    def show_dialog(self, plugin_id: str, widget: QWidget, title: str, modal: bool) -> QDialog | None:
        """验证插件控件并嵌入宿主对话框，按模态配置显示并保活窗口；无效内容返回 None。"""
        if widget is None:
            return None
        if not isinstance(widget, QWidget) or not shiboken6.isValid(widget):
            logger.error("插件 '%s' 对话框内容必须是有效的 PySide6 QWidget", plugin_id)
            return None
        dlg = QDialog(self._w)
        dlg.setObjectName("pluginDialogSurface")
        self._retheme_dialog(dlg)
        dlg.setWindowTitle(tr(title) if title else tr("插件"))
        dlg.setModal(modal)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(widget)
        # 圆角（与全局弹层一致；背景图开关无关）
        try:
            from src.ui import win_effects

            dlg.winId()
            win_effects.apply_rounded_corners(dlg, small=False)
        except Exception:
            pass
        self._dialogs.setdefault(plugin_id, []).append(dlg)
        # 关闭只隐藏可复用窗口，直到 Qt 实际销毁前继续管理。
        dlg.destroyed.connect(lambda _=None, d=ref(dlg), p=plugin_id: self._forget_dialog(p, d()))
        if modal:
            dlg.exec()
        else:
            dlg.show()
        return dlg

    @staticmethod
    def _retheme_page(page: QWidget) -> None:
        # 仅给宿主包装层设表面；不要把背景强制施加给插件的所有子控件。
        """为插件页面外层容器设置与当前应用背景一致的页面表面色。"""
        page.setStyleSheet(
            "QWidget#pluginPageSurface {{ background-color: {}; }}".format(theme.surface_color("bg.base", "page"))
        )

    @staticmethod
    def _retheme_dialog(dialog: QDialog) -> None:
        # 独立顶层窗口没有主窗背景图作底，必须保持不透明。
        """更新插件对话框的基础背景色，避免影响插件内容控件的自定义样式。"""
        dialog.setStyleSheet("QDialog#pluginDialogSurface {{ background-color: {}; }}".format(theme.color("bg.base")))

    def retheme(self) -> None:
        """原位刷新宿主管理的表面和导航图标，保留插件输入状态。"""
        self._styles.retheme()
        for pages in self._pages.values():
            for item, page in pages:
                self._retheme_page(page)
                icon_name = item.data(Qt.ItemDataRole.UserRole + 1) or _DEFAULT_PAGE_ICON
                try:
                    icons.apply_icon(item, icon_name)
                except Exception:
                    icons.apply_icon(item, _DEFAULT_PAGE_ICON)
        for dialogs in self._dialogs.values():
            for dialog in dialogs:
                self._retheme_dialog(dialog)

    def _forget_dialog(self, plugin_id: str, dlg: QDialog | None) -> None:
        """从插件保活列表中移除已销毁对话框，列表为空时删除插件条目。"""
        lst = self._dialogs.get(plugin_id)
        if lst and dlg in lst:
            lst.remove(dlg)
            if not lst:
                self._dialogs.pop(plugin_id, None)

    def apply_style(self, plugin_id: str, widget: QWidget, role: str) -> Disposable:
        """为插件控件绑定与宿主一致的样式，禁止插件依赖内部主题模块。"""
        return self._styles.bind(plugin_id, widget, role)

    # 通知

    def notify(self, plugin_id: str, message: str, level: str) -> None:
        # 等待窗口显示并完成布局后再定位通知。
        """窗口尚未准备好或不可见时排队通知，其余情况立即显示。"""
        if not self._notify_ready or not self._w.isVisible():
            self._notify_queue.append((plugin_id, message, level))
            return
        self._emit_toast(message, level)

    def _emit_toast(self, message: str, level: str) -> None:
        """把插件通知级别映射为成功或失败提示，提示控件不可用时记录调试信息。"""
        try:
            ok = level not in ("warning", "error")
            self._w._ensure_toast().done(message, ok=ok)
        except Exception:
            logger.debug("插件通知失败: %s", message)

    def flush_notifications(self) -> None:
        """窗口显示后由 MainWindow.showEvent 调用：放行排队的启动期通知。"""
        self._notify_ready = True
        pending = self._notify_queue
        self._notify_queue = []
        # Toast 为单实例，多条启动通知仅显示最后一条。
        if pending:
            _plugin_id, msg, level = pending[-1]
            self._emit_toast(msg, level)

    # 生命周期清理

    def cleanup_plugin(self, plugin_id: str) -> None:
        """插件停用：移除其页面与导航项、关闭其对话框。"""
        self._styles.cleanup_plugin(plugin_id)
        self._notify_queue = [item for item in self._notify_queue if item[0] != plugin_id]
        # 停用是强制回收。结束模态循环并立即隐藏，不受 closeEvent 拒绝影响；
        # 插件已停用，不再派发 finished/rejected 回调。
        for dlg in self._dialogs.pop(plugin_id, []):
            try:
                dlg.blockSignals(True)
                QDialog.done(dlg, QDialog.DialogCode.Rejected)
                QWidget.hide(dlg)
                dlg.deleteLater()
            except RuntimeError:  # Qt 对象可能已由插件销毁
                pass

        # 移除页面 + 导航项
        self._remove_pages(plugin_id, list(self._pages.get(plugin_id, [])))

    def _remove_pages(self, plugin_id: str, pages: list) -> None:
        """删除句柄拥有的页面，并同步其余导航项的实际栈索引。"""
        owned = self._pages.get(plugin_id, [])
        pages = [entry for entry in pages if entry in owned]
        if not pages:
            return
        self._pages[plugin_id] = [entry for entry in owned if entry not in pages]
        if not self._pages[plugin_id]:
            del self._pages[plugin_id]
        stack = self._w._stack
        nav = self._w._nav
        current_widget = stack.currentWidget()
        removed_widgets = {page for _, page in pages}
        current_removed = current_widget in removed_widgets
        for item, page in pages:
            row = nav.row(item)
            if row >= 0:
                nav.takeItem(row)
            stack.removeWidget(page)
            page.deleteLater()
        # removeWidget 会让后续控件的 stack 索引前移 → 重新按 widget 引用同步所有
        # 仍存活的插件页导航项 UserRole（内置页 0~6 不受影响，无需动）。
        for plist in self._pages.values():
            for item, page in plist:
                item.setData(Qt.ItemDataRole.UserRole, stack.indexOf(page))
        # 若当前正处于被删页，回退到「插件」页。
        if current_removed:
            try:
                self._w._select_nav_page(self._w.PAGE_PLUGINS)
            except Exception:
                pass
