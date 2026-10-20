import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QDialog, QListWidget, QStackedWidget, QWidget

from src.plugins.runtime.registry import PluginState
from src.ui.pages.plugins import PluginCard, PluginsPage
from src.ui.plugin_ui_host import PluginUIHost
from tests.plugins.test_runtime import activate, write_plugin
from tests.plugins.test_runtime import env as env


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
