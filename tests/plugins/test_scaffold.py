import json
import shutil
import subprocess
import sys
from pathlib import Path

from src.plugins.runtime.registry import PluginState
from src.utils import i18n
from tests.plugins.test_operations import library as library
from tests.plugins.test_runtime import env as env


def test_generated_package_installs_translates_reloads_and_releases_shortcuts(env, qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from src.plugins.runtime.shortcuts import PluginShortcutHost
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    pid = "example.generated"
    output = tmp_path / "generated"
    run = subprocess.run(
        [sys.executable, "scripts/plugins/new_plugin.py", pid, "--dir", str(output), "--name", 'Sample "quoted" name'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run.returncode == 0, run.stderr
    package = output / pid
    manifest = json.loads((package / "plugin.json").read_text("utf-8"))
    assert manifest["contributes"]["shortcuts"][0]["command"] == pid + ".summary"
    archive = Path(shutil.make_archive(str(tmp_path / "generated-plugin"), "zip", package))
    manager, _, _ = env
    window = QWidget()
    qtbot.addWidget(window)
    settings = ShortcutSettings(window)
    manager.set_shortcut_host(PluginShortcutHost(settings, ShortcutDispatcher(settings, window)))
    result = manager.install_archive(archive)
    assert result.plugin_id == pid
    assert manager.get(pid).state is PluginState.DISCOVERED
    assert manager.trust(pid)
    monkeypatch.setattr(i18n.get_i18n(), "_current_lang", "en-US")
    assert manager.enable(pid), manager.get(pid).error
    assert manager.get(pid).state is PluginState.READY
    assert manager.activate(pid)
    context = manager.get(pid).context
    assert context.i18n.t("共 {total} 个 Mod，已启用 {enabled} 个。", total=2, enabled=1) == "2 mods, 1 enabled."
    assert settings.binding(pid + ".summary") == "Ctrl+Alt+M"
    assert manager.reload(pid)
    assert manager.get(pid).context is not context
    manager.shutdown()
    assert pid + ".summary" not in settings.bindings


def test_generated_plugin_native_signals_properties_and_parent_ownership(library, qtbot, tmp_path):
    import gc

    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent, QObject
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import QLabel, QPushButton

    from tests.plugins.test_mainwindow_service import PluginWindow

    pid = "example.generated-ui"
    output = tmp_path / "generated"
    run = subprocess.run(
        [sys.executable, "scripts/plugins/new_plugin.py", pid, "--dir", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run.returncode == 0, run.stderr
    package = output / pid
    entry = package / "entry.py"
    manifest_path = package / "plugin.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["permissions"].extend(["storage", "ui:views"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    entry.write_text(
        entry.read_text("utf-8")
        + """

from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

class Counter(QObject):
    valueChanged = Signal(int)

    def __init__(self, ctx, parent):
        super().__init__(parent)
        self.setObjectName("counterController")
        self._ctx = ctx
        self._value = ctx.storage.get("counter", 0)

    @Property(int, notify=valueChanged)
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        self._ctx.storage.set("counter", value)
        self.valueChanged.emit(value)

    @Slot()
    def increment(self):
        self.value += 1

ScaffoldPlugin = Plugin

class Plugin(ScaffoldPlugin):
    def activate(self, ctx):
        super().activate(ctx)
        ctx.ui.register_page("counter", "Counter", self._make_page)

    def _make_page(self):
        page = QWidget()
        page.setObjectName("generatedCounterPage")
        layout = QVBoxLayout(page)
        counter = Counter(self._ctx, page)
        label = QLabel(str(counter.value))
        label.setObjectName("counterValue")
        button = QPushButton("Increment")
        button.setObjectName("counterIncrement")
        layout.addWidget(label)
        layout.addWidget(button)
        counter.valueChanged.connect(label.setNum)
        button.clicked.connect(counter.increment)
        return page
""",
        encoding="utf-8",
    )
    archive = Path(shutil.make_archive(str(tmp_path / "generated-plugin"), "zip", package))
    window = PluginWindow(library[0])
    qtbot.addWidget(window)
    manager = window._plugin_manager
    try:
        assert manager.install_archive(archive).plugin_id == pid
        assert manager.trust(pid)
        assert manager.enable(pid), manager.get(pid).error
        assert manager.execute_command(pid + ".summary") == {"mods": 1, "enabled": 0}
        assert window._plugin_ui_host._notify_queue[-1][1] == "共 1 个 Mod，已启用 0 个。"
        # 控制器由插件页面的 Qt 父子关系持有，插件实例不额外保存 Python 引用。
        gc.collect()
        controller = window._stack.findChild(QObject, "counterController")
        label = window._stack.findChild(QLabel, "counterValue")
        button = window._stack.findChild(QPushButton, "counterIncrement")
        assert controller is not None
        changes = QSignalSpy(controller.valueChanged)
        button.click()
        assert label.text() == "1"
        assert controller.property("value") == 1
        assert changes.count() == 1 and changes.at(0) == [1]
        assert controller.setProperty("value", 8)
        assert label.text() == "8"
        assert changes.count() == 2 and changes.at(1) == [8]
        assert manager.get(pid).context.storage.get("counter") == 8

        assert manager.reload(pid)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert all(not shiboken6.isValid(obj) for obj in (controller, label, button))
        replacement = window._stack.findChild(QObject, "counterController")
        assert replacement.property("value") == 8
        assert window._stack.findChild(QLabel, "counterValue").text() == "8"

        assert manager.uninstall(pid)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(replacement)
        assert manager.get(pid) is None
        assert pid + ".summary" not in window._shortcut_settings.bindings
    finally:
        window.close()
