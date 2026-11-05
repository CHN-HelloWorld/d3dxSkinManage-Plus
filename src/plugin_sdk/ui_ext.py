"""公开的插件界面扩展接口。

register_view 注册卡片内控件，register_page 注册顶级导航页，
show_dialog 打开宿主托管的对话框，notify 发送通知。
SDK 不依赖 Qt，插件自行使用 PySide6 构造控件，再交由宿主管理主题、父对象
和生命周期；停用插件时自动移除页面及对话框。无界面宿主时不执行界面操作。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.disposable import Disposable, DisposableStore
from src.plugin_sdk.errors import PermissionDenied

# factory -> QWidget（宿主侧创建/托管）
ViewFactory = Callable[[], Any]
PageFactory = Callable[[], Any]
STYLE_ROLES = frozenset(
    {"card", "title", "subtitle", "muted", "heading", "primary_button", "button", "input", "table", "progress"}
)


class _ViewRegistryLike(Protocol):
    """插件视图工厂注册所需的最小宿主协议。"""

    def register(self, plugin_id: str, view_id: str, factory: ViewFactory) -> Disposable:
        """登记带插件归属的视图工厂，由宿主按视图 ID 创建实例。"""
        ...


class _UIHostLike(Protocol):
    """宿主提供的插件页面、对话框和通知接口。"""

    def register_page(self, plugin_id: str, page_id: str, title: str, icon: str, factory: PageFactory) -> Disposable:
        """登记页面标题、图标和工厂，同时保留插件归属以便停用清理。"""
        ...

    def show_dialog(self, plugin_id: str, widget: Any, title: str, modal: bool) -> None:
        """由宿主展示指定控件，并按 modal 决定是否阻塞其他窗口交互。"""
        ...

    def notify(self, plugin_id: str, message: str, level: str) -> None:
        """以指定等级显示带插件归属的提示消息。"""
        ...

    def apply_style(self, plugin_id: str, widget: Any, role: str) -> Disposable:
        """为控件登记宿主语义样式，返回停止主题更新的句柄。"""
        ...


class UIExtensionPoints(ContextResource):
    """插件侧 UI 扩展门面（绑定 plugin_id，宿主注入 ViewRegistry 与 UIHost）。"""

    def __init__(
        self,
        plugin_id: str,
        view_registry: _ViewRegistryLike,
        ui_host: _UIHostLike | None = None,
        *,
        granted: frozenset[str] = frozenset(),
        subscriptions: DisposableStore | None = None,
    ) -> None:
        """绑定插件身份、视图注册表及可选图形宿主，并初始化上下文有效期。"""
        super().__init__()
        self._pid = plugin_id
        self._views = view_registry
        self._host = ui_host
        self._granted = granted
        self._subscriptions = subscriptions if subscriptions is not None else DisposableStore()

    def _require_views(self) -> None:
        """在注册界面或显示窗口前检查所属线程、有效期及界面权限。"""
        self._require_open()
        if "ui:views" not in self._granted:
            raise PermissionDenied("ui:views")

    # 卡片内嵌视图。

    def register_view(self, view_id: str, factory: ViewFactory) -> Disposable:
        """注册一个视图控件工厂（嵌在插件页该插件卡内）。view_id 对应 manifest views[].id。"""
        self._require_views()
        return self._subscriptions.add(self._views.register(self._pid, view_id, factory))

    # 页面、对话框和通知扩展。

    def register_page(self, page_id: str, title: str, factory: PageFactory, icon: str = "") -> Disposable:
        """注册顶级导航页；factory 返回根 QWidget，由宿主应用主题并加入页面栈和导航栏。

        停用插件时宿主自动移除该页与导航项。icon 为应用内已有图标名（如 "navigation/plugins"），
        留空时使用默认插件图标。
        """
        self._require_views()
        if self._host is not None:
            return self._subscriptions.add(self._host.register_page(self._pid, page_id, title, icon, factory))
        return self._subscriptions.add(Disposable(lambda: None))

    def show_dialog(self, widget: Any, title: str = "", modal: bool = True) -> Any:
        """展示插件提供的 QWidget，由宿主管理父子关系、Fluent 主题、圆角和停用时的关闭操作。

        modal=True 时使用模态对话框。返回宿主创建的 QDialog，无图形宿主时返回 None；
        插件可调用返回对象的 close、accept 等方法。
        """
        self._require_views()
        if self._host is not None:
            return self._host.show_dialog(self._pid, widget, title, modal)
        return None

    # 语义别名
    def show_popup(self, widget: Any, title: str = "") -> Any:
        """使用宿主对话框接口展示非模态窗口。"""
        return self.show_dialog(widget, title=title, modal=False)

    def apply_style(self, widget: Any, role: str) -> Disposable:
        """绑定 card、文字层级、按钮、input、table 或 progress 样式，并随宿主主题更新。"""
        self._require_views()
        if role not in STYLE_ROLES:
            raise ValueError(f"Unknown plugin style role: {role}")
        if self._host is None:
            return self._subscriptions.add(Disposable(lambda: None))
        return self._subscriptions.add(self._host.apply_style(self._pid, widget, role))

    def notify(self, message: str, level: str = "info") -> None:
        """发送一条 Toast 通知。level: info | success | warning | error。"""
        self._require_open()
        if self._host is not None:
            self._host.notify(self._pid, message, level)
