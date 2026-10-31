import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

from src.plugins.runtime import manager as runtime
from src.ui import anim, theme
from src.ui.pages.plugins import PluginCard, PluginsPage

ENTRY = """from src.plugin_sdk import PluginBase
from PySide6.QtWidgets import QLineEdit

class Plugin(PluginBase):
    def activate(self, ctx):
        self.views_created = 0
        ctx.ui.register_view("panel", self.make_view)

    def make_view(self):
        self.views_created += 1
        widget = QLineEdit("Plugin-owned draft")
        widget.setObjectName("fixtureView")
        return widget
"""


FAULTING_ENTRY = """from src.plugin_sdk import PluginBase
from PySide6.QtWidgets import QLineEdit

class Plugin(PluginBase):
    def activate(self, ctx):
        self.ctx = ctx
        ctx.commands.register(ctx.meta.id + ".fail", self.fail)
        ctx.ui.register_view("panel", self.make_view)

    def fail(self):
        raise RuntimeError("View initialization command failed")

    def make_view(self):
        self.ctx.commands.execute(self.ctx.meta.id + ".fail")
        widget = QLineEdit("Stale plugin view")
        widget.setObjectName("fixtureView")
        return widget
"""


@pytest.fixture
def plugin_page(qtbot, temp_data_dir, monkeypatch, request):
    external = temp_data_dir / "plugins"
    external.mkdir()
    monkeypatch.setattr(runtime, "get_plugins_dir", lambda: external)
    for pid, name in (("sample.alpha", "Alpha"), ("sample.beta", "Beta"), ("sample.gamma", "Gamma")):
        folder = external / pid
        folder.mkdir()
        manifest = {
            "id": pid,
            "name": name,
            "version": "1.0.0",
            "apiVersion": "2.0",
            "activationEvents": ["onStartup"],
            "entry": "entry.py",
            "author": "Example author",
            "description": name
            + (" color tools that keep settings and live plugin content while navigating the management page."),
            "permissions": ["storage", "ui:views", "ui:settings"],
            "contributes": {
                "views": [{"id": "panel", "title": "Plugin view"}],
                "settings": [{"key": "label", "type": "str", "title": "Label", "default": "Default"}],
            },
        }
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "entry.py").write_text(getattr(request, "param", ENTRY), encoding="utf-8")
    manager = runtime.PluginManager(SimpleNamespace(add_event_listener=lambda callback: None))
    manager.discover()
    for pid in ("sample.alpha", "sample.beta"):
        assert manager.trust(pid)
        assert manager.enable(pid)
    page = PluginsPage()
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(page)
    page.resize(1100, 720)
    page.set_manager(manager)
    page.show()
    qtbot.wait(20)
    yield page, manager
    page.close()
    manager.shutdown()


@pytest.fixture
def animated_plugin_page(plugin_page, monkeypatch):
    page, manager = plugin_page
    with monkeypatch.context() as animation_patch:
        animation_patch.setattr(anim, "_enabled", True)
        try:
            yield page, manager
        finally:
            if getattr(page._details, "_cross_fade_overlay", None) is not None:
                anim.cross_fade(page._details, None)


def visible_detail(page):
    cards = [card for card in page.findChildren(PluginCard) if card.isVisible()]
    assert len(cards) == 1, "Only the selected plugin's details should occupy the content pane"
    return cards[0]


def select(page, qtbot, plugin_id):
    row = next(
        (
            button
            for button in page.findChildren(QPushButton, "pluginEntry")
            if button.property("pluginId") == plugin_id
        ),
        None,
    )
    assert row is not None, "The plugin must be selectable in the library"
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)
    return visible_detail(page)


