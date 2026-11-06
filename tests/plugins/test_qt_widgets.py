import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QDialog, QListWidget, QStackedWidget, QWidget

from src.plugins.runtime.registry import PluginState
from src.ui.pages.plugins import PluginCard, PluginsPage
from src.ui.plugin_ui_host import PluginUIHost
from tests.plugins.test_runtime import activate, write_plugin
from tests.plugins.test_runtime import env as env
from tests.qt_dialogs import interact
from tests.ui.test_dialog_chrome import chrome as chrome


def invalid_content(kind):
    if kind == "none":
        return None
    if kind == "python":
        return object()
    if kind == "qobject":
        return QObject()
    widget = QWidget()
    shiboken6.delete(widget)
    return widget


@pytest.fixture
def ui_host(qtbot, monkeypatch):
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    window = QWidget()
    qtbot.addWidget(window)
    window._stack = QStackedWidget(window)
    window._nav = QListWidget(window)
    host = PluginUIHost(window)
    yield host
    host.cleanup_plugin("example")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("kind", ["none", "python", "qobject", "deleted"])
@pytest.mark.parametrize("surface", ["page", "dialog"])
def test_invalid_content_leaves_no_partial_host_surface(ui_host, kind, surface):
    content = invalid_content(kind)
    if surface == "page":
        ui_host.register_page("example", "page", "Page", "", lambda: content)
    else:
        assert ui_host.show_dialog("example", content, "Dialog", False) is None
    assert ui_host._w._stack.count() == 0
    assert ui_host._w._nav.count() == 0
    assert not ui_host._w.findChildren(QDialog)
    assert not ui_host._pages
    assert not ui_host._dialogs


@pytest.mark.parametrize("modal", [False, True])
def test_plugin_dialog_opens_at_the_application_window_center(ui_host, chrome, qtbot, modal):
    """主窗口偏离屏幕中心时，模态与非模态插件弹窗仍按最终尺寸相对主窗口居中。"""
    window = ui_host._w
    window.setGeometry(120, 100, 1000, 700)
    window.show()
    content = QWidget()
    content.setFixedSize(320, 240)

    def check_center(dialog):
        """检查实际窗口边框的位置，而不是内部插件控件的局部坐标。"""
        assert (dialog.frameGeometry().center() - window.frameGeometry().center()).manhattanLength() <= 2
        assert window.screen().availableGeometry().contains(dialog.frameGeometry())

    if modal:
        with interact(QDialog, check_center):
            ui_host.show_dialog("example", content, "Dialog", True)
    else:
        dialog = ui_host.show_dialog("example", content, "Dialog", False)
        qtbot.wait(20)
        check_center(dialog)


def test_reopened_plugin_dialog_uses_the_current_application_window_center(ui_host, chrome, qtbot):
    """复用弹窗时跟随主窗口的新位置，并保留插件设定的窗口尺寸。"""
    window = ui_host._w
    window.setGeometry(120, 100, 1000, 700)
    window.show()
    content = QWidget()
    content.setFixedSize(320, 240)
    dialog = ui_host.show_dialog("example", content, "Dialog", False)
    dialog.resize(480, 360)
    dialog.hide()
    window.move(200, 160)
    dialog.show()
    qtbot.wait(20)
    assert dialog.size().toTuple() == (480, 360)
    assert (dialog.frameGeometry().center() - window.frameGeometry().center()).manhattanLength() <= 2


@pytest.mark.parametrize("kind", ["none", "python", "qobject", "deleted"])
def test_invalid_view_does_not_break_active_plugin_details(env, qtbot, kind):
    manager, directory, _ = env
    write_plugin(directory, contributes={"views": [{"id": "panel", "title": "Panel"}]})
    record = activate(manager)
    content = invalid_content(kind)
    record.context.ui.register_view("panel", lambda: content)
    page = PluginsPage()
    qtbot.addWidget(page)

    page.set_manager(manager)
    page.show()

    assert page.findChild(PluginCard) is not None
    assert record.state is PluginState.ACTIVE
    assert manager.views.get(record.id, "panel") is not None
