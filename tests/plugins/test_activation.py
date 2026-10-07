"""验证元数据发现、按需激活及过期注册清理。"""

import pytest

from src.plugin_sdk.errors import PluginError
from tests.plugins.test_manifest_v2 import make_plugin
from tests.plugins.test_runtime import env as env

ENTRY = """from src.plugin_sdk import PluginBase
class Plugin(PluginBase):
    def activate(self, ctx):
        self.ctx = ctx
        self.calls = []
        ctx.commands.register(ctx.plugin_id + ".run", self.run)
        ctx.events.on("game_changed", self.changed)
    def run(self):
        self.calls.append("command")
        return len(self.calls)
    def changed(self, **payload):
        self.calls.append(payload["game_id"])
"""


def lazy_plugin(directory, plugin_id="test.lazy", **changes):
    """创建具有命令和事件入口的真实惰性插件。"""
    changes.setdefault("contributes", {"commands": [{"id": plugin_id + ".run"}]})
    plugin = make_plugin(directory, plugin_id, **changes)
    (plugin / "entry.py").write_text(ENTRY, encoding="utf-8")
    return plugin


def prepare(manager, *plugin_ids):
    """发现插件并显式授权；不主动触发代码执行。"""
    manager.discover()
    for plugin_id in plugin_ids:
        manager.trust(plugin_id)
    manager.activate_enabled()


def test_command_contribution_is_visible_without_loading_code(env):
    manager, directory, _ = env
    lazy_plugin(directory)
    prepare(manager, "test.lazy")
    record = manager.get("test.lazy")
    assert record.state.value == "ready"
    assert record.instance is None
    assert [item.command for item in manager.contributions.commands()] == ["test.lazy.run"]
    assert manager.commands.has("test.lazy.run")
    assert manager.execute_command("test.lazy.run") == 1
    instance = record.instance
    assert record.state.value == "active"
    assert manager.execute_command("test.lazy.run") == 2
    assert record.instance is instance
    assert record.activation_reason == "onCommand:test.lazy.run"


def test_activation_event_reaches_newly_activated_subscriber(env):
    manager, directory, _ = env
    lazy_plugin(directory, activationEvents=["onEvent:game_changed"])
    prepare(manager, "test.lazy")
    assert manager.get("test.lazy").instance is None
    manager.emit("game_changed", game_id="game-a")
    assert manager.get("test.lazy").instance.calls == ["game-a"]
    manager.emit("game_changed", game_id="game-b")
    assert manager.get("test.lazy").instance.calls == ["game-a", "game-b"]


def test_untrusted_or_disabled_plugin_never_publishes_executable_contributions(env):
    manager, directory, _ = env
    lazy_plugin(directory)
    manager.load_all()
    assert not manager.commands.has("test.lazy.run")
    assert manager.contributions.commands() == []
    assert not manager.activate("test.lazy")
    manager.trust("test.lazy")
    manager.enable("test.lazy")
    manager.disable("test.lazy")
    manager.emit("game_changed", game_id="game-a")
    assert not manager.commands.has("test.lazy.run")
    assert manager.get("test.lazy").instance is None


def test_disposing_old_command_handle_does_not_remove_replacement(env):
    manager, directory, _ = env
    lazy_plugin(directory)
    prepare(manager, "test.lazy")
    manager.execute_command("test.lazy.run")
    context = manager.get("test.lazy").context
    first = context.commands.register("test.lazy.dynamic", lambda: 1)
    second = context.commands.register("test.lazy.dynamic", lambda: 2)
    first.dispose()
    assert manager.execute_command("test.lazy.dynamic") == 2
    second.dispose()
    assert not manager.commands.has("test.lazy.dynamic")
    callback = context.events.on("game_changed", lambda **_: pytest.fail("disposed subscription was called"))
    callback.dispose()
    manager.emit("game_changed", game_id="game-a")
    manager.disable("test.lazy")
    with pytest.raises(PluginError):
        context.commands.register("test.lazy.late", lambda: None)


def test_activation_failure_removes_every_partial_contribution(env):
    manager, directory, _ = env
    plugin = lazy_plugin(directory)
    (plugin / "entry.py").write_text(
        ENTRY.replace("    def run(self):", '        raise RuntimeError("activation failed")\n    def run(self):'),
        encoding="utf-8",
    )
    prepare(manager, "test.lazy")
    assert manager.execute_command("test.lazy.run") is None
    record = manager.get("test.lazy")
    assert record.state.value == "error"
    assert record.context is None
    assert manager.contributions.commands() == []
    assert not manager.commands.has("test.lazy.run")