def visible_ids(page):
    return {row.property("pluginId") for row in page.findChildren(QPushButton, "pluginEntry") if row.isVisible()}


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("width", [1100, 700])
def test_themed_library_rows_keep_icon_and_title_inside_the_click_target(plugin_page, qtbot, qapp, mode, width):
    page, _manager = plugin_page
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()
    try:
        theme.set_theme(mode)
        theme.set_background_active(False)
        qapp.setStyleSheet(theme.render_qss(mode))
        page.retheme()
        page.resize(width, 720)
        qtbot.wait(20)
        for row in page.findChildren(QPushButton, "pluginEntry"):
            for control in (row._icon, row._name):
                assert control.height() >= control.minimumSizeHint().height()
                top_left = control.mapTo(row, QPoint(0, 0))
                bottom_right = control.mapTo(row, control.rect().bottomRight())
                assert row.rect().contains(top_left) and row.rect().contains(bottom_right), (
                    "The plugin icon and title must fit inside its clickable row"
                )
    finally:
        mode, background, sheet = previous
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(sheet)


def test_library_text_elides_to_the_final_label_width_after_resizing(plugin_page, qtbot, qapp):
    page, manager = plugin_page
    record = manager.get("sample.alpha")
    record.manifest = replace(record.manifest, name="Alpha " + "Long plugin title " * 6)
    page.refresh()
    previous_sheet = qapp.styleSheet()
    try:
        qapp.setStyleSheet(theme.render_qss(theme.current_theme()))
        for width in (1100, 700, 1100):
            page.resize(width, 720)
            qtbot.wait(20)
            for row in page.findChildren(QPushButton, "pluginEntry"):
                label, full_text = row._name, row._record.manifest.name
                available = label.contentsRect().width()
                assert label.fontMetrics().horizontalAdvance(label.text()) <= available
                if label.fontMetrics().horizontalAdvance(full_text) > available:
                    assert label.text().endswith("…"), "Long plugin text must indicate truncation"
                else:
                    assert label.text() == full_text, "Resizing must reveal text when it fits"
    finally:
        qapp.setStyleSheet(previous_sheet)


def test_plugin_selection_keeps_draft_view_and_saved_settings(plugin_page, qtbot):
    page, manager = plugin_page
    first = visible_detail(page)
    assert first._rec.id == "sample.alpha"
    view = first.findChild(QLineEdit, "fixtureView")
    view.setText("Keep this draft")
    setting = next(control for control in first.findChildren(QLineEdit) if control.accessibleName() == "Label")
    setting.setText("Saved label")
    setting.editingFinished.emit()

    assert select(page, qtbot, "sample.beta")._rec.id == "sample.beta"
    assert select(page, qtbot, "sample.alpha") is first
    assert first.findChild(QLineEdit, "fixtureView") is view
    assert view.text() == "Keep this draft"
    assert manager.get("sample.alpha").context.storage.get("settings.label") == "Saved label"
    assert manager.get("sample.alpha").instance.views_created == 1


def test_plugin_selection_cross_fades_only_when_target_changes(animated_plugin_page, qtbot):
    page, _manager = animated_plugin_page
    selected = select(page, qtbot, "sample.beta")
    assert selected._rec.id == "sample.beta", "Selection must complete before the animation finishes"
    overlay = getattr(page._details, "_cross_fade_overlay", None)
    assert overlay is not None and overlay.isVisible(), "Changing plugins must cross-fade the old content"

    assert select(page, qtbot, "sample.beta") is selected
    assert page._details._cross_fade_overlay is overlay, "Reselecting must not interrupt the current transition"
    qtbot.waitUntil(lambda: getattr(page._details, "_cross_fade_overlay", None) is None, timeout=1000)
    assert visible_detail(page) is selected


def test_rapid_plugin_selection_cancels_overlay_and_preserves_draft(animated_plugin_page, qtbot):
    page, manager = animated_plugin_page
    first = visible_detail(page)
    view = first.findChild(QLineEdit, "fixtureView")
    view.setText("Keep this draft during rapid navigation")
    setting = next(control for control in first.findChildren(QLineEdit) if control.accessibleName() == "Label")
    setting.setText("Saved during navigation")
    setting.editingFinished.emit()

    select(page, qtbot, "sample.beta")
    overlay = getattr(page._details, "_cross_fade_overlay", None)
    assert overlay is not None and overlay.isVisible(), "The first selection must start a real transition"
    assert select(page, qtbot, "sample.alpha") is first
    assert getattr(page._details, "_cross_fade_overlay", None) is None
    assert (not shiboken6.isValid(overlay)) or overlay.isHidden(), (
        "Rapid selection must hide the superseded snapshot immediately"
    )

    qtbot.wait(anim.DURATION["normal"] + 30)
    assert visible_detail(page) is first, "An old transition must never restore its former selection"
    assert first.findChild(QLineEdit, "fixtureView") is view
    assert view.text() == "Keep this draft during rapid navigation"
    assert manager.get("sample.alpha").context.storage.get("settings.label") == "Saved during navigation"
    assert manager.get("sample.alpha").instance.views_created == 1


