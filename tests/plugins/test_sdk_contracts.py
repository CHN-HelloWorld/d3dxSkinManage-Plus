import json
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

from src.plugins.runtime.manager import PluginManager
from src.plugins.runtime.registry import PluginState
from tests.plugins.test_facades import library as library
from tests.plugins.test_runtime import activate, write_plugin
from tests.plugins.test_runtime import env as env


def test_install_result_annotation_is_resolvable():
    from src.plugins.runtime.installer import InstallResult

    assert get_type_hints(PluginManager.install_archive)["return"] is InstallResult


def test_command_collision_cannot_replace_another_plugin():
    from src.plugins.runtime.commands import CommandTable

    commands = CommandTable()
    commands.register("first.plugin", "first.plugin.command", lambda: "first")
    with pytest.raises(ValueError):
        commands.register("second.plugin", "first.plugin.command", lambda: "second")
    assert commands.execute("first.plugin.command") == "first"
    commands.register("first.plugin", "first.plugin.command", lambda: "updated")
    assert commands.execute("first.plugin.command") == "updated"


@pytest.mark.parametrize("resource", ["commands", "events", "ui"])
def test_disposed_context_cannot_restore_its_registrations(env, resource):
    from src.plugin_sdk import PluginError

    manager, directory, _ = env
    write_plugin(directory)
    context = activate(manager).context
    manager.disable(context.plugin_id)
    with pytest.raises(PluginError):
        if resource == "commands":
            context.commands.register("test.sample.late", lambda: None)
        elif resource == "events":
            context.events.on("game_changed", lambda **kw: None)
        else:
            context.ui.register_view("late", lambda: object())
    assert not manager.commands.has("test.sample.late")


def test_newer_minor_api_is_rejected_before_import(env):
    from src.plugin_sdk import API_VERSION

    manager, directory, _ = env
    major, minor = map(int, API_VERSION.split("."))
    write_plugin(directory, apiVersion=f"{major}.{minor + 1}")
    manager.discover()
    manager.trust("test.sample")
    assert not manager.enable("test.sample")
    assert manager.get("test.sample").state is PluginState.ERROR
    assert manager.get("test.sample").instance is None


def test_compute_worker_uses_public_sdk_without_loading_qt(tmp_path):
    entry = tmp_path / "compute.py"
    entry.write_text(
        """import json
import sys

class RejectQt:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"PySide6", "shiboken6", "PyQt6"}:
            raise ImportError("A computation worker must not load Qt")

sys.meta_path.insert(0, RejectQt())
from src.plugin_sdk import OperationResult, OperationTask, PluginBase, PluginContext
from src.plugin_sdk.presets import PresetInfo
from src.plugin_sdk.variants import VariantInfo

def compute(payload):
    task = OperationTask()
    task._finish(OperationResult(game_id="worker", succeeded_ids=("example",)))
    return {"total": sum(payload), "done": task.done, "ok": task.result.ok}

print(json.dumps(compute([3, 5, 8])))
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", entry.read_text(encoding="utf-8")],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"total": 16, "done": True, "ok": True}
