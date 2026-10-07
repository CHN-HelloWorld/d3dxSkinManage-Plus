from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QStackedWidget, QWidget

from src.plugins.runtime.manifest import Manifest
from src.plugins.runtime.registry import PluginRecord, PluginState
from src.ui import settings_style, theme
from src.ui.pages.plugins import PluginCard, PluginsPage
from src.ui.plugin_ui_host import PluginUIHost


@pytest.fixture(autouse=True)
def palette():
    previous = theme.current_theme()
    background = theme.is_background_active()
    theme.set_theme("light")
    theme.set_background_active(False)
    yield
    theme.set_theme(previous)
    theme.set_background_active(background)


def test_management_page_rethemes_without_recreating_plugin_view(qtbot, tmp_path):
    page = PluginsPage()
    qtbot.addWidget(page)
    content = QLineEdit("unsaved plugin state")
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return content

    record = PluginRecord(
        Manifest("example", "Example", "1.0", "1.0", "entry.py", directory=tmp_path),
        state=PluginState.ACTIVE,
    )
    manager = SimpleNamespace(
        records=[record],
        discovery_errors={},
        add_change_listener=lambda callback: None,
        contributions=SimpleNamespace(
            views=lambda pid: [SimpleNamespace(data={"id": "view"})], settings=lambda pid: []
        ),
        views=SimpleNamespace(get=lambda pid, vid: factory),
        create_view=lambda pid, vid: factory(),
    )
    page.set_manager(manager)
    card = page.findChild(PluginCard)
    version = next(label for label in card.findChildren(QLabel) if label.text() == "v1.0")
    before = card.styleSheet()
    for mode, background in [("dark", False), ("dark", True), ("light", True), ("light", False)]:
        theme.set_theme(mode)
        theme.set_background_active(background)
        page.retheme()
        assert page.findChild(PluginCard) is card
        assert content.text() == "unsaved plugin state"
        assert factory_calls == [True]
        assert settings_style.surface(over="nav") in card.styleSheet()
        assert theme.color("text.tertiary") in version.styleSheet()
    assert card.styleSheet() == before


def test_page_wrappers_and_open_dialog_retheme_in_place(qtbot, monkeypatch):
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    window = QWidget()
    qtbot.addWidget(window)
    window._stack = QStackedWidget(window)
    window._nav = QListWidget(window)
    host = PluginUIHost(window)
    page_content = QLineEdit("page input")
    dialog_content = QLineEdit("dialog input")
    host.register_page("example", "page", "Page", "", lambda: page_content)
    wrapper = window._stack.widget(0)
    dialog = host.show_dialog("example", dialog_content, "Dialog", False)
    for mode, background in [("dark", False), ("dark", True), ("light", False)]:
        theme.set_theme(mode)
        theme.set_background_active(background)
        host.retheme()
        assert window._stack.widget(0) is wrapper
        assert wrapper.layout().itemAt(0).widget() is page_content
        assert page_content.text() == "page input"
        assert dialog_content.text() == "dialog input"
        assert theme.surface_color("bg.base", "page") in wrapper.styleSheet()
        assert theme.color("bg.base") in dialog.styleSheet()
        assert "rgba" not in dialog.styleSheet()
    host.cleanup_plugin("example")


@pytest.fixture
def dialog_host(qtbot, monkeypatch):
    from src.ui import win_effects

    monkeypatch.setattr(win_effects, "apply_rounded_corners", lambda *args, **kwargs: None)
    window = QWidget()
    qtbot.addWidget(window)
    return PluginUIHost(window)


def test_closed_dialog_remains_managed_when_reopened(dialog_host):
    host = dialog_host
    content = QLineEdit("unsaved input")
    dialog = host.show_dialog("example", content, "Reusable", False)
    dialog.close()
    theme.set_theme("dark")
    host.retheme()
    dialog.show()
    assert theme.color("bg.base") in dialog.styleSheet()
    assert content.text() == "unsaved input"
    host.cleanup_plugin("example")
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dialog)
    assert not shiboken6.isValid(content)


def test_destroyed_dialog_is_removed_from_theme_registry(dialog_host):
    host = dialog_host
    dialog = host.show_dialog("example", QLineEdit(), "Temporary", False)
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dialog)
    assert not host._dialogs.get("example")
    host.retheme()


def test_cleanup_hides_dialog_even_when_plugin_rejects_close(dialog_host):
    class RefuseClose(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Close:
                event.ignore()
                return True
            return False

    host = dialog_host
    content = QLineEdit("plugin data")
    dialog = host.show_dialog("example", content, "Blocked close", False)
    filter_ = RefuseClose(dialog)
    dialog.installEventFilter(filter_)
    assert not dialog.close()
    assert dialog.isVisible()
    host.cleanup_plugin("example")
    assert not dialog.isVisible()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dialog)
    assert not shiboken6.isValid(content)


def test_cleanup_discards_only_stopped_plugins_pending_notifications(dialog_host, monkeypatch):
    host = dialog_host
    emitted = []
    monkeypatch.setattr(host, "_emit_toast", lambda message, level: emitted.append((message, level)))
    host.notify("running", "still relevant", "info")
    host.notify("stopped", "stale", "warning")
    host.cleanup_plugin("stopped")
    host.flush_notifications()
    assert emitted == [("still relevant", "info")]


def test_cleanup_exits_modal_dialog_without_calling_stopped_plugin(dialog_host):
    host = dialog_host
    callbacks = []
    content = QLineEdit("modal input")

    def stop_plugin():
        dialog = host._dialogs["example"][0]
        dialog.finished.connect(lambda _: callbacks.append("finished"))
        host.cleanup_plugin("example")

    QTimer.singleShot(0, stop_plugin)
    dialog = host.show_dialog("example", content, "Modal", True)
    assert callbacks == []
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dialog)
    assert not shiboken6.isValid(content)


@pytest.mark.parametrize("prior_grants", [frozenset(), frozenset({"mods:read"})])
def test_untrusted_plugin_toggle_starts_off_and_can_prompt_for_trust(qtbot, monkeypatch, prior_grants, tmp_path):
    page = PluginsPage()
    qtbot.addWidget(page)
    record = PluginRecord(
        Manifest(
            "untrusted",
            "Untrusted",
            "2.0",
            "1.0",
            "entry.py",
            directory=tmp_path,
            permissions=("mods:read", "mods:write"),
        ),
        enabled=True,
        trusted=False,
        granted_permissions=prior_grants,
    )
    manager = SimpleNamespace(
        records=[record],
        add_change_listener=lambda _: None,
        discovery_errors={},
        trust=lambda pid: None,
        enable=lambda pid: None,
    )
    page.set_manager(manager)
    card = page.findChild(PluginCard)
    prompts = []
    monkeypatch.setattr(card, "_confirm_trust", lambda: prompts.append(True) or False)
    assert not card._toggle.isChecked()
    card._toggle.setChecked(True)
    assert prompts == [True]
    assert not card._toggle.isChecked()