@pytest.mark.parametrize(
    "plugin_id, operation, selected_id",
    [
        ("sample.alpha", "disable", "sample.beta"),
        ("sample.alpha", "uninstall", "sample.beta"),
        ("sample.beta", "uninstall", "sample.alpha"),
    ],
)
def test_runtime_changes_retire_transition_from_stopped_plugin(
    animated_plugin_page,
    qtbot,
    plugin_id,
    operation,
    selected_id,
):
    page, manager = animated_plugin_page
    select(page, qtbot, "sample.beta")
    overlay = getattr(page._details, "_cross_fade_overlay", None)
    assert overlay is not None and overlay.isVisible(), "The runtime change must occur during a transition"
    old_card = page._detail_cards[plugin_id]
    old_view = old_card.findChild(QLineEdit, "fixtureView")

    getattr(manager, operation)(plugin_id)
    assert (not shiboken6.isValid(overlay)) or overlay.isHidden(), (
        "A stopped plugin snapshot must disappear immediately"
    )
    assert getattr(page._details, "_cross_fade_overlay", None) is None
    assert visible_detail(page)._rec.id == selected_id
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(old_view), "Runtime disposal must still release plugin-owned widgets"
    qtbot.wait(anim.DURATION["normal"] + 30)
    assert visible_detail(page)._rec.id == selected_id


def test_permission_chips_wrap_without_losing_unknown_manifest_permissions(plugin_page, qtbot):
    page, manager = plugin_page
    permissions = (
        "mods:read",
        "mods:write",
        "mods:manage",
        "storage",
        "ui:tools-page",
        "ui:menus",
        "ui:settings",
        "ui:views",
        "network",
        "fs:read",
        "fs:write",
        "background",
        "process",
        "native",
        "future:permission",
    )
    record = manager.get("sample.gamma")
    record.manifest = replace(record.manifest, permissions=permissions)
    page.refresh()
    page.resize(900, 720)
    page._splitter.setSizes([400, 400])
    card = select(page, qtbot, "sample.gamma")
    card._permissions.click()
    qtbot.wait(20)

    chips = card.findChildren(QLabel, "pluginPermissionChip")
    assert {chip.toolTip() for chip in chips} == set(permissions), "Every declared permission must remain inspectable"
    assert len({chip.y() for chip in chips}) > 1, "Permission chips must wrap in a narrow detail pane"
    for chip in chips:
        assert chip.isVisible()
        assert chip.parentWidget().rect().contains(chip.geometry()), "A permission must fit inside the detail pane"
        assert chip.fontMetrics().horizontalAdvance(chip.text()) <= chip.contentsRect().width()
    source = card.findChild(QLabel, "pluginSource")
    assert source is not None and source.isVisible()
    assert source.mapTo(card, QPoint()).y() > max(chip.mapTo(card, chip.rect().bottomLeft()).y() for chip in chips), (
        "The source note must follow all wrapped permission rows"
    )


def test_refresh_and_other_plugin_changes_keep_selected_content(plugin_page, qtbot):
    page, manager = plugin_page
    first = visible_detail(page)
    select(page, qtbot, "sample.beta")
    select(page, qtbot, "sample.alpha")
    first.findChild(QLineEdit, "fixtureView").setText("Keep after another plugin stops")
    page.refresh()
    manager.disable("sample.beta")
    assert visible_detail(page) is first
    assert first.findChild(QLineEdit, "fixtureView").text() == "Keep after another plugin stops"
    assert manager.get("sample.alpha").instance.views_created == 1


