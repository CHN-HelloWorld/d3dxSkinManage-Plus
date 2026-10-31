"""验证管理页可见性控制视图激活与构造，同时保留已打开视图的输入状态。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QStackedWidget, QWidget

from src.ui.pages.plugins import PluginsPage
from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_manifest_v2 import make_plugin
from tests.plugins.test_runtime import env as env

ENTRY = '''"""视图插件的真实临时入口。"""
from PySide6.QtWidgets import QLineEdit
from src.plugin_sdk import PluginBase

class Plugin(PluginBase):
    """记录视图构造次数并提供可编辑内容。"""

    def activate(self, ctx):
        """只登记工厂，不提前创建控件。"""
        self.views_created = 0
        ctx.ui.register_view("details", self.make_view)

    def make_view(self):
        """返回新的输入控件，以便检查宿主是否重复构造。"""
        self.views_created += 1
        widget = QLineEdit("initial")
        widget.setObjectName("activationView")
        return widget
'''


def view_plugin(directory, plugin_id="test.view", *, startup=False):
    """创建按视图激活或显式启动激活的真实插件。"""
    plugin = make_plugin(
        directory,
        plugin_id,
        name=plugin_id,
        permissions=["ui:views"],
        contributes={"views": [{"id": "details"}]},
        activationEvents=["onStartup"] if startup else [],
    )
    (plugin / "entry.py").write_text(ENTRY, encoding="utf-8")


def hidden_page(manager, qtbot):
    """把管理页加入真实页面栈，并保持其他页面可见。"""
    stack = QStackedWidget()
    qtbot.addWidget(stack)
    stack.resize(1100, 720)
    stack.addWidget(QWidget())
    page = PluginsPage()
    stack.addWidget(page)
    stack.setCurrentIndex(0)
    stack.show()
    assert not page.isVisible()
    page.set_manager(manager)
    return stack, page


@pytest.mark.parametrize("refresh", ["bind", "startup", "rescan", "reload"])
def test_hidden_page_does_not_activate_view_plugins(env, qtbot, refresh):
    """绑定、启动通知及隐藏时的重新扫描或重载都不能触发视图入口。"""
    manager, directory, _ = env
    view_plugin(directory)
    prepare(manager, "test.view")
    if refresh == "startup":
        manager.discover()
    stack, page = hidden_page(manager, qtbot)
    if refresh in {"startup", "rescan"}:
        manager.load_all()
    elif refresh == "reload":
        assert manager.reload("test.view")
    record = manager.get("test.view")
    assert not page.isVisible()
    assert record.state.value == "ready"
    assert record.instance is None
    assert not page.findChildren(QLineEdit, "activationView")
    stack.setCurrentWidget(page)
    assert record.state.value == "active"
    assert record.instance.views_created == 1
    assert page.findChild(QLineEdit, "activationView").isVisible()


def test_visible_selection_activates_only_the_selected_view_plugin(env, qtbot):
    """首次显示仅激活默认详情，用户点击其他条目后再激活对应插件。"""
    manager, directory, _ = env
    view_plugin(directory, "test.first")
    view_plugin(directory, "test.second")
    prepare(manager, "test.first", "test.second")
    stack, page = hidden_page(manager, qtbot)
    assert all(record.instance is None for record in manager.records)
    stack.setCurrentWidget(page)
    assert manager.get("test.first").instance.views_created == 1
    assert manager.get("test.second").instance is None
    qtbot.mouseClick(page._entries["test.second"], Qt.MouseButton.LeftButton)
    assert manager.get("test.second").instance.views_created == 1
    assert manager.get("test.first").instance.views_created == 1


def test_startup_plugin_does_not_build_its_view_while_management_page_is_hidden(env, qtbot):
    """启动声明仍执行插件入口，但其详情工厂必须等待管理页显示。"""
    manager, directory, _ = env
    view_plugin(directory, startup=True)
    prepare(manager, "test.view")
    instance = manager.get("test.view").instance
    assert instance is not None
    stack, page = hidden_page(manager, qtbot)
    assert instance.views_created == 0
    stack.setCurrentWidget(page)
    assert instance.views_created == 1


def test_showing_management_page_again_preserves_cached_view_and_draft(env, qtbot):
    """隐藏期间的元数据刷新和再次显示不得重建已有视图或丢失输入。"""
    manager, directory, _ = env
    view_plugin(directory)
    prepare(manager, "test.view")
    stack, page = hidden_page(manager, qtbot)
    stack.setCurrentWidget(page)
    view = page.findChild(QLineEdit, "activationView")
    view.setText("unsaved draft")
    stack.setCurrentIndex(0)
    page.refresh()
    stack.setCurrentWidget(page)
    assert page.findChild(QLineEdit, "activationView") is view
    assert view.text() == "unsaved draft"
    assert view.isVisible()
    assert manager.get("test.view").instance.views_created == 1


def test_hidden_ready_settings_without_views_remain_available(env, qtbot):
    """无视图插件继续在待命状态构建设置，避免显示延迟影响配置服务。"""
    manager, directory, _ = env
    lazy_plugin(
        directory,
        permissions=["storage", "ui:settings"],
        contributes={"settings": [{"key": "label", "type": "str", "default": "initial"}]},
    )
    prepare(manager, "test.lazy")
    _stack, page = hidden_page(manager, qtbot)
    control = next(control for control in page.findChildren(QLineEdit) if control.property("pluginSetting") == "label")
    control.setText("saved while hidden")
    control.editingFinished.emit()
    assert manager.get_configuration("test.lazy").get("label") == "saved while hidden"
    assert manager.get("test.lazy").instance is None
