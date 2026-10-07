"""验证插件设置的双向同步、保存反馈与界面订阅生命周期。"""

import gc
import json
import weakref

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QSpinBox, QWidget
from shiboken6 import isValid

from src.plugin_sdk.errors import PluginError
from src.plugins.runtime.registry import PluginState
from src.ui.pages.plugins import PluginsPage
from src.ui.plugin_settings import PluginSettingsForm
from src.ui.widgets.toggle_switch import ToggleSwitch
from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_configuration import SETTINGS
from tests.plugins.test_runtime import env as env

pytestmark = pytest.mark.usefixtures("native_qt_teardown")


def test_ready_plugin_settings_validate_and_refresh_without_activation(env, qtbot):
    manager, directory, _ = env
    lazy_plugin(directory, permissions=["storage", "ui:settings"], contributes={"settings": SETTINGS})
    prepare(manager, "test.lazy")
    page = PluginsPage()
    qtbot.addWidget(page)
    page.set_manager(manager)
    page.show()
    spinner = page.findChild(QSpinBox)
    combo = page.findChild(QComboBox)
    assert spinner is not None and combo is not None
    assert spinner.minimum() == 1 and spinner.maximum() == 10
    assert spinner.value() == 3
    configuration = manager.get_configuration("test.lazy")
    spinner.setValue(7)
    assert configuration.get("limit") == 7
    configuration.set("limit", 9)
    assert spinner.value() == 9
    combo.setCurrentIndex(1)
    assert configuration.get("mode") == "full"
    reset = next(button for button in page.findChildren(QPushButton) if button.property("resetSetting") == "limit")
    reset.click()
    assert spinner.value() == 3
    assert configuration.get("limit") == 3
    assert manager.get("test.lazy").instance is None


def test_large_integer_setting_preserves_value_without_widget_overflow(env, qtbot):
    manager, directory, _ = env
    settings = [{"key": "limit", "type": "int", "default": 10**14}]
    lazy_plugin(directory, permissions=["storage", "ui:settings"], contributes={"settings": settings})
    prepare(manager, "test.lazy")
    page = PluginsPage()
    qtbot.addWidget(page)
    page.set_manager(manager)
    control = next(control for control in page.findChildren(QLineEdit) if control.property("pluginSetting") == "limit")
    assert control.text() == "100000000000000"
    control.setText("200000000000000")
    control.editingFinished.emit()
    assert manager.get_configuration("test.lazy").get("limit") == 2 * 10**14


def test_plugin_settings_survive_activation_rebuilds_reload_and_reenable(env, qtbot, qapp):
    """详情重建后修改设置应保留插件命令、持久值及当前表单的双向订阅。"""
    manager, directory, _ = env
    lazy_plugin(
        directory,
        permissions=["storage", "ui:settings", "ui:tools-page"],
        contributes={
            "commands": [{"id": "test.lazy.run"}],
            "toolsPage": [{"command": "test.lazy.run"}],
            "settings": [{"key": "only_enabled", "type": "bool", "default": False}],
        },
    )
    prepare(manager, "test.lazy")
    page = PluginsPage()
    qtbot.addWidget(page)
    page.set_manager(manager)
    page.show()
    store = directory.parent / "data/plugins_data/test.lazy/store.json"

    for transition in ("activation", "reload", "reenable"):
        previous_form = page.findChild(PluginSettingsForm)
        assert previous_form is not None
        if transition == "reload":
            assert manager.reload("test.lazy")
        elif transition == "reenable":
            assert manager.disable("test.lazy")
            assert manager.enable("test.lazy")
        assert manager.execute_command("test.lazy.run") == 1
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(previous_form)

        form = page.findChild(PluginSettingsForm)
        assert form is not None
        control = form.findChild(ToggleSwitch)
        assert control is not None
        assert control.isChecked() is (transition != "activation")
        configuration = manager.get_configuration("test.lazy")
        configuration.reset("only_enabled")
        assert not control.isChecked()
        changes = []
        subscription = configuration.on_changed(changes.append)
        try:
            control.setChecked(True)
            record = manager.get("test.lazy")
            assert record.state is PluginState.ACTIVE, record.error
            assert configuration.get("only_enabled") is True
            assert json.loads(store.read_text("utf-8"))["settings.only_enabled"] is True
            assert [item.command for item in manager.contributions.tools_page()] == ["test.lazy.run"]
            assert manager.execute_command("test.lazy.run") == 2

            configuration.set("only_enabled", False)
            assert not control.isChecked()
            control.setChecked(True)
            assert [(event.previous, event.value) for event in changes] == [(False, True), (True, False), (False, True)]
        finally:
            subscription.dispose()

    assert manager.uninstall("test.lazy")
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(form)
    assert not page.findChildren(PluginSettingsForm)
    assert not manager.commands.has("test.lazy.run")
    with pytest.raises(PluginError):
        configuration.set("only_enabled", False)


def test_settings_form_releases_observer_when_parent_is_deleted(env, qapp):
    """配置门面继续存活时，父控件销毁仍须释放旧表单及其订阅。"""
    manager, directory, _ = env
    lazy_plugin(directory, permissions=["storage", "ui:settings"], contributes={"settings": SETTINGS})
    prepare(manager, "test.lazy")
    configuration = manager.get_configuration("test.lazy")
    parent = QWidget()
    try:
        form = PluginSettingsForm(configuration, manager.contributions.settings("test.lazy"), parent)
        reference = weakref.ref(form)
        del form
        parent.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        assert reference() is None
        configuration.set("limit", 7)
        assert manager.get("test.lazy").state is PluginState.READY
    finally:
        if isValid(parent):
            parent.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