def test_search_and_enabled_filters_preserve_selected_details(plugin_page, qtbot):
    page, _manager = plugin_page
    first = visible_detail(page)
    search = page.findChild(QLineEdit, "pluginSearch")
    assert search is not None, "The plugin library must be searchable"
    search.setText("BETA color")
    assert visible_ids(page) == {"sample.beta"}
    assert visible_detail(page) is first
    search.clear()
    enabled = page.findChild(QPushButton, "pluginFilter-enabled")
    disabled = page.findChild(QPushButton, "pluginFilter-disabled")
    qtbot.mouseClick(enabled, Qt.MouseButton.LeftButton)
    assert visible_ids(page) == {"sample.alpha", "sample.beta"}
    qtbot.mouseClick(disabled, Qt.MouseButton.LeftButton)
    assert visible_ids(page) == {"sample.gamma"}, "An untrusted plugin cannot appear enabled"
    assert visible_detail(page) is first
    search.setText("missing")
    assert visible_ids(page) == set()
    assert visible_detail(page) is first


@pytest.mark.parametrize("operation", ["reload", "rescan", "disable"])
def test_runtime_replacement_releases_old_plugin_content(plugin_page, operation):
    page, manager = plugin_page
    old_card = visible_detail(page)
    old_view = old_card.findChild(QLineEdit, "fixtureView")
    old_record = manager.get("sample.alpha")
    old_record.context.storage.set("settings.label", "Persisted")
    if operation == "reload":
        assert manager.reload("sample.alpha")
    elif operation == "rescan":
        manager.load_all()
    else:
        manager.disable("sample.alpha")
    current = visible_detail(page)
    assert current is not old_card
    assert current._rec.id == "sample.alpha"
    assert old_card.isHidden()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(old_view), "Stopped plugin callbacks must not survive in hidden details"
    if operation != "disable":
        control = next(item for item in current.findChildren(QLineEdit) if item.accessibleName() == "Label")
        assert control.text() == "Persisted"
    else:
        assert current.findChild(QLineEdit, "fixtureView") is None


def test_uninstalling_selected_plugin_selects_a_remaining_plugin(plugin_page):
    page, manager = plugin_page
    assert visible_detail(page)._rec.id == "sample.alpha"
    assert manager.uninstall("sample.alpha")
    assert visible_detail(page)._rec.id == "sample.beta"
    assert visible_ids(page) == {"sample.beta", "sample.gamma"}


@pytest.mark.parametrize("plugin_page", [FAULTING_ENTRY], indirect=True, ids=["factory-command-error"])
def test_factory_command_error_discards_the_stopped_runtime_view(plugin_page):
    page, manager = plugin_page
    assert manager.get("sample.alpha").state.value == "error"
    card = visible_detail(page)
    assert card._state_lbl.text() == "出错"
    assert card.findChild(QLineEdit, "fixtureView") is None
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert page.findChildren(PluginCard) == [card], "Reentrant construction must not leave an untracked detail pane"
    assert page._details.count() == 2  # 空状态及当前错误详情。


def test_refresh_during_factory_does_not_construct_the_same_view_twice(plugin_page):
    page, manager = plugin_page
    calls = []

    def factory():
        calls.append(True)
        if len(calls) == 1:
            page.refresh()
        return QLineEdit("Reentrant plugin content")

    manager.views.register("sample.beta", "panel", factory)
    page._select_plugin("sample.beta")
    assert visible_detail(page)._rec.id == "sample.beta"
    assert calls == [True]
    assert len(page.findChildren(PluginCard)) == 2


def test_selection_changed_during_factory_remains_current(plugin_page):
    page, manager = plugin_page
    first = visible_detail(page)

    def factory():
        page._entries["sample.alpha"].click()
        return QLineEdit("Background plugin content")

    manager.views.register("sample.beta", "panel", factory)
    page._select_plugin("sample.beta")
    assert visible_detail(page) is first
    assert page._selected_id == "sample.alpha"
